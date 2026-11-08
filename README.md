<div align="center">

<a href="#readme"><img src="app/static/img/mediaflux-logo.svg" alt="MediaFlux Logo" width="320" /></a>

<br/>

# MediaFlux：光鸭云盘整理、STRM 生成与 302 反代

**面向 Jellyfin / Emby 的自托管媒体自动化工具，也支持本地媒体整理与 AI Agent。**

*光鸭云盘 · TMDB 识别整理 · STRM 增量同步 · 302 直链播放 · qBittorrent · RSS · Telegram*

<br/>

[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![FastAPI](https://img.shields.io/badge/FastAPI-0.140+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/) [![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](docs/部署指南.md) [![MIT License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

<br/>

[快速开始](#快速开始) • [光鸭整理与 302 播放教程](docs/tutorials/01_Jellyfin与Emby媒体库及STRM播放实战.md) • [功能概览](#功能概览) • [工作流程](docs/tutorials/00_自动化流转全景与工作流程.md) • [部署指南](docs/部署指南.md) • [配置教程](docs/配置教程.md) • [常见问题](docs/常见问题.md) • [免责声明](docs/免责声明.md)

</div>

---

## 项目简介

**MediaFlux** 是一个 MIT 开源的自托管家庭媒体自动化项目，可完成 **光鸭云盘影视整理、TMDB 识别与重命名、STRM 文件生成，以及 Emby / Jellyfin 302 反代播放**。如果你想把光鸭中的影视整理成 Jellyfin / Emby 可读取的媒体库，可以从下面的完整教程开始。

它也支持 qBittorrent 本地下载与媒体整理、RSS 订阅、Telegram 交互和 AI Agent，不要求所有媒体都存储在云盘。

> **English:** MediaFlux is an MIT-licensed, self-hosted media automation tool for Jellyfin and Emby. It supports Guangya cloud drive organization, TMDB metadata matching, STRM generation, and a media reverse proxy with HTTP 302 direct playback where supported. It also integrates qBittorrent, RSS, Telegram, and an AI agent.

### 从光鸭云盘到播放器

```text
光鸭云盘文件
  → TMDB 识别与整理预览 → 确认后归档
  → STRM 增量同步 → Jellyfin / Emby 扫描媒体库
  → 播放器连接 MediaFlux 媒体反代 → 协商播放链路
```

**[阅读完整教程：光鸭云盘影视整理、STRM 生成与 Emby/Jellyfin 302 反代](docs/tutorials/01_Jellyfin与Emby媒体库及STRM播放实战.md)** —— 包含准备条件、配置步骤、界面截图、挂载与端口说明，以及如何验证实际播放链路。

> **302 的边界：** 当光鸭媒体和客户端满足直放条件时，视频数据由播放器直接向云盘 CDN 获取，MediaFlux 不中继持续的视频流。兼容中继仍会使用 MediaFlux 的带宽；HLS、转封装或转码由 Jellyfin / Emby 协商处理，不能保证所有客户端、所有文件都返回 302。

### 按你的需求开始

| 你要解决的问题 | 推荐入口 |
| --- | --- |
| 整理光鸭云盘影视，生成 STRM 并接入 Emby / Jellyfin 302 反代 | [光鸭整理到播放的完整教程](docs/tutorials/01_Jellyfin与Emby媒体库及STRM播放实战.md) |
| qBittorrent 下载完成后整理本地文件并刷新媒体库 | [本地媒体整理与 qBittorrent 联动](docs/tutorials/03_本地媒体安全移动与qBittorrent联动实战.md) |
| 通过 RSS 订阅与过滤规则自动接收更新 | [Mikan 追番与标签过滤](docs/tutorials/02_Mikan全自动追番与标签过滤实战.md) |
| 用自然语言查询、预览并确认媒体操作 | [Agent 配置与使用](docs/tutorials/08_Agent架构与使用实战.md) |

MediaFlux 负责媒体进入库前的自动化流转及播放入口协商，**不替代 Jellyfin / Emby 本身，也不提供媒体内容**。首次使用建议先部署并跑通少量样本，再开启自动整理或订阅任务。

## 项目由来

MediaFlux 起源于将本地下载、光鸭云盘和 Jellyfin / Emby 串联使用时的维护需求：希望减少多工具之间的手动交接，让下载、识别、整理、STRM 同步和媒体库刷新有统一的状态与排错入口。

项目参考了 [TgtoDrive](https://github.com/walkingddd/TgtoDrive) 的开源探索，以及 [guangyaclient](https://github.com/DDSRem-Dev/guangyaclient) 的光鸭云盘 Python 客户端实现，感谢相关作者的工作。

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
- **规则与映射锁**：支持识别预处理、TMDB 强制匹配规则与映射锁，对指定来源复用已确认的识别结果。
- **Agent 发布格式教学**：在对话中提供真实样本，由 Agent 推导并预览，确认后保存复用，无需填写手动表单。教学规则不替代 TMDB 身份匹配与人工确认，详见[Agent 教学指南](docs/tutorials/10_发布格式教学.md)。

### 3. 本地媒体安全整理
- **多目录支持**：支持配置多个下载源目录和媒体库目录，可由 qB 下载完成自动触发，也可在 Web / Telegram 手动发起整理。
- **移动保障**：同盘原子重命名；跨盘写入校验完整性后再清理源文件。
- **垃圾文件清理**：整理完成后自动清理 sample、无用说明文档等垃圾文件，未知文件、外挂字幕与特效字体安全保留。

### 4. 光鸭云盘与 STRM 302 直链
- **免 Key 登录**：Web 端直接手机验证码登录，Token 本地保存并自动定时刷新。
- **云端文件管理**：支持文件树浏览、秒传转存、分享解析、批量改名和移动。
- **媒体反代与播放协商**：Jellyfin / Emby 读取本地 `.strm` 文件，MediaFlux 对符合条件的光鸭媒体返回短时签名的 CDN 直链；不适合真 302 的场景使用兼容中继或保留上游 HLS / 转码链路。
- **增量同步与防误删**：基于本地 SQLite 索引增量维护 STRM，网络抖动或远端异常时自动熔断，防止误删本地媒体库。

### 5. 媒体探索（实验性）
- **聚合榜单**：聚合 TMDB（需配置 `TMDB_API_KEY`）、豆瓣公共榜单（基于 `豆瓣 Frodo` 公共接口）与 Bangumi 番剧榜（基于 `BANGUMI_USER_AGENT` 规范请求）。
- **多级缓存与容灾**：所有探索元数据在本地 `SQLite` 中做分层缓存并支持 `stale` 容灾回退；如无需此功能可设置环境变量 `DISCOVERY_ENABLED=0` 或 `DISCOVERY_DOUBAN_ENABLED=0` 完全禁用。
- **边界说明**：**探索收藏不等于 RSS 订阅**；探索仅提供榜单浏览、详情检索与单次资源推送，持续监控更新请使用专属订阅规则。
- **资源检索**：点开媒体档案可检索已配置的站点资源，支持按版本归组和一键推送下载。

### 6. Media Agent
- **自然语言操作**：可在 Web 或 Telegram 中直接查询媒体库、最近播放、缺集、下载任务、RSS/媒体订阅、资源站和光鸭云盘，无需记忆页面与工具名称。
- **多步协作**：Agent 可根据需求组合搜索、核对、整理、下载、STRM 和媒体库刷新等项目能力，并保留跨轮对话上下文。
- **偏好与媒体消费**：可保存画质、语言、题材和下载目标偏好，用于本地推荐、缺集选源与默认下载目标；支持查询和修改已看/收藏状态、管理播放列表。
- **任务闭环**：可查看下载到整理、STRM、入库复核的真实记录，确认开启任务跟踪或每日摘要；有可靠操作快照时可预览撤销。
- **写入安全**：读取操作可直接执行；写操作先生成冻结计划，用户确认后执行。持续自动化规则需一次明确授权，之后由既有调度器按规则运行，不让模型无限循环。

### 7. 日志、回退与管理
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

打开 `docker-compose.yml`，把下载目录和媒体库左侧路径改成宿主机实际路径。配置好上述路径后直接启动，随后在浏览器完成[首次安装](docs/首次安装.md)：

```bash
docker compose up -d
```

浏览器访问 `http://服务器IP:1258/setup` 创建管理员。默认 host 网络会直接开放 Web `1258` 和页面中配置的媒体反代端口；无需 `.env`、专用用户、手动 Secret 或 `chown`。

需要 bridge 端口映射、固定非 root UID/GID 或迁移备份时，请参阅 [部署指南](docs/部署指南.md)。开发环境在源码仓库中使用独立的 `docker-compose.dev.yml` 与 `.env.development`。

单个 MediaFlux 容器同时承载服务与 Agent 工作区。保留 Compose 中的 `security_opt`，让 MF 的 Python 子进程启动 Bubblewrap；内层工作区继续使用独立 namespace、清空 capabilities 并加载 seccomp。容器入口保持 `python mediaflux.py start`，具体运行条件见[部署指南](docs/部署指南.md#5-权限说明)。

---

### 方式二：Python 源码直接运行

Agent 源码运行适用于允许非特权 user namespace 的 Linux，依赖系统 `bubblewrap` 与 `libseccomp2`，与 Docker 共用同一隔离执行器。日常开发直接使用 `python mediaflux.py start`；可用 `python tools/agent_doctor.py --json` 实际检查 Bubblewrap、seccomp 与当前身份的 namespace 创建能力。

Ubuntu 源码环境若提示缺少 Bubblewrap 的 AppArmor profile，可先安装系统 `bubblewrap` 包，使用发行版随包提供的策略。执行器按需只读挂载现有解释器、标准库、共享库和常用命令。

1. **环境准备与依赖安装**（要求 Python 3.13+）：
   ```bash
   # Debian / Ubuntu 系统依赖
   sudo apt-get update
   sudo apt-get install -y bubblewrap libseccomp2
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **启动服务**：
   ```bash
   python mediaflux.py start
   ```

3. **初始化设置**：
   首次启动后访问 `http://127.0.0.1:1258/setup`，填写管理员用户名、密码和确认密码；成功后直接进入 `/login`，无需重启。已有账号直接登录；已有数据库或恢复异常时拒绝重新初始化，请检查备份与恢复状态。详见[首次安装](docs/首次安装.md)。

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
- 🗺️ [**项目全链路拓扑图**](docs/项目全链路拓扑图.md)：从启动、入口、下载、识别、整理到 STRM 播放、刷新、通知和恢复的完整流转图。
- 🎬 [**进阶教程专区**](docs/tutorials/)：
  - [自动化流转全景与工作流程](docs/tutorials/00_自动化流转全景与工作流程.md)
  - [光鸭云盘影视整理、STRM 生成与 Emby/Jellyfin 302 反代](docs/tutorials/01_Jellyfin与Emby媒体库及STRM播放实战.md)
  - [Mikan 蜜柑全自动追番与过滤实战](docs/tutorials/02_Mikan全自动追番与标签过滤实战.md)
  - [本地媒体安全移动与 qB 联动实战](docs/tutorials/03_本地媒体安全移动与qBittorrent联动实战.md)
  - [云盘大容量归档与冲突策略实战](docs/tutorials/04_云盘大容量影视归档与冲突策略实战.md)
  - [整理纠偏审计与数据回退实战](docs/tutorials/05_纠偏审计与数据回退实战.md)
  - [Apple TV / Infuse / VidHub 直连播放配置](docs/tutorials/06_AppleTV与Infuse及VidHub终极直连配置.md)
  - [性能调优与大规模媒体库优化指南](docs/tutorials/07_性能调优与大规模媒体库优化指南.md)
  - [Agent 架构、配置与使用实战](docs/tutorials/08_Agent架构与使用实战.md)
  - [Agent 工具清单与 Provider 能力参考](docs/tutorials/09_Agent工具与能力参考.md)
  - [通过 Agent 教学发布格式](docs/tutorials/10_发布格式教学.md)

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

- [DDSRem-Dev/guangyaclient](https://github.com/DDSRem-Dev/guangyaclient)：为光鸭云盘的高效底层通讯、免 Key 验证码登录与 Token 管理提供了 Python 客户端实现参考。
- [walkingddd/TgtoDrive](https://github.com/walkingddd/TgtoDrive)：在云盘流转与早期 STRM 模式的探索上带来了宝贵的架构启发。
- [qBittorrent](https://www.qbittorrent.org/) / [Jellyfin](https://jellyfin.org/) / [Emby](https://emby.media/)：为现代家庭媒体生态提供了强大的基础底座。
- [LINUX.DO](https://linux.do)：一个友好的技术社区。
---

## 开源许可证

本项目基于 [MIT License](LICENSE) 开源。
