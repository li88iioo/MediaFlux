# 实战：Apple TV / Infuse / VidHub 302 直连配置与验证

本指南面向 Apple TV 4K、iPhone、iPad 和 Mac 用户，介绍如何让 **Infuse、VidHub、Fileball 等原生播放器**通过 MediaFlux 媒体反代访问 Jellyfin / Emby，并在条件满足时使用光鸭 CDN 真 302 播放。

> 真 302 的含义是“实际视频数据由播放器直接从 CDN 获取”，并不表示登录、海报、媒体详情、播放进度和 WebSocket 也会绕过 Jellyfin / Emby。画质、HDR、音频输出和字幕能力最终仍取决于媒体文件、播放器版本、Apple TV 型号及显示 / 音响设备。

---

## 1. 为什么原生播放器更适合 302 直连

| 对比项 | Jellyfin / Emby 服务端转码 | MediaFlux + 原生播放器真 302 |
| --- | --- | --- |
| 视频数据路径 | 文件经媒体服务器读取并重新输出 | 播放器收到 302 后直接连接光鸭 CDN |
| 服务器资源 | 可能占用 CPU / GPU、磁盘读取和下行带宽 | 只保留 API、鉴权和重定向等控制流量 |
| 原始画质 | 转码时可能降低分辨率、码率或 HDR 信息 | 不经过服务端转码，保留原始文件视频数据 |
| 音频处理 | 可能按客户端能力转码 | 由 Infuse / VidHub 和 Apple TV 按设备能力解码或输出 |
| 拖动与起播 | 受转码启动和服务器上行影响 | 主要取决于终端到 CDN 的网络质量和播放器缓存策略 |

不要把固定的 CPU 占用、起播毫秒数或拖动时间当成承诺。真 302 只改变视频数据路径，最终体验仍受宽带、CDN 节点、Wi-Fi、文件码率和终端解码能力影响。

---

## 2. 正确的端口关系

MediaFlux 主服务、媒体反代实例和上游媒体服务器是三个不同入口：

```text
STRM 文件内地址
http://NAS_IP:1258/playgy/...
        │
        └── MediaFlux 主服务：获取光鸭 signed URL

Infuse / VidHub 添加的服务器地址
http://NAS_IP:18096
        │
        └── MediaFlux 媒体反代实例
                │
                └── 上游 Jellyfin / Emby：http://jellyfin:8096
```

示例中的端口含义：

- `1258`：MediaFlux Web 与 `/playgy` STRM 播放入口；
- `18096`：某个媒体反代实例的独立监听端口，可以自行修改；
- `8096`：真实 Jellyfin 上游端口。

**Infuse / VidHub 应添加 `18096` 这一类媒体反代入口，而不是直接添加上游 `8096`。** 直接访问 `8096` 会绕过 MediaFlux 的 `PlaybackInfo` 改写和客户端自动协商。

Docker bridge 网络部署时，还要发布媒体反代实例端口：

```yaml
services:
  mediaflux:
    ports:
      - "0.0.0.0:1258:1258"
      - "0.0.0.0:18096:18096" # 与反代实例监听端口一致
```

---

## 3. MediaFlux 前置配置

### 3.1 配置 STRM 播放入口

进入 **「STRM → 光鸭 STRM」**：

1. 将 **「媒体反代播放服务地址」**设置为 Apple TV、Jellyfin / Emby 都能访问的 MediaFlux 地址，例如 `http://192.168.1.100:1258`；
2. 不要填写 `localhost` 或 `127.0.0.1`；
3. 若修改过地址，执行一次完整刷新 / 完整校准，更新已有 STRM 内容；
4. 确认目标光鸭目录已经同步，并已在 Jellyfin / Emby 中建立媒体库。

### 3.2 创建媒体反代实例

进入 **「媒体反代 → 反代实例」**：

1. 选择已配置的 Jellyfin / Emby，或填写自定义上游地址；
2. 上游地址示例：`http://jellyfin:8096` 或 `http://192.168.1.20:8096`；
3. 监听地址选择局域网可达地址，监听端口示例为 `18096`；
4. 保存并加载，确认实例状态为“运行中”且连接测试正常；
5. 确认 Apple TV 能访问媒体反代端口，也能正常解析和访问光鸭 CDN 域名。

MediaFlux 会自动区分光鸭 STRM 和本地实体视频：

- 光鸭 STRM：满足条件时返回真 302；
- 本地实体视频：继续由 Jellyfin / Emby Direct Play、Direct Stream 或转码；
- 不需要再为 Infuse、VidHub 单独创建第二套 302 策略。

---

## 4. Infuse 配置

1. 打开 Infuse，进入 `设置 → 共享 → 添加媒体服务器`；
2. 选择 Jellyfin 或 Emby；
3. **服务器地址填写 MediaFlux 媒体反代实例地址**，例如 `http://192.168.1.100:18096`；
4. 使用原 Jellyfin / Emby 账号登录；
5. 流式传输模式保持“自动”或“直接播放优先”，不要无条件强制转码；
6. Apple TV 系统中可按设备能力开启“匹配动态范围”和“匹配帧率”；
7. 选择一部已同步的光鸭 STRM 媒体进行测试。

Infuse 能直接解码源文件时，MediaFlux 通常会返回真 302。若媒体本身不受当前设备或播放器版本支持，播放器仍可能请求其他播放方式。

