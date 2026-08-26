# 实战：Jellyfin / Emby 媒体库与 STRM 302 播放

本指南介绍如何让 Jellyfin / Emby 读取 MediaFlux 生成的光鸭 STRM，并通过 **媒体反代实例**自动协商播放链路。

> **先说明一个关键概念：配置了 302，不代表所有请求都会返回 302。**
> MediaFlux 会根据媒体来源、客户端能力、协议和 Jellyfin / Emby 的 `PlaybackInfo` 结果，在“真 302、兼容中继、HLS / 转码”之间自动选择。用户只需要配置一套媒体反代，不需要为不同客户端再创建额外的 302 策略。

---

## 1. 三种实际播放链路

### 1.1 真 HTTP 302：客户端直接从光鸭 CDN 取视频

```text
客户端播放器
   │ 1. 通过媒体反代端口访问 Jellyfin / Emby
   ▼
MediaFlux 媒体反代 ───────► Jellyfin / Emby
   │                         │
   │ 2. 获取播放信息          │ 读取本地 .strm
   │ 3. 获取短时 signed URL   │
   ▼                         │
光鸭云盘 API                 │
   │                         │
   └── 4. MediaFlux 返回 HTTP 302 + Location: https://...guangyacdn...
                              │
客户端播放器 ────────────────┴──► 光鸭 CDN
             5. 直接拉取视频数据
```

真 302 时：

- Jellyfin / Emby 和 MediaFlux 仍负责登录、海报墙、播放协商与鉴权；
- 实际视频数据由播放器直接向光鸭 CDN 请求；
- MediaFlux 只处理控制请求和短时播放地址，不承载持续的视频下行流量；
- CDN 看到的是播放器出口 IP，而不是 MediaFlux 服务器出口 IP。

### 1.2 兼容中继：视频经过 MediaFlux，但不一定转码

```text
客户端播放器 ◄──── HTTP 200 / 206 ──── MediaFlux ◄──── 光鸭 CDN
```

当客户端无法安全跟随当前重定向链、浏览器受到跨域限制，或 Media3 / ExoPlayer 遇到 HTTP → HTTPS 跨协议兼容问题时，MediaFlux 会代理 Range 请求。此时通常仍是原始媒体直放，但视频流量会经过 MediaFlux。

### 1.3 HLS、转封装或转码：由 Jellyfin / Emby 负责

```text
客户端播放器 ◄──── .m3u8 / .ts / .m4s ──── Jellyfin / Emby
```

当容器、视频、音频或字幕不符合客户端能力时，Jellyfin / Emby 可能选择 HLS、转封装或转码。这类请求不会强制改成 302，以免得到“显示为直连、实际无法播放”的错误结果。

---

## 2. 当前客户端如何自动选择链路

| 客户端 / 场景 | 常见链路 | 说明 |
| --- | --- | --- |
| Infuse、VidHub、Fileball | **真 302 优先** | 客户端能跟随重定向且可直接解码该媒体时，视频数据直连 CDN |
| Yamby、Moonfin | **真 302 优先** | MediaFlux 保留其已验证可用的 signed URL 直连路径 |
| Jellyfin Android 原生播放器 | 真 302 或兼容中继 | 同协议链路可使用 302；MediaFlux 为 Media3 / ExoPlayer 的 HTTP → HTTPS 场景自动使用兼容中继 |
| Findroid | **兼容中继** | 当前使用已验证的完整 signed-media 中继链，播放成功不等于网络面板必须出现 302 |
| Jellyfin Web / Android 网页播放器 | 真 302、兼容中继或 HLS | 只有准确媒体版本被上游判定为可 Direct Play 时才允许真 302；仍受浏览器编解码和 CDN CORS 限制 |
| Jellyfin / Emby 本地实体视频 | 上游直放、转封装或转码 | 本地视频不属于光鸭 STRM 302 链路，继续交给上游媒体服务器处理 |

以下请求本来就不应被判断为“真 302 视频流”：

- 登录、海报、字幕、媒体详情、播放进度和 WebSocket；
- `HEAD` 探测请求；
- HLS 清单与分片（`.m3u8`、`.ts`、`.m4s`）；
- 客户端显式关闭 Direct Play / Direct Stream 的请求；
- 无法唯一绑定到光鸭文件、鉴权失败或 signed URL 获取失败的请求。

---

## 3. 媒体库目录结构规划

推荐在宿主机统一规划 STRM 输出目录，并只读挂载给 Jellyfin / Emby：

```text
/data/strm/
└── 光鸭云盘/
    ├── 电影/
    │   └── 肖申克的救赎 (1994) {tmdb-278}/
    │       └── 肖申克的救赎.1994.2160p.strm
    └── 剧集/
        └── 葬送的芙莉莲 (2023) {tmdb-209867}/
            ├── Season 1/
            │   ├── 葬送的芙莉莲.2023.S01E01.1080p.strm
            │   └── 葬送的芙莉莲.2023.S01E02.1080p.strm
            └── Specials/
                └── 葬送的芙莉莲.2023.S00E01.OVA.1080p.strm
```

