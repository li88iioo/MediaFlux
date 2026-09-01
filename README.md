<div align="center">

<a href="#readme"><img src="app/static/img/mediaflux-logo.svg" alt="MediaFlux Logo" width="320" /></a>

<br/>

**自动化家庭媒体整理与 STRM 流转中心**

*RSS 订阅 · Telegram 交互 · qBittorrent · 光鸭云盘 · TMDB 刮削整理 · STRM 直链 · Jellyfin / Emby 媒体库刷新*

<br/>

[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![FastAPI](https://img.shields.io/badge/FastAPI-0.140+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/) [![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](docs/部署指南.md) [![MIT License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

<br/>

[快速开始](#快速开始) • [项目由来](#项目由来) • [功能概览](#功能概览) • [工作流程](docs/tutorials/00_自动化流转全景与工作流程.md) • [部署指南](docs/部署指南.md) • [配置教程](docs/配置教程.md) • [常见问题](docs/常见问题.md) • [免责声明](docs/免责声明.md)

</div>

---

## 项目简介

**MediaFlux** 是一个为家庭媒体中心打造的自动化整理与流转工具。它将 RSS 追番、磁力下载、云盘转存、TMDB 刮削重命名、STRM 生成以及 Jellyfin/Emby 媒体库刷新串成一条自动运行的流水线。

日常追番或下载影视时，MediaFlux 能自动把下载好的文件整理进标准媒体库；如果使用光鸭云盘存储，它还能生成 `.strm` 文件供播放器通过 302 直链直接播放，不消耗本地服务器的转码 CPU 和出口带宽。

> 💡 **全链路工作流程全景**：点击查阅 [《自动化流转全景与工作流程指南》](docs/tutorials/00_自动化流转全景与工作流程.md)，了解任务下发、下载调度、刮削整理、STRM 增量同步与 302 直链播放的完整执行链路。

---

## 项目由来

每一个媒体折腾党大概都经历过类似的“折腾之痛”：

1. **工具链断裂与 API 异常**：早先一直使用 NASTool 进行追番与媒体整理，但在 qBittorrent 升级到 5.2 之后，因底层 API 变动经常抛出异常，自动化流水线频频断流。
2. **大容量上云的契机**：后来发现了性价比极高的光鸭云盘（108 元 500TB），便将本地占满机械硬盘的影视库全量搬迁至云端。但随之而来的问题是：如何让 Jellyfin / Emby 在不消耗本地服务器 CPU 转码和出口下行带宽的情况下，高速、稳定地直接播放云端影视？
3. **前人项目的启发与局限**：在探索云盘流转方案时，发现了优秀的开源项目 [TgtoDrive](https://github.com/walkingddd/TgtoDrive)（特别致敬原作者的开源探索！）；同时光鸭云盘Python 客户端 [guangyaclient (DDSRem-Dev/guangyaclient)](https://github.com/DDSRem-Dev/guangyaclient) 为底层的稳定通讯提供了关键基石。但在后续深入使用中，把 Jellyfin 升级到12.X版本后，遇到了一些 API 不兼容与整理流转方面的断层。

面对这一连串割裂的痛点，与其在各个工具的修补缝合中反复折腾，不如从零构建一套现代化、高可用、且对本地与云端均具备事务保障的完整流转体系。

于是 **MediaFlux** 诞生了——它原生支持最新版 qBittorrent、完美适配 Jellyfin / Emby 最新接口，并将 TMDB 严格刮削、光鸭云盘 302 直链秒播、防误删增量索引、Telegram 交互和全平台部署融为一体，让家庭媒体自动化真正变得稳定、省心、开箱即用。

> [!CAUTION]
> 本项目仅供 Python 编程学习、技术研究与个人合法家庭媒体资产归档整理使用。

---

## 功能概览

### 1. 下载与任务调度
- **多渠道接入**：支持 Mikan（蜜柑计划）等 RSS 自动追番、Telegram Bot 快捷提交磁力/种子/分享链接，或在 Web 界面手动添加。
- **双下载通道**：任务可推送到本地 **qBittorrent** 下载，也可推送到 **光鸭云盘** 离线转存。
- **自动触发**：下载完成后自动开始后续的刮削、整理与媒体库刷新。

### 2. TMDB 刮削与识别
- **精准匹配**：结合标题清洗、年份约束、拼音匹配与结构化解析，支持电影、剧集分季以及特别篇（`Specials`/`S00E##`）标准化归档。
- **待确认机制**：识别置信度较低的内容自动进入人工待确认列表，避免误归档。
- **自定义规则与映射锁**：支持自定义正则重命名规则；对特殊命名源支持一次锁定、后续永久精准匹配。

### 3. 本地媒体安全整理
- **多目录支持**：支持配置多个下载源目录和媒体库目录，可由 qB 下载完成自动触发，也可在 Web / Telegram 手动发起整理。
- **移动保障**：同盘原子重命名；跨盘写入校验完整性后再清理源文件。
- **垃圾文件清理**：整理完成后自动清理 sample、无用说明文档等垃圾文件，未知文件、外挂字幕与特效字体安全保留。

### 4. 光鸭云盘与 STRM 302 直链
- **免 Key 登录**：Web 端直接手机验证码登录，Token 本地保存并自动定时刷新。
- **云端文件管理**：支持文件树浏览、秒传转存、分享解析、批量改名和移动。
- **302 直链播放**：Jellyfin/Emby 读取本地 `.strm` 文件，MediaFlux 提供短时签名并 302 重定向到云盘 CDN 直链，播放不耗本地服务器 CPU 与出口带宽。
- **增量同步与防误删**：基于本地 SQLite 索引增量维护 STRM，网络抖动或远端异常时自动熔断，防止误删本地媒体库。

### 5. 媒体探索（实验性）
- **聚合榜单**：聚合 TMDB（需配置 `TMDB_API_KEY`）、豆瓣公共榜单（基于 `豆瓣 Frodo` 公共接口）与 Bangumi 番剧榜（基于 `BANGUMI_USER_AGENT` 规范请求）。
- **多级缓存与容灾**：所有探索元数据在本地 `SQLite` 中做分层缓存并支持 `stale` 容灾回退；如无需此功能可设置环境变量 `DISCOVERY_ENABLED=0` 或 `DISCOVERY_DOUBAN_ENABLED=0` 完全禁用。
- **边界说明**：**探索收藏不等于 RSS 订阅**；探索仅提供榜单浏览、详情检索与单次资源推送，持续监控更新请使用专属订阅规则。
- **资源检索**：点开媒体档案可检索已配置的站点资源，支持按版本归组和一键推送下载。

### 6. 日志、回退与管理
- **操作可溯**：每次整理和重命名均记录操作日志与媒体快照。
- **一键回退**：如归档错误，可在日志页一键重新匹配、送回源目录或撤销重命名。
- **Telegram Bot**：支持在 Telegram 中触发同步、整理网盘、管理订阅、接收整理通知与异常告警。

---

## 快速开始

MediaFlux 提供开箱即用的 Docker 容器化部署与 Python 源码运行方式：

### 方式一：Docker Compose 部署（推荐）


```bash
mkdir -p mediaflux && cd mediaflux
curl -fsSL https://raw.githubusercontent.com/li88iioo/MediaFlux/main/docker-compose.yml -o docker-compose.yml
```

打开 `docker-compose.yml`，把下载目录和媒体库左侧路径改成宿主机实际路径，然后启动：

```bash
docker compose up -d
```

浏览器访问 `http://服务器IP:1258/setup` 创建管理员。默认 host 网络会直接开放 Web `1258` 和页面中配置的媒体反代端口；无需 `.env`、专用用户、手动 Secret 或 `chown`。

需要 bridge 端口映射、固定非 root UID/GID 或迁移备份时，请参阅 [部署指南](docs/部署指南.md)。开发环境在源码仓库中使用独立的 `docker-compose.dev.yml` 与 `.env.development`。

---

### 方式二：Python 源码直接运行

1. **环境准备与依赖安装**（要求 Python 3.13+）：
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **启动服务**：
   ```bash
   python mediaflux.py start
   ```

3. **初始化设置**：
   浏览器访问 `http://127.0.0.1:1258/setup`，按引导创建管理员账号并配置访问权限。

---

## 运行目录与持久化规范

MediaFlux 严格隔离只读代码与持久化数据：

| 部署方式 | 数据库文件 | 配置文件 (`user.env`) | 缓存目录 | 日志目录 | STRM 输出目录 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Docker 容器** | `./data/mediaflux.db` | `./data/user.env` | `./data/cache` | `./data/logs` | `./strm` |
| **Python 源码** | `<repo>/db/mediaflux.db` | `<repo>/db/user.env` | `<repo>/db/cache` | `<repo>/db/logs` | `<repo>/strm-data` |

---

## 命令行运维 (CLI)

源码运行与容器内均可通过 `python mediaflux.py` 使用命令行运维工具：

```bash
# 启动服务
python mediaflux.py start [--host HOST] [--port PORT] [--data-dir /path/to/data]

# 查看服务状态
python mediaflux.py status

# 环境与权限诊断
python mediaflux.py doctor --source /path/to/downloads --target /path/to/media

# 创建数据备份
python mediaflux.py backup create --reason before-upgrade

# 校验备份完整性
python mediaflux.py backup verify /path/to/backup.zip

# 恢复备份（需先停止服务）
python mediaflux.py backup restore /path/to/backup.zip

# 生成脱敏支持包（排查问题时使用，不含数据库和密码密钥）
python mediaflux.py support-bundle
```

---

## 文档索引

- 📖 [**部署指南**](docs/部署指南.md)：包含 Docker、源码运行、Nginx/Caddy 反代配置及升级说明。
- 🖼️ [**配置教程（带截图）**](docs/配置教程.md)：TMDB、qBittorrent、Emby/Jellyfin、光鸭云盘、STRM 和本地整理分步图文教程。
- ❓ [**常见问题 (FAQ)**](docs/常见问题.md)：整理移动、局域网访问、STRM 播放、刮削排错常见疑问。
- ⚙️ [**配置参考**](docs/配置参考.md)：全量环境变量与配置项说明。
- 🛠️ [**开发文档**](docs/开发文档.md)：内部架构设计、统一整理流程与开发规范。
- 🎬 [**进阶教程专区**](docs/tutorials/)：
  - [自动化流转全景与工作流程](docs/tutorials/00_自动化流转全景与工作流程.md)
  - [Jellyfin / Emby 与 STRM 直链播放实战](docs/tutorials/01_Jellyfin与Emby媒体库及STRM播放实战.md)
  - [Mikan 蜜柑全自动追番与过滤实战](docs/tutorials/02_Mikan全自动追番与标签过滤实战.md)
  - [本地媒体安全移动与 qB 联动实战](docs/tutorials/03_本地媒体安全移动与qBittorrent联动实战.md)
  - [云盘大容量归档与冲突策略实战](docs/tutorials/04_云盘大容量影视归档与冲突策略实战.md)
  - [整理纠偏审计与数据回退实战](docs/tutorials/05_纠偏审计与数据回退实战.md)
  - [Apple TV / Infuse / VidHub 直连播放配置](docs/tutorials/06_AppleTV与Infuse及VidHub终极直连配置.md)
  - [性能调优与大规模媒体库优化指南](docs/tutorials/07_性能调优与大规模媒体库优化指南.md)

---

## 安全与隐私说明

1. **本地运行与零遥测**：MediaFlux 100% 运行在用户本地设备，**不包含任何远程遥测、数据上报或用户追踪代码**。
2. **私有凭据保护**：所有 API Key、密码与 Token 均保存在本地存储中，不上传任何第三方服务器。
3. **网络安全默认值**：全新安装默认仅监听本地回环地址（`127.0.0.1`），需要开启局域网或公网访问时，请配置反向代理并开启身份验证。

---

## 法律免责声明

1. **个人学习与管理用途**：MediaFlux 仅作为个人及家庭媒体整理与自动化管理的开源技术工具。使用者应严格遵守所在国家或地区的相关法律法规，不得将本软件用于任何侵犯版权或违法的用途。
2. **遵守第三方服务协议**：使用第三方云盘、索引源及 API（如 TMDB、豆瓣公共接口、Bangumi、Telegram 等）时，使用者须遵守相应服务商的服务条款与调用规范。
3. **零托管与无担保**：本项目不存储、不分发亦不托管任何实际音视频文件实体。本软件基于 MIT 许可证按“现状”（AS-IS）提供，使用者须自行做好数据备份与测试验证。

详细法律条款全文请参阅 [《免责声明全文》](docs/免责声明.md)。

---

## 鸣谢

MediaFlux 的诞生与演进离不开开源社区优秀项目与开发者的探索，特别鸣谢以下项目与维护者：

- [DDSRem-Dev/guangyaclient](https://github.com/DDSRem-Dev/guangyaclient)：为光鸭云盘的高效底层通讯、免 Key 验证码登录与 Token 管理提供了优秀的官方客户端 SDK 支持。
- [walkingddd/TgtoDrive](https://github.com/walkingddd/TgtoDrive)：在云盘流转与早期 STRM 模式的探索上带来了宝贵的架构启发。
- [qBittorrent](https://www.qbittorrent.org/) / [Jellyfin](https://jellyfin.org/) / [Emby](https://emby.media/)：为现代家庭媒体生态提供了强大的基础底座。
- [LINUX.DO](https://linux.do)：一个友好的技术社区。
---

## 开源许可证

本项目基于 [MIT License](LICENSE) 开源。