---

## 5. VidHub 配置

1. 打开 VidHub，点击添加媒体服务器；
2. 选择 Jellyfin / Emby；
3. 服务器地址填写媒体反代实例地址，例如 `http://192.168.1.100:18096`；
4. 使用原媒体服务器账号完成登录；
5. 播放设置使用“自动”或“直接播放优先”；
6. 根据网络环境调整缓存，不必盲目设置超大缓存；
7. 播放光鸭 STRM 并按下一节验证数据链路。

Fileball 等能够连接 Jellyfin / Emby、跟随标准 HTTP 重定向并直接解码源文件的原生客户端，配置思路相同。

---

## 6. 如何验证是真 302，而不是“能播放就算 302”

判断时应查看**实际媒体 GET**，不要使用 `curl -I`。`HEAD` 是探测请求，MediaFlux 会按安全探测链路处理。

### 真 302

应看到：

```text
HTTP/1.1 302 Found
Location: https://...guangyacdn.com/...
```

随后大流量发生在 Apple TV / iPhone / iPad 与光鸭 CDN 之间，MediaFlux 不持续承载视频码率对应的下行流量。

### 兼容中继

如果 MediaFlux 对媒体请求持续返回 `200` 或 `206 Partial Content`，并传输 Range 数据，则当前是兼容中继。它仍可能是原始文件直放，但视频流量经过 MediaFlux，因此不能称为“完全 302”。

### HLS、转封装或转码

如果请求中出现以下特征，则由 Jellyfin / Emby 负责 HLS、转封装或转码：

```text
.m3u8
.ts
.m4s
/hls
/master
```

这不是 302 故障，而是媒体服务器根据客户端能力做出的播放合同。

---

## 7. 公网、HTTPS 与反向代理

公网使用时建议给媒体反代入口配置有效 HTTPS，并确保：

- 证书链完整，Apple 设备能够信任；
- 反向代理允许 Jellyfin / Emby API、Range 请求和 WebSocket `/socket`；
- 反向代理不要吞掉或改写 MediaFlux 返回的 `Location`；
- Apple TV 能访问 302 的最终光鸭 CDN 域名；
- 防火墙只开放实际需要的端口。

若客户端通过 Nginx、Caddy 或负载均衡器访问 MediaFlux，MediaFlux 默认只信任它实际看到的 TCP 对端地址，不接受客户端自行提交的 `X-Forwarded-For`。因此 Jellyfin 设备页可能显示前置代理或 MediaFlux 地址，这不影响视频是否真 302。

若需要 Jellyfin 识别直接连接 MediaFlux 的客户端 IP，可在 Jellyfin 的 **Known Proxies（已知代理）** 中仅加入 MediaFlux 实际连接上游时使用的可信 IP。不要信任任意公网地址或无边界网段。

---

## 8. 常见故障排查

### 能登录媒体库，但播放没有经过 MediaFlux

最常见原因是客户端填了真实 Jellyfin `:8096`。删除该连接，重新添加 MediaFlux 媒体反代实例端口，例如 `:18096`。

### 提示“无法打开项目”或长时间转圈

按顺序检查：

1. Apple TV 能否访问媒体反代端口；
2. 媒体反代实例能否访问上游 Jellyfin / Emby；
3. STRM 中的 `:1258/playgy/...` 地址能否从媒体服务器和播放器访问；
4. Docker 是否发布了媒体反代实例端口；
5. HTTPS 证书、DNS 和防火墙是否允许访问最终 CDN；
6. 当前文件的视频、音频和字幕格式是否受播放器与设备支持。

### 可以播放，但 MediaFlux 有持续大流量

这通常表示当前为兼容中继或 HLS，而不是真 302。检查媒体 GET 的状态码和 URL 类型；不要只看播放是否成功。

### 302 后立即失败

确认最终 `Location` 指向可访问的光鸭 CDN，并检查客户端是否允许跟随重定向。signed URL 有时效性，不要复制后长期保存，也不要手工写入 STRM。

### 海报墙正常，但 WebSocket 报错

若通过反向代理访问媒体反代入口，确认 `/socket` 已启用 WebSocket Upgrade。WebSocket 只负责媒体服务器实时状态，不承载真 302 的视频数据，但持续失败会影响客户端在线状态、进度同步和部分交互。

---

## 9. 相关专题教程推荐

- 🧭 [**自动化流转全景与工作流程**](00_自动化流转全景与工作流程.md)
- 🎬 [**Jellyfin 与 Emby 媒体库及 STRM 302 播放**](01_Jellyfin与Emby媒体库及STRM播放实战.md)
- 🌸 [**Mikan 番组计划全自动追番与标签过滤**](02_Mikan全自动追番与标签过滤实战.md)
- 📂 [**本地媒体安全移动与 qBittorrent 联动**](03_本地媒体安全移动与qBittorrent联动实战.md)
- ☁️ [**光鸭云盘影视归档、冲突策略与分享转存**](04_云盘大容量影视归档与冲突策略实战.md)
- 🛡️ [**整理纠偏审计、一键回退与映射锁实战**](05_纠偏审计与数据回退实战.md)
- 🚀 [**性能调优与大规模媒体库优化指南**](07_性能调优与大规模媒体库优化指南.md)