Jellyfin / Emby 看到的路径可以与 MediaFlux 不同，但二者必须挂载同一批文件。只有绝对路径确实不一致时，才需要在媒体服务器配置中添加高级路径映射。

---

## 4. Docker Compose 端口与挂载示例

下面只展示与本教程有关的部分，请把它合并到自己的完整 Compose 配置中：

```yaml
services:
  mediaflux:
    image: ghcr.io/li88iioo/mediaflux:latest
    container_name: mediaflux
    ports:
      # MediaFlux Web 与 STRM /playgy 播放入口
      - "0.0.0.0:1258:1258"
      # 媒体反代实例示例端口；必须与页面中的实例监听端口一致
      - "0.0.0.0:18096:18096"
    volumes:
      - ./mediaflux-data:/app/db
      - /mnt/media/strm-data:/data/strm

  jellyfin:
    image: jellyfin/jellyfin:latest
    container_name: jellyfin
    volumes:
      - /mnt/media/strm-data:/media/strm:ro
      - ./jellyfin-config:/config
    ports:
      - "8096:8096"
```

注意：

- MediaFlux 容器内部 Web 端口固定为 `1258`；宿主机端口可按部署配置调整；
- `18096` 只是媒体反代实例的示例端口，每个实例都可以使用不同端口；
- Docker bridge 网络下，新增或更换实例端口后，要同步发布对应端口；
- 局域网示例使用 `0.0.0.0` 便于其他设备访问。公网部署应使用防火墙、鉴权和有效 HTTPS 反向代理，不要直接裸露管理端口。

---

## 5. MediaFlux 与媒体服务器配置

### 5.1 设置 STRM 播放地址

进入 **「STRM → 光鸭 STRM」**，找到 **「媒体反代播放服务地址」**：

1. 填写 Jellyfin / Emby 与实际播放器都能访问的 MediaFlux 地址，例如：
   - 局域网：`http://192.168.1.100:1258`
   - HTTPS 域名：`https://media.example.com`
2. 不要填写 `localhost` 或 `127.0.0.1`，除非媒体服务器和播放器确实与 MediaFlux 处于同一个网络命名空间；
3. 地址保存后执行一次**完整刷新 / 完整校准**，让已有 `.strm` 批量写入新地址；
4. 选择已整理的光鸭源目录并执行完整同步。

`.strm` 文件中写入的是 MediaFlux `/playgy` 播放入口。signed URL 是短时地址，由 MediaFlux 在播放时实时获取，不要把 CDN signed URL 手工写进 STRM。

### 5.2 创建媒体反代实例

进入 **「媒体反代 → 反代实例」**：

1. 选择已配置的 Jellyfin / Emby，或填写自定义上游地址；
2. 上游地址填写真实媒体服务器地址，例如 Docker 网络内的 `http://jellyfin:8096`；
3. 设置独立监听地址与端口，例如 `0.0.0.0:18096`；
4. 保存并加载后，确认实例显示“运行中”且连接测试正常；
5. **播放器以后连接媒体反代入口**，例如 `http://192.168.1.100:18096`，而不是绕过 MediaFlux 直接连接上游 `:8096`。

媒体反代负责转发 Jellyfin / Emby API、WebSocket 和本地视频请求，并只对能够精确识别的光鸭媒体改写播放链路。绕过媒体反代直接访问 `8096` 时，本文描述的客户端自动协商不会生效。

### 5.3 添加 Jellyfin / Emby 媒体库

以 Jellyfin 为例：

1. 在 Jellyfin 中添加电影、剧集等媒体库；
2. 目录选择容器内的 STRM 挂载路径，例如 `/media/strm/光鸭云盘/电影`；
3. 在 Jellyfin 控制台创建 API Key，并在 MediaFlux 的媒体服务器配置中保存；
4. 启用整理或 STRM 完成后的媒体库刷新；
5. 首次同步后执行一次媒体库扫描。

---

## 6. 如何确认当前到底是不是“真 302”

不要只看“能否播放”，也不要用 `curl -I` 判断。MediaFlux 会把 `HEAD` 当作安全探测处理，它不代表真实视频 GET 的最终链路。

检查播放器实际发出的媒体 `GET` 请求：

| 现象 | 当前链路 |
| --- | --- |
| MediaFlux 返回 `HTTP 302`，并带有 `Location: https://...guangyacdn...` | **真 302**；后续大流量由客户端直连 CDN |
| MediaFlux 持续返回 `HTTP 200 / 206` 和 Range 数据 | **兼容中继**；视频流量经过 MediaFlux |
| 请求 URL 包含 `.m3u8`、`.ts`、`.m4s`、`/hls` 或 `/master` | **Jellyfin / Emby HLS、转封装或转码链路** |
| 本地视频直接由 Jellyfin / Emby 返回 | 正常本地媒体链路，不属于光鸭 302 |

