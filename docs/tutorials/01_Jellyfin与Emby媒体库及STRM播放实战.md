# 进阶实战：Jellyfin / Emby 媒体库搭建与 STRM 302 直链播放

本实战指南面向希望使用 Jellyfin、Emby 或 Plex 搭建家庭影院，并结合 MediaFlux 实现光鸭云盘海量影视「零服务器转码流量、秒级 302 直链播放」的用户。

---

## 1. 播放架构与数据流向

```text
┌────────────────────────────────────────────────────────┐
│                   客户端播放器                         │
│ (Infuse / VidHub / Fileball / Jellyfin Official App)   │
└───────────┬───────────────────────────────▲────────────┘
            │ 1. 浏览媒体库与发起播放请求     │ 5. 直连 CDN 拉取视频流
            ▼                               │    (不占服务器下行带宽)
┌───────────────────────┐                   │
│   Jellyfin / Emby     │                   │
│ (读取本地 .strm 文件) │                   │
└───────────┬───────────┘                   │
            │ 2. 解析 .strm 内的 URL 地址   │
            ▼                               │
┌───────────────────────┐                   │
│   MediaFlux 服务端    │                   │
│  (/playgy 302 鉴权)   ├───────────────────┘
└───────────┬───────────┘   4. HTTP 302 重定向到 CDN 直链
            │ 3. 向云盘获取短时 signedURL
            ▼
┌───────────────────────┐
│     光鸭云盘 API      │
└───────────────────────┘
```

### 核心收益
1. **零 CPU 占用**：服务端无需实时压制转码，所有解码工作由客户端硬件完成（支持 4K HDR、杜比视界 Profile 8/5）。
2. **零服务器带宽消耗**：媒体服务器仅返回 302 重定向，客户端播放器直接连接云端 CDN 高速节点拉取视频数据。
3. **元数据完整**：Jellyfin/Emby 正常刮削演员、海报、剧集概述、演职员表和分季信息。

---

## 2. 媒体库目录结构规划

推荐在宿主机统一规划 STRM 输出目录，并挂载给 Jellyfin / Emby：

```text
/data/strm/
├── 电影/
│   └── 肖申克的救赎 (1994) [tmdbid-278]/
│       └── 肖申克的救赎.1994.2160p.strm
└── 剧集/
    └── 葬送的芙莉莲 (2023) [tmdbid-209867]/
        ├── Season 01/
        │   ├── 葬送的芙莉莲.2023.S01E01.1080p.strm
        │   └── 葬送的芙莉莲.2023.S01E02.1080p.strm
        └── Specials/
            └── 葬送的芙莉莲.2023.S00E01.OVA.1080p.strm
```

---

## 3. Docker Compose 挂载配置

在 `docker-compose.yml` 中确保 MediaFlux 与 Jellyfin 共享挂载该目录：

```yaml
services:
  mediaflux:
    image: ghcr.io/li88iioo/mediaflux:latest
    container_name: mediaflux
    environment:
      - STRM_ROOT=/data/strm
    volumes:
      - ./db:/app/db
      - /mnt/media/strm-data:/data/strm
    ports:
      - "127.0.0.1:1258:1258"

  jellyfin:
    image: jellyfin/jellyfin:latest
    container_name: jellyfin
    volumes:
      - /mnt/media/strm-data/电影:/media/movies:ro
      - /mnt/media/strm-data/剧集:/media/tvshows:ro
      - ./jellyfin-config:/config
    ports:
      - "8096:8096"
```

---

## 4. MediaFlux 控制台关键配置

进入 Web 侧边栏 **「STRM」**：

1. **设置 `GY_STRM_BASE_URL`**（重点）：
   - 填入 Jellyfin 容器能够直接访问 MediaFlux 的 IP 与端口。
   - 局域网环境：`http://192.168.1.100:1258`
   - 同一 Docker 网络：`http://mediaflux:1258`
   - **严禁填写 `http://127.0.0.1:1258` 或 `http://localhost:1258`**（否则 Jellyfin 容器内部请求回环地址会连接失败）。
2. **勾选源目录**：选择云盘内已经整理完毕的影视目录。
3. **点击「立即全量同步」**：系统将在秒级内生成全部 `.strm` 文件，并建立 SQLite 索引。

---

## 5. 媒体服务器自动刷新与播放器配置

### 5.1 媒体库自动刷新
1. 在 Jellyfin 中生成 API Key：`控制台 -> API 密钥 -> 添加`。
2. 在 MediaFlux 的「设置」中填入 Jellyfin URL 与 API Key。
3. 勾选 **「整理/STRM 完成后刷新媒体服务器」**。后续每当有新影视归档或同步，Jellyfin 将自动触发增量扫描。

### 5.2 播放器客户端推荐
- **iOS / macOS / Apple TV**：Infuse Pro、VidHub、Fileball
- **Android / Android TV**：Jellyfin 官方客户端（ExoPlayer 模式）、Kodi（结合 Jellyfin 插件）
- **Windows / Mac**：Jellyfin Media Player (MPV 内核)

---

## 6. 相关专题教程推荐

- 🧭 [**自动化流转全景与工作流程**](00_自动化流转全景与工作流程.md)
- 🌸 [**Mikan 番组计划全自动追番与标签过滤**](02_Mikan全自动追番与标签过滤实战.md)
- 📂 [**本地媒体安全移动与 qBittorrent 联动**](03_本地媒体安全移动与qBittorrent联动实战.md)
- ☁️ [**光鸭云盘影视归档、冲突策略与分享转存**](04_云盘大容量影视归档与冲突策略实战.md)
- 🛡️ [**整理纠偏审计、一键回退与映射锁实战**](05_纠偏审计与数据回退实战.md)
- 📺 [**Apple TV / Infuse / VidHub 终极 302 直连播放**](06_AppleTV与Infuse及VidHub终极直连配置.md)
- 🚀 [**性能调优与大规模媒体库优化指南**](07_性能调优与大规模媒体库优化指南.md)