真 302 下，MediaFlux 只会出现获取 signed URL 和返回重定向的短请求，不应持续产生与视频码率相当的下行流量。

---

## 7. 客户端 IP 与 Jellyfin “Known Proxies”

MediaFlux 默认丢弃外部传入的转发头，只把实际 TCP 对端 IP 转发给 Jellyfin。若播放器直接连接媒体反代实例，无需额外配置。

当链路前面还有 Nginx、Caddy、负载均衡器或 FRP 隧道，并且希望 Jellyfin 设备页显示最终客户端 IP：

1. 编辑对应的媒体反代实例，展开 **「真实客户端 IP」**；
2. 开启 **「信任上游转发头」**；
3. 在 **「可信代理来源」** 中至少填写 MediaFlux 实际收到连接的直接来源 IP/CIDR，例如 Docker bridge 下可能是 `172.18.0.1/32`；若 `X-Forwarded-For` 中还包含其他受控中间代理，也应逐项加入；
4. MediaFlux 只有在 socket 对端命中该列表时才解析 `X-Forwarded-For`，并按可信代理链从右向左选择首个不可信地址；其他请求仍会丢弃伪造头；
5. 在 Jellyfin 的 **Known Proxies（已知代理）** 中，只填写 Jellyfin 实际看到的 MediaFlux 直连地址。它与 MediaFlux 表单中的“可信直接代理来源”属于链路的不同一跳，不应机械填写为同一个值。

不要填写 `*`、`0.0.0.0/0` 或 `::/0`。若局域网直连用户与可信入口经过同一个 NAT 地址进入 MediaFlux，应先通过防火墙限制该实例端口只允许可信入口访问，否则不要启用转发头信任。该功能只影响 Jellyfin / Emby 看到的客户端 IP，不改变真 302、兼容中继或 HLS 的播放选择。

另外：

- 真 302 时，光鸭 CDN 看到播放器出口 IP；
- 兼容中继时，光鸭 CDN 看到 MediaFlux 服务器出口 IP；
- Jellyfin 设备页显示的 IP 与 CDN 实际取流 IP 不是同一个概念。

---

## 8. 常见问题

### 播放器能登录，但光鸭 STRM 不走 302

依次检查：

1. 播放器连接的是媒体反代实例端口，而不是上游 Jellyfin / Emby 端口；
2. 媒体反代实例已启用，且监听端口已由 Docker / 防火墙放行；
3. STRM 内的 MediaFlux 地址可被媒体服务器和播放器访问；
4. 当前媒体能唯一匹配到光鸭文件，没有错误的 Item / MediaSource 映射；
5. 当前客户端是否进入了兼容中继或 HLS——这可能是正确降级，不一定是故障。

### Jellyfin Web 能打开媒体，但浏览器播放失败

浏览器除编解码能力外，还受 CDN CORS 和媒体元素安全策略限制。即使 Jellyfin 报告 `SupportsDirectPlay=true`，浏览器环境也不保证像原生播放器一样稳定跟随 CDN 直链。需要优先验证：

- 是否实际请求了 HLS 清单；
- 浏览器控制台是否出现 CORS 或 codec 错误；
- 同一媒体在 Infuse、VidHub、Yamby、Moonfin 等原生客户端是否正常。

不要为了追求网络面板中的“302”而强制关闭 Jellyfin 的必要转码或 HLS 回退。

### 本地视频没有 302

这是正常行为。本地实体视频继续由 Jellyfin / Emby 负责 Direct Play、Direct Stream 或转码；MediaFlux 不会把本地文件伪装成光鸭 CDN 直链。

---

## 9. 相关专题教程推荐

- 🧭 [**自动化流转全景与工作流程**](00_自动化流转全景与工作流程.md)
- 🌸 [**Mikan 番组计划全自动追番与标签过滤**](02_Mikan全自动追番与标签过滤实战.md)
- 📂 [**本地媒体安全移动与 qBittorrent 联动**](03_本地媒体安全移动与qBittorrent联动实战.md)
- ☁️ [**光鸭云盘影视归档、冲突策略与分享转存**](04_云盘大容量影视归档与冲突策略实战.md)
- 🛡️ [**整理纠偏审计、一键回退与映射锁实战**](05_纠偏审计与数据回退实战.md)
- 📺 [**Apple TV / Infuse / VidHub 302 直连配置与验证**](06_AppleTV与Infuse及VidHub终极直连配置.md)
- 🚀 [**性能调优与大规模媒体库优化指南**](07_性能调优与大规模媒体库优化指南.md)
