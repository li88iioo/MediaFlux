# Changelog

所有关于 MediaFlux 的重大变更都将记录在此文件中。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.15] - 2026-10-31

### Added
- 新增签名 STRM 的云盘画质查询与解析接口，供支持该协议的客户端选择云盘已有版本；一个媒体仍只需要一个 STRM，不创建云端转码任务（[`9853f8cc`](https://github.com/li88iioo/MediaFlux/commit/9853f8cc)）。
- MF 302 反代在播放信息中保留云盘画质入口，并通过当前连接入口提供查询，支持 Pathfin 经公网反代接入；文档补充前置代理改写 Host 或遗漏公网端口时的客户端处理方式（[`5dd49053`](https://github.com/li88iioo/MediaFlux/commit/5dd49053)、[`18a65db6`](https://github.com/li88iioo/MediaFlux/commit/18a65db6)、[`39961019`](https://github.com/li88iioo/MediaFlux/commit/39961019)）。
- 整理日志支持批量选择媒体移入光鸭回收站，并清理本次操作后变空的父目录（[`07ac0ae8`](https://github.com/li88iioo/MediaFlux/commit/07ac0ae8)）。

### Changed
- 整理日志回收成功独立显示“已删除”，不再计入整理失败；删除后的本地清理使用精准目标，避免整源 STRM 扫描（[`abd5e9f8`](https://github.com/li88iioo/MediaFlux/commit/abd5e9f8)、[`6273f5b6`](https://github.com/li88iioo/MediaFlux/commit/6273f5b6)）。
- Jellyfin 插件链路移除仅允许 12.2 的小版本限制，映射统一从 MediaFlux 同步，并补充在线安装更新清单及图标；插件安装包仍独立发布（[`6705b73f`](https://github.com/li88iioo/MediaFlux/commit/6705b73f)）。
- 下载验证通知统一读取持久化事实，日历元数据刷新不再延长旧排期事实的新鲜度（[`02879f37`](https://github.com/li88iioo/MediaFlux/commit/02879f37)、[`68e42df4`](https://github.com/li88iioo/MediaFlux/commit/68e42df4)）。

### Fixed
- 手动整理与日志身份修正尊重明确选择的 MetaTube 条目，保留选择来源和确认时的文件版本；后续 STRM 交接被拒绝时明确反馈，避免看似成功但未完成同步（[`75046a2e`](https://github.com/li88iioo/MediaFlux/commit/75046a2e)、[`d0ba006f`](https://github.com/li88iioo/MediaFlux/commit/d0ba006f)、[`d57e7aee`](https://github.com/li88iioo/MediaFlux/commit/d57e7aee)、[`12d37c1d`](https://github.com/li88iioo/MediaFlux/commit/12d37c1d)）。
- 归档名称统一等价间隔点，避免同一媒体因“·／‧”产生重复目录；电影标题与花絮标记分开判断（[`a1cd0b38`](https://github.com/li88iioo/MediaFlux/commit/a1cd0b38)、[`0b2deb8d`](https://github.com/li88iioo/MediaFlux/commit/0b2deb8d)）。
- 修复整理取消、回执结算和重启恢复：保留已完成组及操作事实，恢复送回目录的执行回执，取消贯穿空目录清理，避免重新执行已完成的写入（[`1920fcd4`](https://github.com/li88iioo/MediaFlux/commit/1920fcd4)、[`270e0858`](https://github.com/li88iioo/MediaFlux/commit/270e0858)、[`636f9ffa`](https://github.com/li88iioo/MediaFlux/commit/636f9ffa)、[`7c2d3ac2`](https://github.com/li88iioo/MediaFlux/commit/7c2d3ac2)）。
- STRM 修复元数据消费者异常后的锁释放、失败回执恢复、来源退役前旧路径清理，以及来源配置发布与文件写入并发；旧同步事实不再覆盖后到的新变化（[`cc025a7e`](https://github.com/li88iioo/MediaFlux/commit/cc025a7e)、[`0354c8da`](https://github.com/li88iioo/MediaFlux/commit/0354c8da)、[`32d41b40`](https://github.com/li88iioo/MediaFlux/commit/32d41b40)、[`87657a63`](https://github.com/li88iioo/MediaFlux/commit/87657a63)、[`4109fd42`](https://github.com/li88iioo/MediaFlux/commit/4109fd42)）。
- RSS 投递回执绑定稳定的下载请求和目标，取消后释放对应集数占用，清理时保留未决回执；重试不再错误归属到后续请求（[`25fb3304`](https://github.com/li88iioo/MediaFlux/commit/25fb3304)、[`3db9745f`](https://github.com/li88iioo/MediaFlux/commit/3db9745f)、[`358ef5dd`](https://github.com/li88iioo/MediaFlux/commit/358ef5dd)、[`419ad2c2`](https://github.com/li88iioo/MediaFlux/commit/419ad2c2)）。
- 下载已受理结果不再因展示日志失败而丢失；多目标提交等待全部回执结算，qB 去重与任务身份核验保持同一提交范围。光鸭已核验的单文件 BT 不再携带不适用的文件索引，并保留远端失败原因（[`98eac111`](https://github.com/li88iioo/MediaFlux/commit/98eac111)、[`1a3d62d8`](https://github.com/li88iioo/MediaFlux/commit/1a3d62d8)、[`669d299f`](https://github.com/li88iioo/MediaFlux/commit/669d299f)、[`6ea4d02a`](https://github.com/li88iioo/MediaFlux/commit/6ea4d02a)、[`e19faf32`](https://github.com/li88iioo/MediaFlux/commit/e19faf32)、[`df446d69`](https://github.com/li88iioo/MediaFlux/commit/df446d69)）。
- 更新爱盼资源接口，Mikan 各端点独立保留读取时间，BTBtla 首批资源采用流式读取；聚合检索保留来源受限状态，避免把不完整结果当成完整搜索（[`9f09de11`](https://github.com/li88iioo/MediaFlux/commit/9f09de11)、[`0e457e7c`](https://github.com/li88iioo/MediaFlux/commit/0e457e7c)、[`3f1944f0`](https://github.com/li88iioo/MediaFlux/commit/3f1944f0)、[`89aba6ba`](https://github.com/li88iioo/MediaFlux/commit/89aba6ba)）。
- 探索收藏状态跨卡片和缓存视图保持一致；分页失败可在原位置重试，只有结果被接纳后才提交分页与查询快照（[`5d1602c7`](https://github.com/li88iioo/MediaFlux/commit/5d1602c7)、[`a05d5ace`](https://github.com/li88iioo/MediaFlux/commit/a05d5ace)、[`f00678e0`](https://github.com/li88iioo/MediaFlux/commit/f00678e0)、[`9e4b8955`](https://github.com/li88iioo/MediaFlux/commit/9e4b8955)）。
- Agent 保留推荐过滤约束、后台结果和被限流拒绝的待确认状态；取消计划结算完成后再释放会话，持久化异常不再泄漏回合占用。Telegram 整理通知和控制确认卡保留原话题（[`d5923089`](https://github.com/li88iioo/MediaFlux/commit/d5923089)、[`46aa9420`](https://github.com/li88iioo/MediaFlux/commit/46aa9420)、[`ce017856`](https://github.com/li88iioo/MediaFlux/commit/ce017856)、[`22f0010f`](https://github.com/li88iioo/MediaFlux/commit/22f0010f)、[`7973c1dd`](https://github.com/li88iioo/MediaFlux/commit/7973c1dd)、[`8f18f9f4`](https://github.com/li88iioo/MediaFlux/commit/8f18f9f4)、[`17cfad2b`](https://github.com/li88iioo/MediaFlux/commit/17cfad2b)）。
- 整理探测、媒体库刷新和通知恢复在启动失败后继续重试，通知快照在同一写入事务内验证（[`a118bd14`](https://github.com/li88iioo/MediaFlux/commit/a118bd14)、[`c12569e0`](https://github.com/li88iioo/MediaFlux/commit/c12569e0)、[`045e0e23`](https://github.com/li88iioo/MediaFlux/commit/045e0e23)、[`bfc354dd`](https://github.com/li88iioo/MediaFlux/commit/bfc354dd)）。
- 备份只读快照正确处理特殊 SQLite 路径；非法 Unicode 签名及 CSRF 令牌不再引发服务器异常；代理编辑器异步响应绑定发起时的会话（[`96881780`](https://github.com/li88iioo/MediaFlux/commit/96881780)、[`7c98c275`](https://github.com/li88iioo/MediaFlux/commit/7c98c275)、[`bad01e10`](https://github.com/li88iioo/MediaFlux/commit/bad01e10)、[`e07df015`](https://github.com/li88iioo/MediaFlux/commit/e07df015)）。

### 升级说明
- 本版本数据库由 schema35 → 36；从 v0.1.14 升级时自动迁移 RSS 投递归属，保留下载请求、目标及历史回执关联。
- 升级前停止服务并完整备份配置、凭据和数据库；需要回退时停止服务，按离线恢复流程恢复升级前数据，不要让 schema35 程序打开 schema36 数据库。
- 云盘画质切换需要支持 MediaFlux 协议的客户端；Pathfin 需启用 STRM 原始地址播放。仅列出云盘已有档位，可能只有原画；本版不提供官方 Jellyfin Web／客户端云盘切档适配。
- Jellyfin 插件与 Pathfin 为独立安装包，不随 MediaFlux 镜像更新。旧插件手动映射应重新从 MediaFlux 同步后再启用联动。

## [0.1.14] - 2026-10-22

### Added
- 设置页新增 Tavily API Key 只读连接测试，支持测试尚未保存的输入，并区分认证失败和服务暂不可用（[`319f1887`](https://github.com/li88iioo/MediaFlux/commit/319f1887)）。
- 新增独立的本地网页正文读取及开关、超时和缓存配置；Tavily 保留搜索职责，正文读取不再依赖 Tavily Extract 或消耗搜索额度，仅抓取公开 HTTPS HTML/文本，不执行网页 JavaScript（[`1431bc85`](https://github.com/li88iioo/MediaFlux/commit/1431bc85)）。
- Agent 支持按名称、扩展名、大小与类型规则筛选光鸭目录，预览并确认后批量移动、移入回收站、平铺目录或清理本次变空的子目录；重名可保留两份，不提供覆盖模式，扫描不完整时不生成执行计划（[`654139a9`](https://github.com/li88iioo/MediaFlux/commit/654139a9)）。
- Telegram 支持话题隔离、会话模型选择与分页菜单；选择或取消后收起按钮，首条消息及后续回复使用相同话题路由（[`6d8d73a7`](https://github.com/li88iioo/MediaFlux/commit/6d8d73a7)、[`be6fb4f3`](https://github.com/li88iioo/MediaFlux/commit/be6fb4f3)、[`e545d4aa`](https://github.com/li88iioo/MediaFlux/commit/e545d4aa)）。
- Agent 增加流式显示开关、按实际媒体库范围查询最近入库、核对公网推荐与本地库存，以及整批资源预检与提交能力（[`37f56dae`](https://github.com/li88iioo/MediaFlux/commit/37f56dae)、[`be7ac825`](https://github.com/li88iioo/MediaFlux/commit/be7ac825)、[`2015239c`](https://github.com/li88iioo/MediaFlux/commit/2015239c)、[`6809bb3b`](https://github.com/li88iioo/MediaFlux/commit/6809bb3b)）。
- 整理日志支持通过预览批量修正媒体身份；RSS 增加 12/24 小时刷新间隔；探索页增加空闲隐藏的返回顶部按钮（[`781d4329`](https://github.com/li88iioo/MediaFlux/commit/781d4329)、[`1bf0632b`](https://github.com/li88iioo/MediaFlux/commit/1bf0632b)、[`4d200b93`](https://github.com/li88iioo/MediaFlux/commit/4d200b93)）。

### Changed
- Agent 按领域拆分业务与内核职责，分离回合、模型流、工具执行和终态处理；只读探索使用固定有界额度，不再靠成功读取逐次增加规划额度（[`852dbb4d`](https://github.com/li88iioo/MediaFlux/commit/852dbb4d)、[`479131ae`](https://github.com/li88iioo/MediaFlux/commit/479131ae)、[`10ec9bdd`](https://github.com/li88iioo/MediaFlux/commit/10ec9bdd)、[`68eeee23`](https://github.com/li88iioo/MediaFlux/commit/68eeee23)）。
- 整理统一单文件身份与季集规划、主文件和伴随文件事务及失败补偿；媒体推荐和偏好下沉到共享业务层，避免 Web 与 Agent 各自实现（[`3fe15878`](https://github.com/li88iioo/MediaFlux/commit/3fe15878)、[`50b1e996`](https://github.com/li88iioo/MediaFlux/commit/50b1e996)、[`1163b31c`](https://github.com/li88iioo/MediaFlux/commit/1163b31c)）。
- 首次安装收敛为 Web 初始化流程；配置校验、持久发布与运行快照使用统一事务入口（[`bc9d6e85`](https://github.com/li88iioo/MediaFlux/commit/bc9d6e85)、[`4dea3297`](https://github.com/li88iioo/MediaFlux/commit/4dea3297)）。
- RSS 条目统一分页浏览，批量全选限定当前页；媒体订阅与收藏清单支持逐页读全，按真实媒体身份解析订阅状态，不再将首批返回当作完整清单。同批订阅复用相同范围的库存快照，减少重复读取（[`5f914b97`](https://github.com/li88iioo/MediaFlux/commit/5f914b97)、[`76ac2027`](https://github.com/li88iioo/MediaFlux/commit/76ac2027)、[`0d770bc7`](https://github.com/li88iioo/MediaFlux/commit/0d770bc7)）。
- Jellyfin 插件删除媒体后，仅在云盘回收已确认时精准退役对应 STRM／元数据索引及关联任务，不再触发整个来源的扫描与媒体库刷新；本地媒体删除仍由 Jellyfin 负责（[`85258af7`](https://github.com/li88iioo/MediaFlux/commit/85258af7)）。
- 索引站独立配置，区分前端搜索和 Agent 来源权限；资源目录先完成分页再应用结果上限。公网磁力在下载接入边界补充 Tracker，移除固定片名别名推断（[`275fe24b`](https://github.com/li88iioo/MediaFlux/commit/275fe24b)、[`4c867b68`](https://github.com/li88iioo/MediaFlux/commit/4c867b68)、[`9facc6d6`](https://github.com/li88iioo/MediaFlux/commit/9facc6d6)、[`705471ba`](https://github.com/li88iioo/MediaFlux/commit/705471ba)、[`5bcba2a5`](https://github.com/li88iioo/MediaFlux/commit/5bcba2a5)）。
- 刷新 Python 3.13 发布哈希锁，升级 FastAPI、Starlette、Pydantic、HTTP 客户端及加密依赖；更新 Node/Python 基础镜像摘要，构建使用 Node 24.22.1、npm 12.2.0，并升级静态压缩、Ruff 和 Playwright 工具链（[`78c1a25e`](https://github.com/li88iioo/MediaFlux/commit/78c1a25e)、[`f3bc1a21`](https://github.com/li88iioo/MediaFlux/commit/f3bc1a21)、[`5a0bae21`](https://github.com/li88iioo/MediaFlux/commit/5a0bae21)）。
- 本地 Lucide/GSAP 更新并纳入 npm 精确锁定，测试校验分发文件与锁定包字节一致；Jellyfin 插件测试依赖更新，运行兼容目标仍保持 Jellyfin 12.2（[`5a0bae21`](https://github.com/li88iioo/MediaFlux/commit/5a0bae21)、[`765cf6dc`](https://github.com/li88iioo/MediaFlux/commit/765cf6dc)）。

### Fixed
- 按 TMDB ID 的单项存在性／剧集候选查询复用统一分页库存读取，移除未受支持的服务端等值过滤；库存未扫描完整时不再把未命中报告为不存在（[`de66b8ce`](https://github.com/li88iioo/MediaFlux/commit/de66b8ce)）。
- 媒体库存统一展开电影合集成员，不再把 Jellyfin / Emby 的界面折叠清单当作完整库存，避免推荐重复的已入库影片（[`c099e8e9`](https://github.com/li88iioo/MediaFlux/commit/c099e8e9)）。
- 连续对话按窗口剩余容量保留最近查询工具的 Schema，仍重新核验当前权限；修复能力召回把工具示例中的“排除已入库”误当成工具动作，导致推荐工具被压低的问题（[`59d4ed53`](https://github.com/li88iioo/MediaFlux/commit/59d4ed53)、[`73c65809`](https://github.com/li88iioo/MediaFlux/commit/73c65809)）。
- 修复 Agent 中断流、跨轮约束、部分查询失败和确认后后台任务的状态交接；停止、过期确认及重试保留执行身份，不把提交、检查或文件移动当作入库完成（[`077ea313`](https://github.com/li88iioo/MediaFlux/commit/077ea313)、[`fc9adc27`](https://github.com/li88iioo/MediaFlux/commit/fc9adc27)、[`0f7fae6b`](https://github.com/li88iioo/MediaFlux/commit/0f7fae6b)、[`42f2cd6c`](https://github.com/li88iioo/MediaFlux/commit/42f2cd6c)）。
- 日历查询自动补齐请求范围；缺集搜索排除明确错集/错作品候选，媒体库存支持合并集文件；推荐翻页保留来源偏移（[`e60a79c8`](https://github.com/li88iioo/MediaFlux/commit/e60a79c8)、[`b2cac42d`](https://github.com/li88iioo/MediaFlux/commit/b2cac42d)、[`6207504a`](https://github.com/li88iioo/MediaFlux/commit/6207504a)、[`24212db5`](https://github.com/li88iioo/MediaFlux/commit/24212db5)）。
- RSS 条目与下载中心重试共享状态，未处理条目优先排序，耗尽重试的提醒不丢失；电影追更预览与执行复用相同观察事实，策略和范围原子更新（[`226a2c01`](https://github.com/li88iioo/MediaFlux/commit/226a2c01)、[`ed615a3f`](https://github.com/li88iioo/MediaFlux/commit/ed615a3f)、[`eea85ed6`](https://github.com/li88iioo/MediaFlux/commit/eea85ed6)、[`33f58359`](https://github.com/li88iioo/MediaFlux/commit/33f58359)、[`47e11608`](https://github.com/li88iioo/MediaFlux/commit/47e11608)）。
- 下载提交时钟与进度更新时间分离；重启后恢复尚未开始的整理交接；显示名称补齐不改变原有下载路由（[`f2bb8467`](https://github.com/li88iioo/MediaFlux/commit/f2bb8467)、[`f52e13e1`](https://github.com/li88iioo/MediaFlux/commit/f52e13e1)、[`8fbc0d33`](https://github.com/li88iioo/MediaFlux/commit/8fbc0d33)）。
- STRM 元数据输出与刷新绑定同一冻结根目录，恢复中断后的未索引安装，保留同步期间到达的增量变化；完整性不足时不执行删除型清理（[`7e74db7d`](https://github.com/li88iioo/MediaFlux/commit/7e74db7d)、[`49b7aa4f`](https://github.com/li88iioo/MediaFlux/commit/49b7aa4f)、[`1188ccb4`](https://github.com/li88iioo/MediaFlux/commit/1188ccb4)、[`7f288bfc`](https://github.com/li88iioo/MediaFlux/commit/7f288bfc)）。
- 修复索引器取消请求后浏览器会话提前释放，以及媒体代理握手取消时连接泄漏；探索弹窗释放关闭后的结果，侧栏展开收起时文字不再换行跳变（[`9a2e9301`](https://github.com/li88iioo/MediaFlux/commit/9a2e9301)、[`cd437066`](https://github.com/li88iioo/MediaFlux/commit/cd437066)、[`ab799610`](https://github.com/li88iioo/MediaFlux/commit/ab799610)、[`daed6895`](https://github.com/li88iioo/MediaFlux/commit/daed6895)）。
- 启动与备份并发测试显式隔离首次模块导入，消除单独运行与全量套件之间的顺序依赖，保留原锁顺序与备份完整性断言（[`acb41080`](https://github.com/li88iioo/MediaFlux/commit/acb41080)）。
- 整理人工确认重新核对冻结文件与实际执行规则，避免无关配置变化误报“整理规则已变化”；排队受理持久化后再回复，迟到的受理回执不覆盖已完成结果，Telegram 确认回执按序交付（[`eb66e30c`](https://github.com/li88iioo/MediaFlux/commit/eb66e30c)、[`479762ad`](https://github.com/li88iioo/MediaFlux/commit/479762ad)）。
- NSFW 自动识别、日志重新整理与分段处理复用相同番号清洗逻辑，正确处理紧凑文件名、字幕／版本后缀和域名噪声，修复搜索能找到候选却因原文件名校验失败而无法重新整理的问题（[`1c9fe966`](https://github.com/li88iioo/MediaFlux/commit/1c9fe966)、[`3d77a09d`](https://github.com/li88iioo/MediaFlux/commit/3d77a09d)）。
- 整理日志重新执行须有当前成功预览，提交前检查日志快照及云端文件是否过期；切换记录后，旧请求或错误不再覆盖当前详情（[`0b8793e3`](https://github.com/li88iioo/MediaFlux/commit/0b8793e3)、[`d1d323a9`](https://github.com/li88iioo/MediaFlux/commit/d1d323a9)、[`a9220e33`](https://github.com/li88iioo/MediaFlux/commit/a9220e33)）。
- 整理日志清除仅作用于当前来源、状态及关键词匹配的记录，保留运行中和待确认关联记录；本地整理补齐历史分页与分批清除，清除记录不删除媒体文件（[`3f01d952`](https://github.com/li88iioo/MediaFlux/commit/3f01d952)、[`7a5d6867`](https://github.com/li88iioo/MediaFlux/commit/7a5d6867)）。
- 统一下载、订阅和通知中的后处理状态：部分目标失败、整理或 STRM 交接失败仍标为“需要处理”，不会被下载已完成掩盖；错误通知脱敏，并保留对应恢复入口（[`a4b50ce1`](https://github.com/li88iioo/MediaFlux/commit/a4b50ce1)、[`a5cb2d85`](https://github.com/li88iioo/MediaFlux/commit/a5cb2d85)、[`8a6fd2de`](https://github.com/li88iioo/MediaFlux/commit/8a6fd2de)、[`8b7409f3`](https://github.com/li88iioo/MediaFlux/commit/8b7409f3)、[`0a374cd8`](https://github.com/li88iioo/MediaFlux/commit/0a374cd8)）。
- STRM 选中重试与批量重试共用失败恢复链路；元数据手动恢复与后台安装、确认及刷新交接统一，已安装但确认中断的文件可复用，避免重复下载或排队（[`f9d872ae`](https://github.com/li88iioo/MediaFlux/commit/f9d872ae)、[`e06b5790`](https://github.com/li88iioo/MediaFlux/commit/e06b5790)）。
- RSS 刷新结果绑定同一订阅配置修订并原子提交；重试预检失败保留原下载受理回执与状态，不把已受理任务误当作未提交（[`09f30581`](https://github.com/li88iioo/MediaFlux/commit/09f30581)、[`6ae00fae`](https://github.com/li88iioo/MediaFlux/commit/6ae00fae)）。
- 分享转存的回执绑定确认时的预览、文件选择与目标目录；修改链接或切换预览后，旧异步结果不再覆盖当前状态（[`50a9f0de`](https://github.com/li88iioo/MediaFlux/commit/50a9f0de)）。
- 库存查询的降级与缺集审计保留扫描不完整状态，不将未知库存报告为未入库；自动化通知保留创建时的 Telegram 话题，局部修改每日摘要规则不再重置未修改的发送时间（[`a046ad79`](https://github.com/li88iioo/MediaFlux/commit/a046ad79)、[`e6b6918b`](https://github.com/li88iioo/MediaFlux/commit/e6b6918b)、[`c14c961f`](https://github.com/li88iioo/MediaFlux/commit/c14c961f)）。

### Security
- 更新 certifi 至 2026.10.19，并提高 aiohttp、urllib3、multidict 和 pip 的安装下限；Node 构建阶段先更新同发行版 Alpine 系统包与 CA bundle，再安装 npm 依赖，修复固定基础镜像仍携带受影响的 emSign G3 根证书及旧版 zlib 的问题（[`78c1a25e`](https://github.com/li88iioo/MediaFlux/commit/78c1a25e)、[`5a0bae21`](https://github.com/li88iioo/MediaFlux/commit/5a0bae21)）。

### Removed
- 退役未使用的 `POST /api/offline/preview`、`POST /api/offline/submit`，资源接入复用统一下载链路；离线规则查询与配置保留（[`fa894152`](https://github.com/li88iioo/MediaFlux/commit/fa894152)）。
- 移除独立依赖安全扫描工作流及其发布前置依赖，不再定时扫描或因此阻塞构建；已有依赖更新、证书修复、单元测试、浏览器测试和容器冒烟门禁保留（[`262b1e06`](https://github.com/li88iioo/MediaFlux/commit/262b1e06)）。

### 升级说明
- 本版本数据库由 schema31 → 35；从 v0.1.13 升级时自动执行连续迁移，增加下载展示名称、后台轮询索引、巡检周期身份和独立下载提交时钟，保留已有任务与历史记录。
- 升级前请停止服务，对配置、凭据和数据库进行完整离线备份；WAL 模式下不要在运行中只复制主 `.db` 文件。先确认数据卷与 STRM 输出目录的挂载和运行账户权限。
- 回退必须停止服务，并按离线恢复流程恢复升级前配置和数据库；不要直接让 schema31 程序打开 schema35 数据库。
- Web 与 Telegram 功能及外部来源仍取决于各自配置。已有历史确认、未知远端写入结果需要按当前状态复核，不应通过反复提交来验证升级。
- 自建 API 客户端需适配分页响应：`/api/rss/entries` 的响应数据改为含 `items/total/offset/limit/has_more` 的对象；`/api/subscriptions/media`、`/api/subscriptions/watchlist`、`/api/discovery/watchlist` 改为含 `items/total/has_more/next` 的对象。旧离线预览与提交接口已移除，外部脚本不能继续调用；项目内 Web、Telegram 与 Agent 调用已迁移。

### 已知限制
- 外部索引、云盘离线下载与模型服务可能超时或限流；下载受理不代表媒体已传输完成。发布组季集映射仍需可核验依据。
- 自动化通过与有界真实对话验收不等于所有模型、所有媒体和全部生产任务都已验证；未覆盖的第三方环境仍需部署后按自身配置验收。
- 依赖更新不代表镜像已无漏洞：本次核查仍发现 Debian 运行系统包及 npm/pip 内置依赖的已知公告，其中部分尚无发行方修复；安全核查需单独执行，不能据此保证规避未知零日漏洞。

## [0.1.13] - 2026-09-19

### Added
- 媒体发现新增经过核验的动漫日历，并可通过 Agent 查询；日历卡片沿用资源展示模式（[`0dbfb99`](https://github.com/li88iioo/MediaFlux/commit/0dbfb99268881ed248e4272f3c81c7113febe7d8)、[`d2318f4`](https://github.com/li88iioo/MediaFlux/commit/d2318f46d9fa313c0ea4460b3f7aa1bb27ace207)）。
- Agent 支持一次核对多部剧集的真实媒体库存与已播缺集，并将多作品资源候选带入同一批次预检和确认（[`4b5a484`](https://github.com/li88iioo/MediaFlux/commit/4b5a48460565819d5be4b7d5e4309ab48f6eb512)、[`6fdf6ef`](https://github.com/li88iioo/MediaFlux/commit/6fdf6efeb81d57f7cb767c23c5c2b5b34e6ffee4)）。
- 增加复杂季集编号的受限研究能力、统一光鸭剧集命名计划，以及复用现有识别器的文件名预览工具；研究结果仍需证据与确认（[`9b05516`](https://github.com/li88iioo/MediaFlux/commit/9b05516769a32ced0547088ec7c740f579317d5d)、[`5622798`](https://github.com/li88iioo/MediaFlux/commit/5622798f1c0f19b5dedaef59f1fd010eb4cf3fe9)、[`30bc181`](https://github.com/li88iioo/MediaFlux/commit/30bc181794b1b7281acf716f2486bb4541c43ff5)）。
- 设置页显示经实际认证核验的豆瓣 dbcl2 Cookie 状态；失败原因与未能确认状态不会被当成有效登录（[`2045ddc`](https://github.com/li88iioo/MediaFlux/commit/2045ddc569f0eae076f7b7239e1e4e84b06ff429)）。

### Changed
- 发布格式教学保留后端与 Agent 的确认后复用能力，移除独立前端教学工作台和聊天输入框教学按钮，避免重复入口（[`fb985a3`](https://github.com/li88iioo/MediaFlux/commit/fb985a32a99055094527fd829772b2171fab8e96)、[`c7689eb`](https://github.com/li88iioo/MediaFlux/commit/c7689eb3623371e54ef00252d812431c6b8af059)、[`2120fb2`](https://github.com/li88iioo/MediaFlux/commit/2120fb24bcc79574316e2c97fc107203e1c4a5f6)、[`bbb37ec`](https://github.com/li88iioo/MediaFlux/commit/bbb37ec0df330509a84c881bb619f5948f287eee)）。
- 确认按钮统一作为任务授权继续点：Web 与 Telegram 在确认后接回同一会话执行循环，并核对真实工具回执，不把“任务已启动”当成整个请求已完成（[`2284bc5`](https://github.com/li88iioo/MediaFlux/commit/2284bc5c85b5417e6df4c782885f304a3af85219)、[`f9453db`](https://github.com/li88iioo/MediaFlux/commit/f9453db6f2678f51eb18c607c43502121771452c)、[`fbed218`](https://github.com/li88iioo/MediaFlux/commit/fbed218c8a110d6f272aecd08538fbb7bcafb927)）。
- 云盘媒体清洗与目录迁移可合并进一次确认；普通移动不再重复改名，上传经同一可核验任务链路完成（[`1315f66`](https://github.com/li88iioo/MediaFlux/commit/1315f66ce687e1d15c2da17f055bbca0f1936d80)、[`634fd1b`](https://github.com/li88iioo/MediaFlux/commit/634fd1b9c9fd3ad12180182a096db362efb987d7)、[`caa9611`](https://github.com/li88iioo/MediaFlux/commit/caa96115d783511cf3456f0f47e95d9a54001b0e)）。
- 统一云盘文件变更的 STRM 联动，只校准受影响的已配置来源；空目录创建和无关目录操作不触发媒体同步，同时保留原大批量改名能力（[`76cd962`](https://github.com/li88iioo/MediaFlux/commit/76cd9622b77273a4c23d998aba7a1370f71cdc14)、[`1437e51`](https://github.com/li88iioo/MediaFlux/commit/1437e51da5ac43a93f6671d81d41017e6961d13b)）。
- 本地整理只保留持久执行链，任务与下载回执原子提交；共享目录遍历、目标批量快照和同次预览的源季集证据，减少重复解析与读取（[`d59e03e`](https://github.com/li88iioo/MediaFlux/commit/d59e03ef918e1fc1b0829a5fc4392e83700694d3)、[`4605f60`](https://github.com/li88iioo/MediaFlux/commit/4605f60680d655d0eea6028a9fecc941055ac461)、[`f1ddec2`](https://github.com/li88iioo/MediaFlux/commit/f1ddec2d3e904e231cc73a0c138907750c4f2b93)、[`812ea8d`](https://github.com/li88iioo/MediaFlux/commit/812ea8d7f5334f21dbac41b320f7a7e4c760c3cf)、[`609900a`](https://github.com/li88iioo/MediaFlux/commit/609900a6442e976315c3dbbc68900df161faa281)）。
- 收敛数据库启动恢复与迁移入口、RSS 下载提交、订阅搜索诊断、播放 GET/HEAD 入口和 STRM 索引清理；移除未使用的发布/维护及通知续租接口（[`d06515f`](https://github.com/li88iioo/MediaFlux/commit/d06515f38d0ce72edd3b5a5e3f4ad5d12dab15cf)、[`d67968b`](https://github.com/li88iioo/MediaFlux/commit/d67968b287fb827ac3bd706e3e22d662ea1f88b7)、[`104f81b`](https://github.com/li88iioo/MediaFlux/commit/104f81b3e734d434fe5287d6ec70416f95756bbb)、[`73dfbd5`](https://github.com/li88iioo/MediaFlux/commit/73dfbd521b2bb3a2cffc638dc3b6bee50dc1a084)、[`cb3b1c8`](https://github.com/li88iioo/MediaFlux/commit/cb3b1c8f8af32f5c3a28ecb1a548b377e51fe180)、[`8412e11`](https://github.com/li88iioo/MediaFlux/commit/8412e119894d50615c5619b08cc91403c783b71f)、[`a904ad9`](https://github.com/li88iioo/MediaFlux/commit/a904ad9f30e975eceb1bd2a09766fe302ed942a8)、[`310ff76`](https://github.com/li88iioo/MediaFlux/commit/310ff763bc7743002888e3f750e08a527c54bab7)）。

### Fixed
- 修复 Telegram Agent 操作完成或取消后按钮残留、已处理批次重新回到选择界面，以及编辑失败后旧卡未收尾；多阶段确认同步转交候选状态，无进度事件时也保留群组话题（[`a6dc430`](https://github.com/li88iioo/MediaFlux/commit/a6dc430408832dbc9aea2c35ff3b97845976f011)）。
- 普通资源搜索先供 Agent 核对，不再把旧集或无关命中自动显示为资源卡；只展示明确筛选的候选，同一轮新结果替换旧卡、空结果移除旧卡。会话恢复按当前候选关联确认计划，并保留筛选后的下载确认链路（[`a5f7f80`](https://github.com/li88iioo/MediaFlux/commit/a5f7f809939d20dc76c30e957cb5784eae7da9ec)）。
- 修复 GM-Team 等分类双语标题、发布组季号/总集号、特别篇和正片混组；数字前缀番号、显式 TV 映射尾集号和人工季集映射不再被错误清洗或覆盖（[`e06f049`](https://github.com/li88iioo/MediaFlux/commit/e06f0492c9f31c0cdd0cbf665495ddbb4391cd27)、[`4ac9ba9`](https://github.com/li88iioo/MediaFlux/commit/4ac9ba9a10155444f983cdba601a77609d4151f7)、[`74e6a93`](https://github.com/li88iioo/MediaFlux/commit/74e6a93b654707be5601fbaa45a1200500af3aea)、[`5b69c9e`](https://github.com/li88iioo/MediaFlux/commit/5b69c9e832c5d152acb1d22960226444ec9eadb8)、[`868751f`](https://github.com/li88iioo/MediaFlux/commit/868751fcbfdcb89e960d101bcee73cf9ac1718e1)、[`17f69d0`](https://github.com/li88iioo/MediaFlux/commit/17f69d01395b25df1aa8857562f58b425b5a2cae)）。
- 修复 Agent 工具预算耗尽或取消时丢失已完成查询结果、不完整模型流被当作成功，以及续问使用陈旧库存；保留逐项事实和未核实结论（[`8e7ee55`](https://github.com/li88iioo/MediaFlux/commit/8e7ee559e11e25c2af375b20a9d10fa87d6c9ed8)、[`dd577f8`](https://github.com/li88iioo/MediaFlux/commit/dd577f8ff58c5537f8b7505122819dc0627fe928)、[`d5c01fb`](https://github.com/li88iioo/MediaFlux/commit/d5c01fb72af309c1c97e4abebec72c6951b21efc)、[`df94759`](https://github.com/li88iioo/MediaFlux/commit/df947591ef0641068d4ddaa63d7ad258dfbadf08)、[`4b8ab5e`](https://github.com/li88iioo/MediaFlux/commit/4b8ab5e99319150011299bcc97fc9139d65a7470)、[`8ea6124`](https://github.com/li88iioo/MediaFlux/commit/8ea6124eb5a55914b76e19599bcb673ac2497856)）。
- 只有经核验覆盖缺集的候选才生成补缺推荐；无可操作结果时不再显示空的资源批选卡片和下载按钮，正常文本回答仍保留（[`6438b87`](https://github.com/li88iioo/MediaFlux/commit/6438b8717fcfee4a40b4b1d21cd93d4d3a99e367)、[`bbf327c`](https://github.com/li88iioo/MediaFlux/commit/bbf327ccef03c3b2331a0740dcf3db7dd480aa0c)）。
- Telegram 提交失败与 Agent 后续状态查询共享安全失败原因，例如光鸭“文件违规”；部分成功、结果未知、重复提交和普通等待状态保持区分（[`e58fe73`](https://github.com/li88iioo/MediaFlux/commit/e58fe73f273e98f638c5d3750ae465f5bf531423)、[`16abdaa`](https://github.com/li88iioo/MediaFlux/commit/16abdaa87b5058750c83c0d60f6c2149ac4ebe56)、[`3ef98d6`](https://github.com/li88iioo/MediaFlux/commit/3ef98d692cff928186165af4a728dd83f77c7d60)、[`e67ceca`](https://github.com/li88iioo/MediaFlux/commit/e67ceca764c1e7d703471a1a5dea7853bae82f78)）。
- 修复确认过期/拒绝反馈、流结束与已提交终态不一致，以及 Telegram 取消任务后执行容量未释放；继续保留人工审查窗口（[`9e6191e`](https://github.com/li88iioo/MediaFlux/commit/9e6191eb6f320250e902209d09dd95d14f4ef7ce)、[`2f84dfe`](https://github.com/li88iioo/MediaFlux/commit/2f84dfefd32ee4620f9cc2245537dab2313c9994)、[`af23751`](https://github.com/li88iioo/MediaFlux/commit/af237518ce6deac2f21ac083e42afce677947e04)、[`e3bc984`](https://github.com/li88iioo/MediaFlux/commit/e3bc984a571a7c261e478e9953064e017f589535)）。
- 云盘进程中断后，持久队列与签名变更计划统一收束终态；未知远端结果保留人工核对，不重放已完成操作、不让计划永久占用活动配额（[`1437e51`](https://github.com/li88iioo/MediaFlux/commit/1437e51da5ac43a93f6671d81d41017e6961d13b)）。
- RSS 不再接受实际无法调度的新 cron 配置，明确提示使用刷新间隔；历史 cron 数据和旧客户端原值回传无损保留（[`c8fe8c4`](https://github.com/li88iioo/MediaFlux/commit/c8fe8c45c92c0a71a51bd6955fb9adfda5626854)）。
- 修复媒体发现的标题查询与 TMDB 默认季选择、整理异常跳转和 qB 重试，并改善设置说明提示、滚动反馈和页面对齐（[`537dcd4`](https://github.com/li88iioo/MediaFlux/commit/537dcd48eb0bb7e83630de8f0e1214bceb1f7254)、[`7951fb3`](https://github.com/li88iioo/MediaFlux/commit/7951fb3ab74a64aad78d6f33fbe9a91fd489e0ac)、[`809611d`](https://github.com/li88iioo/MediaFlux/commit/809611d83dd85c1090d3396aef5a0715bc255a9e)、[`1a8cecf`](https://github.com/li88iioo/MediaFlux/commit/1a8cecfe46f85810a74d8ffbf05a169d23bf4f57)）。

### 升级说明
- 已经安装 v0.1.13 的用户也需要重新拉取镜像并重建容器，以获得本版全部修复；本次更新不新增数据库迁移。
- 本版本数据库由 schema29 → 31；从 v0.1.12 升级时会自动执行连续迁移，保留已有配置、任务、历史记录与发布格式规则。
- 升级前请停止服务，对数据目录和 SQLite 数据库进行完整离线备份；使用 WAL 模式时不要在运行中仅复制主 `.db` 文件。
- 回退旧版本前必须停止服务，并按离线恢复流程恢复升级前数据库；不要直接让 schema29 程序打开 schema31 数据库。
- 现有云盘大批量改名计划及通用文件变更计划保持各自原有签名与执行约束；中断且远端结果未知的任务需要人工核对，不自动重放写操作。

### 已知限制
- 发布组季集映射、TMDB 已播记录与索引站资源发布时间可能不一致；无足够证据时保持待核对，不承诺任意发布格式自动识别。
- 新增 STRM 来源范围控制不会把任意移动/删除简化为仅更新新路径；需要校准时仍对受影响来源执行完整核对，避免遗留旧路径。

## [0.1.12] - 2026-09-09

### Added
- Agent 新增动态能力发现、元数据队列控制与光鸭 SDK 管理能力，继续通过明确确认后执行写操作（[`6ee901c`](https://github.com/li88iioo/MediaFlux/commit/6ee901c5b1f3f03ae862e12a4284b01f6e3c1be3)、[`1bca74e`](https://github.com/li88iioo/MediaFlux/commit/1bca74ea5b04aa8488ed6ae8fb411eadc77afcb8)）。
- 补齐媒体发现、媒体库识别与可信详情链接，以及播放活动、偏好和媒体自动化流程（[`ae47eae`](https://github.com/li88iioo/MediaFlux/commit/ae47eaef4df0cc87ad888456da83f61df30961e9)、[`72459a4`](https://github.com/li88iioo/MediaFlux/commit/72459a411d7f7eee8203d5feeb99259d2d9b30b5)、[`8e9bc79`](https://github.com/li88iioo/MediaFlux/commit/8e9bc7944dae32a69ebc64a14800b1566d545bc1)）。
- 新增 Agent 辅助整理识别复核；光鸭敏感内容净标题复核为可选功能，默认关闭（[`1bb1a23`](https://github.com/li88iioo/MediaFlux/commit/1bb1a232a80f172b790f017350f1e21872139352)、[`4077240`](https://github.com/li88iioo/MediaFlux/commit/4077240eb71b6755ead0eab63e83613d0c45b051)）。
- Agent 增加草稿保留、候选预览和会话操作，并统一 Web 与 Telegram 的资源批量选择与确认（[`6122fba`](https://github.com/li88iioo/MediaFlux/commit/6122fba78a2dfa83ec1543d5aac03c15813211d2)、[`2389e0c`](https://github.com/li88iioo/MediaFlux/commit/2389e0c57a5d4dc53669ecaed85cc932f0499932)）。
- 新增下载隔离目录对账与整理后的空目录清理生命周期；活动任务、永久来源、归档根及无法核验的删除状态继续保留，不凭目录名称推断清理授权（[`99ba542`](https://github.com/li88iioo/MediaFlux/commit/99ba5424979a9bb37f9eb00af4b6de8786b9ac3b)）。

### Changed
- Agent 控制面统一为模型驱动的事件式 Kernel，Web 与 Telegram 共用会话、私有工具引用、确认和可信结果合同；移除退役的旧控制面执行入口（[`d0000fa`](https://github.com/li88iioo/MediaFlux/commit/d0000fa439bfb7760e18861cbd04068a3c526a42)、[`4848e8f`](https://github.com/li88iioo/MediaFlux/commit/4848e8f86b1d2352f1525e38f84a26fd208e58b1)、[`f2c3884`](https://github.com/li88iioo/MediaFlux/commit/f2c3884bef78f084d8615b8ee69cd67d35eb54a2)、[`c127cf3`](https://github.com/li88iioo/MediaFlux/commit/c127cf3a82f147772c680b501e7b14e68d15b8c1)、[`e6a49c4`](https://github.com/li88iioo/MediaFlux/commit/e6a49c451d0076647cee4e47b1e0e8364383c440)）。
- 收敛数据库与整理职责、操作历史仓储和云端补偿实现；优化独立目录刷新、长季冲突预演、STRM 重复读取及备份资源占用（[`8929644`](https://github.com/li88iioo/MediaFlux/commit/89296443e86cd5409e6726b858f0817ed10f1588)、[`a0310b7`](https://github.com/li88iioo/MediaFlux/commit/a0310b777bf1a21269cfe3fe034362ef9de1e876)、[`20dee8d`](https://github.com/li88iioo/MediaFlux/commit/20dee8d4b2c20f177b0b67e3d926a1a6efa9a264)）。
- 调整 Agent 输入区提示和移动端控件对齐，保持桌面与移动端交互布局一致（[`bead475`](https://github.com/li88iioo/MediaFlux/commit/bead475688050efaa4a72d416a8752d67da36f42)、[`75e9f82`](https://github.com/li88iioo/MediaFlux/commit/75e9f82d1a5714f1b2ebdf7a75001671da3eebf3)）。
- 统一媒体库存、播放、RSS 和本地绑定的批量读取快照；活动检索保持全局排序，展示分页不再截断成员及关联任务的完整状态汇总（[`fce3ba9`](https://github.com/li88iioo/MediaFlux/commit/fce3ba98781df0c2b45e4d09c4f4dd156c5f624c)、[`0a42208`](https://github.com/li88iioo/MediaFlux/commit/0a42208c91b078c9e47721c0588bd8facf144fa3)、[`5524165`](https://github.com/li88iioo/MediaFlux/commit/552416500b13573dba10751c0924d988be170c8f)、[`79c3dc3`](https://github.com/li88iioo/MediaFlux/commit/79c3dc36576f21204facab455e3044106b9fa488)、[`d2563be`](https://github.com/li88iioo/MediaFlux/commit/d2563be91b23b42c731c4b377ebb4b8d2d5b6d04)、[`35514e1`](https://github.com/li88iioo/MediaFlux/commit/35514e1b459c142be6a093370bc2ea1f6b7ac079)）。
- 将发布检查要求归并到正式部署与开发指南，持续保留运行时锁定、真实浏览器和逐架构镜像验收门禁（[`7397a80`](https://github.com/li88iioo/MediaFlux/commit/7397a807b633464a31618ef0ac34879ee5fe8c6e)）。

### Fixed
- 修复 qBittorrent 重提任务时的身份保持、本地路径映射推导，以及下载完成到整理/媒体刷新之间的交接竞态（[`071a425`](https://github.com/li88iioo/MediaFlux/commit/071a4255a8b9d7444c1701b02ec291c576bccd05)、[`e6a0b26`](https://github.com/li88iioo/MediaFlux/commit/e6a0b260bf6da6d4849d7326aa4c8ea4d56f5413)、[`53c84cf`](https://github.com/li88iioo/MediaFlux/commit/53c84cf28bf76666897d64e6f7aba374eec0882f)）。
- 修复光鸭种子上传、BT 任务分发和批次结果判定，统一种子解码；不完整离线分页不再被当作任务已消失（[`e34921a`](https://github.com/li88iioo/MediaFlux/commit/e34921a17fe84b32a20bf75edfc3121539a15902)、[`2893b56`](https://github.com/li88iioo/MediaFlux/commit/2893b56cd18cc713bfa6ee925151e14a1ed1cee6)、[`20dee8d`](https://github.com/li88iioo/MediaFlux/commit/20dee8d4b2c20f177b0b67e3d926a1a6efa9a264)）。
- 修复云端文件移动后 STRM 路径收敛、进程中断恢复和可信旧指针清理；刷新未定位或交接暂不可用时保留持久意图（[`a47855f`](https://github.com/li88iioo/MediaFlux/commit/a47855fd8da7908f566a7f8e4824d50c617b58d6)、[`a0310b7`](https://github.com/li88iioo/MediaFlux/commit/a0310b777bf1a21269cfe3fe034362ef9de1e876)、[`20dee8d`](https://github.com/li88iioo/MediaFlux/commit/20dee8d4b2c20f177b0b67e3d926a1a6efa9a264)）。
- 纠偏回退同步恢复媒体身份、季集和成员目标；后台规格改名提交后仅重试收尾，不重复探测，也不覆盖后续人工纠偏的有效快照（[`20dee8d`](https://github.com/li88iioo/MediaFlux/commit/20dee8d4b2c20f177b0b67e3d926a1a6efa9a264)、[`03a2f9c`](https://github.com/li88iioo/MediaFlux/commit/03a2f9c1bed4a07cc1656a357ffcf8a2864e04c5)）。
- 修复混合季动画识别与人工季集持久化，以及长标题、扩展名、自定义模板、宽季集编号和版本标签的命名截断（[`fed3920`](https://github.com/li88iioo/MediaFlux/commit/fed3920643da212d07e2f628e14b3b93963b14e1)、[`20dee8d`](https://github.com/li88iioo/MediaFlux/commit/20dee8d4b2c20f177b0b67e3d926a1a6efa9a264)、[`03a2f9c`](https://github.com/li88iioo/MediaFlux/commit/03a2f9c1bed4a07cc1656a357ffcf8a2864e04c5)）。
- 修复光鸭 Token 更换后的刮削服务恢复，收敛确认、客户端、线程和跨事件循环运行时的生命周期边界（[`1136404`](https://github.com/li88iioo/MediaFlux/commit/11364048c7263ef6324e5b01861c4f04d5a8cb3b)、[`613ee7f`](https://github.com/li88iioo/MediaFlux/commit/613ee7f29f13b705f9618dea8aa64ef81e0f91a8)、[`2893b56`](https://github.com/li88iioo/MediaFlux/commit/2893b56cd18cc713bfa6ee925151e14a1ed1cee6)）。
- Telegram 投递异常不再覆盖已经受理的下载或已确认业务结果；旧票据的取消和终态回写不再清除较新的待确认计划（[`20dee8d`](https://github.com/li88iioo/MediaFlux/commit/20dee8d4b2c20f177b0b67e3d926a1a6efa9a264)、[`a0310b7`](https://github.com/li88iioo/MediaFlux/commit/a0310b777bf1a21269cfe3fe034362ef9de1e876)）。
- 修复 Agent 页面恢复时的对话布局跳动与整理规则锚点页签刷新闪现；适配 Telegram 消息选项并隔离进度投递失败（[`92f6e76`](https://github.com/li88iioo/MediaFlux/commit/92f6e76d4c54e1e54f876d4ca4072b49f24f346f)、[`778cc8a`](https://github.com/li88iioo/MediaFlux/commit/778cc8a53c72a28bdf2219617a549d95fec413e5)、[`f5f0f9a`](https://github.com/li88iioo/MediaFlux/commit/f5f0f9a850a4bbf39e32243c0c83bc418cb19406)）。
- 修复光鸭单文件 BT 清单、磁力任务恢复和 qBittorrent 移除意图丢失；收敛云端读写校验、任务分发、下载状态与混合发布标题处理（[`5acb84e`](https://github.com/li88iioo/MediaFlux/commit/5acb84e5faf9c106b23d58c78de609d4e8968630)、[`580e359`](https://github.com/li88iioo/MediaFlux/commit/580e3597e9d236e7c97e85761a4c01efa6507f58)、[`437f688`](https://github.com/li88iioo/MediaFlux/commit/437f68836b37da4403acd33f8911d0baac3ca1b5)、[`7068c54`](https://github.com/li88iioo/MediaFlux/commit/7068c54f6736a84ef07fca3131c20aed08441ca4)、[`0ab30fd`](https://github.com/li88iioo/MediaFlux/commit/0ab30fd412f6f1a7d140927c439931402dcc0739)）。
- 修复任务重试、索引器启动、RSS 公平调度与退出交接中的恢复缺口；保留已确认的重试意图，隔离单项读取和通知失败，不让旧执行者覆盖新一代结果（[`f098013`](https://github.com/li88iioo/MediaFlux/commit/f09801353948a897b905eb66e0bde7dbb201f459)、[`2325b25`](https://github.com/li88iioo/MediaFlux/commit/2325b255c2f7399ffa8426830ddc0eab2a487d2b)、[`ddbdb69`](https://github.com/li88iioo/MediaFlux/commit/ddbdb69d79adcba28231935c83e434a5bf25ce1b)、[`fce3ba9`](https://github.com/li88iioo/MediaFlux/commit/fce3ba98781df0c2b45e4d09c4f4dd156c5f624c)、[`0a42208`](https://github.com/li88iioo/MediaFlux/commit/0a42208c91b078c9e47721c0588bd8facf144fa3)、[`07de969`](https://github.com/li88iioo/MediaFlux/commit/07de969eda211f05300bfb6552b90ec59b34794f)）。
- 修复媒体刷新启停交接、GCID 清单校验与重试持久化、通知规则重新确认及异常自动化规则阻塞；播放和活动读取保持一致快照，完整传播关联任务状态（[`5524165`](https://github.com/li88iioo/MediaFlux/commit/552416500b13573dba10751c0924d988be170c8f)、[`aa5931c`](https://github.com/li88iioo/MediaFlux/commit/aa5931c9a8293c400b22acd26ba13536b4aea77f)、[`79c3dc3`](https://github.com/li88iioo/MediaFlux/commit/79c3dc36576f21204facab455e3044106b9fa488)、[`d2563be`](https://github.com/li88iioo/MediaFlux/commit/d2563be91b23b42c731c4b377ebb4b8d2d5b6d04)、[`35514e1`](https://github.com/li88iioo/MediaFlux/commit/35514e1b459c142be6a093370bc2ea1f6b7ac079)）。
- 修复首次发布附件上传中断后无法安全续传的问题：先准备并校验同候选草稿附件，再提升镜像标签和公开 Release；已公开且完整的同候选版本幂等保留，未知或不一致的发布状态拒绝接管（[`790f939`](https://github.com/li88iioo/MediaFlux/commit/790f939c3369754e7697dc5f2564e998060bfe90)）。
- 修复本地发布失败后的安全恢复，保留最后一份媒体副本与重试能力；RSS 处理仍按预算提交，但待处理统计覆盖完整积压队列（[`98afda4`](https://github.com/li88iioo/MediaFlux/commit/98afda4f08592da079b966a20a91a1283ac560fc)）。
- 修复分享转存受理、执行认领与完成交接的所有权竞态；取消、恢复或新任务已接管后，旧回调不再覆盖当前结果或重复提交云端写入（[`618896d`](https://github.com/li88iioo/MediaFlux/commit/618896d45009c16aa391b429e427b7fa90e7917d)）。
- 原子取消尚未执行的下载并及时释放准入占用；统一即时、启动及批量恢复的准入投影，避免旧代次、并行恢复或遗留状态覆盖有效任务（[`a5cae38`](https://github.com/li88iioo/MediaFlux/commit/a5cae380c315e581a4acc67b713657a443dd1db2)、[`69096bd`](https://github.com/li88iioo/MediaFlux/commit/69096bd8e7d583ea54f8ced07044bce7e4341ff0)、[`146aba6`](https://github.com/li88iioo/MediaFlux/commit/146aba661afb5ba217b95d48816026880c598f32)、[`0fc92da`](https://github.com/li88iioo/MediaFlux/commit/0fc92daba77101c2eca6bcf3f258e27d9ffd3a09)）。
- 保持下载重试、HTTP 种子别名与后处理任务身份一致；为资源认领和 STRM 刷新增加持久所有权约束，拒绝过期执行者释放或覆盖新任务（[`b43b23f`](https://github.com/li88iioo/MediaFlux/commit/b43b23f08ca1e8bfcf0ba77c2e168cce2d7a1df8)、[`813bc38`](https://github.com/li88iioo/MediaFlux/commit/813bc38851eb549f4b0762d84d6a286cf90884f5)）。
- 历史 STRM 清理在删除前持久化恢复证据；保留元数据完成结果与索引扫描所有权，失败重试及交错执行仅清理自身租约对应的证据（[`a614fb9`](https://github.com/li88iioo/MediaFlux/commit/a614fb90a0892f44ad8ec0662d923f42f74b9b5c)、[`d9e5d4b`](https://github.com/li88iioo/MediaFlux/commit/d9e5d4bdb30c211816cce1c8272d0b340464f3f5)、[`223f7a3`](https://github.com/li88iioo/MediaFlux/commit/223f7a358053aa420d7943f6e807ecd2881b6ed9)、[`102fac3`](https://github.com/li88iioo/MediaFlux/commit/102fac316c13c4e8077f1fbcb6507449dfb58b1a)）。
- RSS 的 HTTP 种子链接按 BT 资源处理，并保留可用回退地址；HTTP 传输继续使用受校验的固定地址、主机身份与隔离生命周期（[`8531720`](https://github.com/li88iioo/MediaFlux/commit/853172082e624f2f1a8bdce694e95cfaf439ff05)、[`92c17a1`](https://github.com/li88iioo/MediaFlux/commit/92c17a172a9c965cad12606672ae8a2b08065d70)）。
- 修复 RSS 目录选择双弹窗的层级和窄屏布局、订阅表单按钮与反馈跳动，以及迟到的刮削响应污染新编辑会话的问题（[`8531720`](https://github.com/li88iioo/MediaFlux/commit/853172082e624f2f1a8bdce694e95cfaf439ff05)、[`5783e89`](https://github.com/li88iioo/MediaFlux/commit/5783e89d50cc789cb4223aa11133b4f5f7e3ab5d)、[`a6c7646`](https://github.com/li88iioo/MediaFlux/commit/a6c7646464c75cceaabfd27df6ee06ac89d0f5de)）。
- 修复 BTBtla 正常零结果页面被误报为站点不可用，以及 Mikan 原始超时/连接异常未尝试已注册备站的问题；保留挑战页拒绝、安全异常和取消语义，不延长总超时预算（[`99aa076`](https://github.com/li88iioo/MediaFlux/commit/99aa0763b6539d083621ea632cf2a71570a3e9e7)）。

- 修复动态 HTTP 种子先提交 qB、再补充光鸭目标时丢失已校验内容的问题；复用原种子且不重复提交 qB，已有 qB 内容身份不一致、取消或竞争认领时拒绝不安全补写（[`68a946a`](https://github.com/li88iioo/MediaFlux/commit/68a946acbfd729ab89e08b7f206e887e3f58a5c4)、[`710f53d`](https://github.com/li88iioo/MediaFlux/commit/710f53d06cf37a496d41480578fa194f2466668c)）。

### 升级说明
- 从 v0.1.11 升级的完整数据库跨度为 **schema20→29**，不是仅 28→29。首次启动会先创建迁移前备份，再连续迁移；升级前仍应保留完整数据目录和可用备份。
- Agent 已切换到新的 Kernel：旧 Agent 对话不会自动出现在新历史列表中，旧 Agent 待确认操作请重新发起并确认。旧表或备份保留不代表旧会话可在新界面续接，系统不会自动执行旧待确认操作；这不等于所有传统 Telegram 确认票据统一失效。
- 旧整理操作没有业务前像时，只能明确回退已知文件位置与名称，历史媒体身份需人工核验。云端状态不可读或补偿无法确认时停止自动写入，不猜测成功。
- 回退旧版本前必须停止服务，并按离线恢复流程恢复升级前数据库；不要直接让 schema20 程序打开 schema29 数据库。

### 已知限制
- 资源索引仍受上游限流、JavaScript 验证和慢响应影响。Mikan 网络回退修复不代表热门大页面已恢复；1lou 新 HTTPS 搜索入口仍待核验，本版不放宽 HTTPS 安全限制或宣称所有站点恢复。

## [0.1.11] - 2026-09-03

### Added
- 整理规划器现可按独立媒体组动态并行执行识别与媒体探测，并通过有序单写阶段统一处理冲突、移动、STRM 与通知；单个大目录及多个来源目录均可共享空闲 Worker，同时保留停止、快照校验和确定性提交边界（[`71f7c54`](https://github.com/li88iioo/MediaFlux/commit/71f7c54)、[`288224b`](https://github.com/li88iioo/MediaFlux/commit/288224b)）。
- Media Agent 新增统一 Provider 能力网关、服务端实时读取、一次性人工确认和受控写入执行，光鸭、qBittorrent、媒体服务器、资源下载与订阅动作收敛到同一计划/票据/审计模型（[`d36ddbe`](https://github.com/li88iioo/MediaFlux/commit/d36ddbe)、[`f66ba0c`](https://github.com/li88iioo/MediaFlux/commit/f66ba0c)、[`f2b812c`](https://github.com/li88iioo/MediaFlux/commit/f2b812c)、[`f9bc28c`](https://github.com/li88iioo/MediaFlux/commit/f9bc28c)、[`40a9fe2`](https://github.com/li88iioo/MediaFlux/commit/40a9fe2)、[`670b84c`](https://github.com/li88iioo/MediaFlux/commit/670b84c)）。
- 剧集识别新增绝对集号自动换季与 TMDB 季界刷新重算，支持被发布组错误标成单季的跨季长篇动画，并补齐短尾越界集号的保守识别与人工确认边界（[`2e6f8c0`](https://github.com/li88iioo/MediaFlux/commit/2e6f8c0)、[`ca150ec`](https://github.com/li88iioo/MediaFlux/commit/ca150ec)）。
- 媒体订阅支持按周检查周期，并继续沿用统一资源检索、下载请求与整理联动链路（[`84909c6`](https://github.com/li88iioo/MediaFlux/commit/84909c6)）。

### Changed
- Agent 的资源摄取、确认后动作、订阅与下载流程统一到单一执行链路，移除残留旧工具入口，并加强真实用户意图、上下文续接、Provider/RSS 实时状态与失效计划处理；配套确认契约测试已同步收口（[`3a4f444`](https://github.com/li88iioo/MediaFlux/commit/3a4f444)、[`886aee1`](https://github.com/li88iioo/MediaFlux/commit/886aee1)、[`43bb4a6`](https://github.com/li88iioo/MediaFlux/commit/43bb4a6)、[`6a98b5c`](https://github.com/li88iioo/MediaFlux/commit/6a98b5c)、[`de4be27`](https://github.com/li88iioo/MediaFlux/commit/de4be27)、[`d2fc3cb`](https://github.com/li88iioo/MediaFlux/commit/d2fc3cb)）。
- RSS 的 qB/光鸭下载认领、幂等、完成跟踪与本地整理联动改用统一下载请求生命周期，旧专用协调表在数据库升级时保守迁移后退休（[`aac8b5b`](https://github.com/li88iioo/MediaFlux/commit/aac8b5b)）。
- 运行时权威路径、Telegram 通知、STRM 媒体库刷新交接、TMDB 并发缓存和静态资源版本统一收口；客户端关闭、发现缓存和媒体刷新状态均改为有界、可恢复实现（[`998118e`](https://github.com/li88iioo/MediaFlux/commit/998118e)、[`9ba155b`](https://github.com/li88iioo/MediaFlux/commit/9ba155b)、[`3bc3bfd`](https://github.com/li88iioo/MediaFlux/commit/3bc3bfd)）。
- 资源检索把同一剧集的单集结果聚合为剧集卡并延迟加载海报；订阅、下载和整理页刷新保留稳定占位，媒体卡元数据避免重复显示年份（[`ed0bbc6`](https://github.com/li88iioo/MediaFlux/commit/ed0bbc6)、[`f5ebf4a`](https://github.com/li88iioo/MediaFlux/commit/f5ebf4a)、[`368dbb1`](https://github.com/li88iioo/MediaFlux/commit/368dbb1)）。
- Agent 人工确认卡统一视觉层级、状态提示与移动端布局；README 与维护文档同步到当前功能和部署契约（[`3e88347`](https://github.com/li88iioo/MediaFlux/commit/3e88347)、[`03bd551`](https://github.com/li88iioo/MediaFlux/commit/03bd551)）。

### Fixed
- 修复整理候选完成后汇总卡不更新、旧确认状态覆盖新状态、人工确认终态重复或过早结束输入提示，以及 Telegram 分享转存与普通网页链接分流错误（[`025979a`](https://github.com/li88iioo/MediaFlux/commit/025979a)、[`316fc3f`](https://github.com/li88iioo/MediaFlux/commit/316fc3f)、[`9eb5162`](https://github.com/li88iioo/MediaFlux/commit/9eb5162)）。
- 修复升级与发布冒烟中的预期 ready 重试噪声、pytest 收集边界和 STRM 等待者隔离，保持普通 `main` 验证与正式标签多架构发布契约一致（[`339e833`](https://github.com/li88iioo/MediaFlux/commit/339e833)、[`b617ba2`](https://github.com/li88iioo/MediaFlux/commit/b617ba2)、[`0c140aa`](https://github.com/li88iioo/MediaFlux/commit/0c140aa)）。
- 移除已并入正式业务页面的残留工具 UI，并修复整理页加载闪变与旧入口误导（[`a06c773`](https://github.com/li88iioo/MediaFlux/commit/a06c773)）。
- 统一媒体探索、资源站搜索与 Agent LLM 的未配置默认值，修复 Agent 状态、页面守卫和真实执行路径不一致，以及目标配置已满足时仍要求确认的问题（[`cd911b8`](https://github.com/li88iioo/MediaFlux/commit/cd911b8)）。
- 修复并行整理部分来源失败时成功来源不再执行 STRM/媒体库后处理的问题；任务现保留成功结果并以部分完成收口，同时避免把内部异常暴露到公开状态（[`cd911b8`](https://github.com/li88iioo/MediaFlux/commit/cd911b8)）。
- 修复旧 RSS claim 合并到既有统一下载请求时丢失后端完成态、可能重复提交下载的问题，并确保光鸭整理预览始终释放客户端资源（[`cd911b8`](https://github.com/li88iioo/MediaFlux/commit/cd911b8)）。
- 修正 Docker 构建信息的时间戳、平台和包类型来源，使正式镜像元数据可复现且与发布文档、系列标签说明一致（[`4a7aa0e`](https://github.com/li88iioo/MediaFlux/commit/4a7aa0e)）。

## [0.1.10] - 2026-08-30

### Added
- 新增按来源目录启用的成人内容整理链路：仅处理显式指定的 NSFW 来源，支持番号清洗、MetaTube 多来源候选归并、同番号分段文件、Telegram/Web 人工确认，以及无元数据时“清洗标题后入库”的安全兜底；归档位置继续复用媒体库页面配置的真实分类目录（[`d7f12ab`](https://github.com/li88iioo/MediaFlux/commit/d7f12ab)）。
- 新增统一 Telegram 通知中心与线程快照，整理、STRM、下载、RSS、Agent 等长任务可共享可恢复的消息身份、修订号和投递结果，并等待卡片更新完成后再结束输入状态（[`bb3f99c`](https://github.com/li88iioo/MediaFlux/commit/bb3f99c)、[`a7ca872`](https://github.com/li88iioo/MediaFlux/commit/a7ca872)）。

### Changed
- Telegram 媒体通知统一为紧凑、分段且信息完整的富文本布局，恢复入库媒体、文件、STRM 与媒体库刷新明细，同时减少重复终态与链路噪声（[`8763dd2`](https://github.com/li88iioo/MediaFlux/commit/8763dd2)、[`bb3f99c`](https://github.com/li88iioo/MediaFlux/commit/bb3f99c)）。
- 运行时生命周期统一采用关闭门控、在途任务排空和有界缓存；Web 刷新保留旧数据并呈现部分失败，整理轮询改为可见性感知重试，订阅媒体库映射改为批量读取，Docker 非 root 数据目录仅在首次或显式请求时递归迁移权限；后续可靠性收敛进一步补齐数据库隔离、异步资源回收、任务恢复、媒体反代与下载边界以及刷新态稳定性（[`cb4957f`](https://github.com/li88iioo/MediaFlux/commit/cb4957f)、[`0b9bd05`](https://github.com/li88iioo/MediaFlux/commit/0b9bd05)、[`b20da18`](https://github.com/li88iioo/MediaFlux/commit/b20da18)、[`c604b4a`](https://github.com/li88iioo/MediaFlux/commit/c604b4a)、[`c3c8a8d`](https://github.com/li88iioo/MediaFlux/commit/c3c8a8d)、[`5f2a987`](https://github.com/li88iioo/MediaFlux/commit/5f2a987)）。
- 资源检索补齐 BTBTLA 新旧页面解析与原生翻页、Nyaa 镜像翻页识别和 1LOU 超时降级；下线 AnimeTosho，并在读取旧配置时安全忽略残留站点 ID（[`b667719`](https://github.com/li88iioo/MediaFlux/commit/b667719)）。
- Docker 工作流在普通 `main` 提交仅执行完整测试与 amd64 冒烟验证，多架构构建、镜像发布和 GitHub Release 仅由正式版本标签触发，减少无标签构建的重复资源消耗（[`74a71ae`](https://github.com/li88iioo/MediaFlux/commit/74a71ae)）。

### Fixed
- 修复整理联动 STRM 在变化目标合并时把布尔值当作可迭代对象，导致 `'bool' object is not iterable`；同时加强发布名清洗与队列合并边界（[`50aaf85`](https://github.com/li88iioo/MediaFlux/commit/50aaf85)）。
- 修复 Telegram 终态编辑失败时重复补发、投递结果未知时盲目重放、旧 revision 覆盖新状态，以及输入状态早于候选卡更新消失的问题（[`9a41d6f`](https://github.com/li88iioo/MediaFlux/commit/9a41d6f)、[`0636477`](https://github.com/li88iioo/MediaFlux/commit/0636477)、[`0f2de1d`](https://github.com/li88iioo/MediaFlux/commit/0f2de1d)、[`cb4957f`](https://github.com/li88iioo/MediaFlux/commit/cb4957f)）。
- 修复本地媒体移动提交后收尾异常可能错误恢复 qB、配置文件与 STRM 退役状态可能分裂、发现/索引/反代客户端关机泄漏、持久缓存无界增长，以及 Chromium 首次打开目录菜单时被自身 resize/scroll 立即关闭的问题（[`cb4957f`](https://github.com/li88iioo/MediaFlux/commit/cb4957f)）。
- 发布流水线现拒绝用不同提交覆盖既有精确版本镜像或 GitHub Release，保持正式版本标签与制品不可变（[`cb4957f`](https://github.com/li88iioo/MediaFlux/commit/cb4957f)）。
- 修复 Python 3.13/部分文件系统快速替换 SQLite 数据库时 inode 被复用，导致连接误用旧 WAL 协商缓存并使发布测试失败的问题；文件代际现同时校验纳秒级 ctime（[`74a71ae`](https://github.com/li88iioo/MediaFlux/commit/74a71ae)）。

## [0.1.9] - 2026-08-29

### Added
- 新增统一媒体库映射工作台，可在同一页面为 STRM 子目录与本地归档分类绑定 Jellyfin/Emby 媒体库、容器路径和服务器可见路径，并通过受限目录浏览器选择挂载目录（[`d0ee96f`](https://github.com/li88iioo/MediaFlux/commit/d0ee96f)）。
- 本地手动整理补齐目录级识别、外部候选线索、季集编号模式持久化、媒体参数探测及多版本替换计划；预览与执行复用同一编号规则，并在来源或既有目标变化时安全拒绝或回滚（[`d0ee96f`](https://github.com/li88iioo/MediaFlux/commit/d0ee96f)）。

### Changed
- Agent Web 与 Telegram 统一采用面向用户的自然语言结果投影，推荐查询支持年份、地区与类型约束，并减少内部检查状态、原始工具结构和无效后续提示对会话的干扰（[`0a39e1e`](https://github.com/li88iioo/MediaFlux/commit/0a39e1e)、[`483437a`](https://github.com/li88iioo/MediaFlux/commit/483437a)、[`8d1eee1`](https://github.com/li88iioo/MediaFlux/commit/8d1eee1)）。
- Agent Web 工作区移除重复的常用任务与欢迎卡，改为稳定的空会话输入态，并收敛标题、阴影、按钮和继续会话交互，使首次会话与历史会话保持一致（[`6c69b3e`](https://github.com/li88iioo/MediaFlux/commit/6c69b3e)、[`7f7e46e`](https://github.com/li88iioo/MediaFlux/commit/7f7e46e)）。

### Fixed
- 修复包含站点包装、集号和发布组信息的剧集文件名清洗不完整，导致标题或季集位置识别偏差的问题（[`021e1f1`](https://github.com/li88iioo/MediaFlux/commit/021e1f1)）。
- 修复统一媒体库映射并发保存时数据库绑定与 `user.env` 可能互相覆盖的问题；保存操作现跨线程/进程串行，配置冲突会回滚绑定并返回明确状态，同时显式释放媒体服务器探测连接（[`b14b867`](https://github.com/li88iioo/MediaFlux/commit/b14b867)）。
- 修复非全屏桌面窗口、iPad 与窄屏下固定保存栏、侧边栏退出入口及设置双栏可能被窗口边界裁切的问题，并统一共享工作台的平板响应断点（[`aca32fe`](https://github.com/li88iioo/MediaFlux/commit/aca32fe)）。
- 修复本地媒体删除快照中的纳秒时间及文件系统标识经浏览器 JSON 往返后发生整数舍入，导致 Docker/NAS 部署误报“条目在读取后发生变化”的问题（[`aca32fe`](https://github.com/li88iioo/MediaFlux/commit/aca32fe)）。
- 修复媒体反代客户端切源、取消播放或离开页面时 `ClientDisconnect` 被错误记录为 Uvicorn ASGI 异常堆栈的问题（[`aca32fe`](https://github.com/li88iioo/MediaFlux/commit/aca32fe)）。

## [0.1.8] - 2026-08-28

### Added
- Media Agent 升级为面向项目全链路的领域编排运行时：新增能力检索、媒体事实状态、统一响应契约与目标识别，可通过自然语言组合媒体检索、缺集检查、本地来源扫描、STRM 同步、媒体库刷新、反代诊断及下载分发，并对写操作统一生成可审计确认计划（[`ccc9f14`](https://github.com/li88iioo/MediaFlux/commit/ccc9f14)、[`c84825f`](https://github.com/li88iioo/MediaFlux/commit/c84825f)、[`c858c38`](https://github.com/li88iioo/MediaFlux/commit/c858c38)）。
- 新增受控的光鸭媒体工作区，支持目录检查、残留垃圾识别、媒体名称清理、批量改名和变更计划预览；所有移动、回收与改名操作均经过冻结计划、用户确认、后端复核和结果审计（[`b0e75db`](https://github.com/li88iioo/MediaFlux/commit/b0e75db)）。
- 索引检索新增规范化发布信息、分层查询计划、并发控制与质量排序，改善 Nyaa、OneLou、Pirate Bay、BTBTLA 等来源的搜索召回、镜像降级和候选资源排序（[`ade8236`](https://github.com/li88iioo/MediaFlux/commit/ade8236)）。

### Changed
- Jellyfin/Emby 精准刷新改为持久化合并队列：自动合并相邻变化路径、去重并发刷新，优先刷新 Series、Movie 或媒体库物理根，自动链路不再因定位失败隐式触发全库扫描（[`daf7659`](https://github.com/li88iioo/MediaFlux/commit/daf7659)）。
- Agent 的 Telegram/Web 进度、确认卡、操作编号与部分成功结果采用统一投影，长任务可恢复已完成检查，并允许按来源或计划项精确执行而非固定处理全部对象（[`ccc9f14`](https://github.com/li88iioo/MediaFlux/commit/ccc9f14)、[`c84825f`](https://github.com/li88iioo/MediaFlux/commit/c84825f)）。

### Fixed
- 修复本地目录浏览无法正确进入部分挂载路径、整理媒体规格统计重复，以及增量 STRM 同步跳过数量不准确的问题（[`515e3f8`](https://github.com/li88iioo/MediaFlux/commit/515e3f8)）。
- 修复 Agent 对 Telegram 状态查询、Jellyfin/Emby 反代别名、Agent 开关表达和“检查剧集更新后推送”等复合请求的误路由，并补齐确认门、限流、操作历史及结果投影的一致性（[`c858c38`](https://github.com/li88iioo/MediaFlux/commit/c858c38)）。

## [0.1.7] - 2026-08-27

### Added
- Media Agent 新增执行阶段进度事件与 Telegram 实时状态更新，并优化长回复的段落、列表及反馈识别，减少等待过程中的无响应感（[`b0209ae`](https://github.com/li88iioo/MediaFlux/commit/b0209ae)、[`0196d2b`](https://github.com/li88iioo/MediaFlux/commit/0196d2b)）。
- 下载管理新增原始种子缓存保留策略；光鸭重新提交可从 qBittorrent 5.x 导出任务种子恢复文件树，降低历史资源因本地种子缺失而无法重试的概率（[`f56e59c`](https://github.com/li88iioo/MediaFlux/commit/f56e59c)）。

### Changed
- Docker 生产部署改为开箱即用的精简 Compose：默认 host 网络、首次启动 Web 初始化、自动创建持久化目录并兼容 NAS 权限；开发配置独立维护，仍可按需启用固定 UID/GID 与 bridge 端口映射（[`04cf206`](https://github.com/li88iioo/MediaFlux/commit/04cf206)）。

### Fixed
- 修复 Telegram“全部整理”与本地手动整理流程中确认卡缺失、确认结果未闭环、任务恢复及 Agent 路由异常的问题（[`b9e9822`](https://github.com/li88iioo/MediaFlux/commit/b9e9822)、[`ab8e23d`](https://github.com/li88iioo/MediaFlux/commit/ab8e23d)、[`2d9e0b9`](https://github.com/li88iioo/MediaFlux/commit/2d9e0b9)）。
- 修复媒体标题使用 Unicode 罗马数字表达续作季数时无法正确识别的问题，并补齐目录身份缓存与识别链路回归覆盖（[`b16b7e2`](https://github.com/li88iioo/MediaFlux/commit/b16b7e2)）。
- 修复下载请求、本地整理任务、调度器和终态回写之间的并发一致性问题；qB 完成任务现以事务方式创建或复用并绑定，避免重复重置、错误重跑与状态漂移（[`f105546`](https://github.com/li88iioo/MediaFlux/commit/f105546)）。

## [0.1.6] - 2026-08-26

### Added
- 媒体反代实例新增可信代理来源配置，可按实例校验直接连接方的 IP/CIDR，并在明确授权后安全还原 `X-Forwarded-For` 中的真实客户端地址（[`0430e3d`](https://github.com/li88iioo/MediaFlux/commit/0430e3d)）。
- 新增统一的媒体库路径映射管理，可为本地媒体分类和 STRM 子目录绑定 Jellyfin/Emby 媒体库及服务器可见路径，并据此执行精准刷新（[`fedd358`](https://github.com/li88iioo/MediaFlux/commit/fedd358)）。
- Telegram 新增 Media Agent 控制面板，可直接查看并切换全局与 Telegram Agent 状态，传统整理、同步、搜索、RSS 和运行状态命令保持独立可用（[`1bff479`](https://github.com/li88iioo/MediaFlux/commit/1bff479)）。

### Changed
- 拆分媒体反代的播放信息、签名直链与重定向耗时统计，使首播延迟、缓存命中和具体慢点可独立诊断（[`8129a72`](https://github.com/li88iioo/MediaFlux/commit/8129a72)）。
- 优化索引器站点适配、镜像故障切换、请求超时与重试边界，提高 Mikan、BtBtLa 等来源失效时的检索可用性（[`b96ccc5`](https://github.com/li88iioo/MediaFlux/commit/b96ccc5)）。
- 统一整理规则、元数据设置、整理详情和日志界面的视觉细节，稳定移动端弹窗布局并移除不必要的卡片动效与侧栏滚动条干扰（[`e7aded8`](https://github.com/li88iioo/MediaFlux/commit/e7aded8)、[`6593c49`](https://github.com/li88iioo/MediaFlux/commit/6593c49)、[`e9d671d`](https://github.com/li88iioo/MediaFlux/commit/e9d671d)、[`051e06b`](https://github.com/li88iioo/MediaFlux/commit/051e06b)、[`74ae25e`](https://github.com/li88iioo/MediaFlux/commit/74ae25e)、[`ae55d10`](https://github.com/li88iioo/MediaFlux/commit/ae55d10)）。

### Fixed
- 修复媒体反代高级配置折叠区因浏览器命中旧主样式缓存而显示为散落图标和文本的问题（[`fd5ecb0`](https://github.com/li88iioo/MediaFlux/commit/fd5ecb0)）。
- 修复仪表盘可播放媒体统计偏差，以及本地媒体整理识别结果未完整持久化、详情中标题/TMDB/类型/季集信息缺失的问题（[`03be7dd`](https://github.com/li88iioo/MediaFlux/commit/03be7dd)、[`5bf3078`](https://github.com/li88iioo/MediaFlux/commit/5bf3078)）。
- 加强下载、RSS、订阅、整理、媒体代理、通知和任务队列的端到端生命周期、并发边界、失败恢复与状态一致性（[`f250ff9`](https://github.com/li88iioo/MediaFlux/commit/f250ff9)、[`2a4e179`](https://github.com/li88iioo/MediaFlux/commit/2a4e179)、[`0a86175`](https://github.com/li88iioo/MediaFlux/commit/0a86175)、[`07f8a6a`](https://github.com/li88iioo/MediaFlux/commit/07f8a6a)、[`fa244da`](https://github.com/li88iioo/MediaFlux/commit/fa244da)）。
- 修复光鸭离线任务仅视频选择、完成状态与整理触发边界，并支持清理历史记录后显式重新提交同一资源（[`c2745c1`](https://github.com/li88iioo/MediaFlux/commit/c2745c1)、[`5594d78`](https://github.com/li88iioo/MediaFlux/commit/5594d78)）。
- Agent 开关改为运行时切换，不再为了启停 Telegram Agent 重启 Bot；关闭时会阻止旧任务继续产生受控副作用（[`71281d0`](https://github.com/li88iioo/MediaFlux/commit/71281d0)）。
- 修复进程重启后 STRM 元数据持久刷新任务可能在启动初期被错误节流的问题，确保待刷新路径会立即恢复执行（[`ea56761`](https://github.com/li88iioo/MediaFlux/commit/ea56761)）。
- 修复媒体库路径映射的 STRM 目录选择器将真实子目录误判为空的问题，现在可从 `/data/strm/光鸭云盘` 逐级选择整理目录与媒体分类（[`c0eede0`](https://github.com/li88iioo/MediaFlux/commit/c0eede0)）。

## [0.1.5] - 2026-08-24

### Added
- 媒体 Agent 扩展为可持续的影视工作台：支持从已核验候选创建、暂停、恢复和删除媒体追更订阅，并覆盖 RSS、资源候选、本地媒体恢复、媒体库巡检、STRM 与媒体反代诊断等连续工作流；高风险写操作仍需明确确认（[`8213751`](https://github.com/li88iioo/MediaFlux/commit/8213751)、[`a4d2a69`](https://github.com/li88iioo/MediaFlux/commit/a4d2a69)、[`3037a9e`](https://github.com/li88iioo/MediaFlux/commit/3037a9e)、[`c676b04`](https://github.com/li88iioo/MediaFlux/commit/c676b04)、[`bb32494`](https://github.com/li88iioo/MediaFlux/commit/bb32494)）。
- Agent 新增结构化核验、持久会话上下文、跨重启候选恢复、流式步骤跟踪和离线评测门禁；自然语言“确认/取消”可直接驱动当前待确认动作（[`4527f68`](https://github.com/li88iioo/MediaFlux/commit/4527f68)、[`9154c02`](https://github.com/li88iioo/MediaFlux/commit/9154c02)、[`8477595`](https://github.com/li88iioo/MediaFlux/commit/8477595)、[`c8cc360`](https://github.com/li88iioo/MediaFlux/commit/c8cc360)、[`def6f6c`](https://github.com/li88iioo/MediaFlux/commit/def6f6c)）。

### Changed
- Agent 路由改为优先结合 LLM 规划、已验证上下文和安全证据执行，并统一 Web/Telegram 的结果呈现、后续提问与恢复语义；兼容型 LLM Provider 遇到可恢复协议错误或瞬时限流时会在预算内降级或重试（[`e5eea7a`](https://github.com/li88iioo/MediaFlux/commit/e5eea7a)、[`f5a8472`](https://github.com/li88iioo/MediaFlux/commit/f5a8472)、[`9ea3903`](https://github.com/li88iioo/MediaFlux/commit/9ea3903)、[`a5b2438`](https://github.com/li88iioo/MediaFlux/commit/a5b2438)、[`a3a5a17`](https://github.com/li88iioo/MediaFlux/commit/a3a5a17)）。
- 本地媒体整理后的 Jellyfin/Emby 刷新改为按变化路径和已绑定媒体库精确触发；无法安全定位目标库时默认跳过全库扫描，避免无关媒体库被重复刷新（[`d486f4f`](https://github.com/li88iioo/MediaFlux/commit/d486f4f)）。
- Docker 发布链路新增源码版本、标签祖先、带日期非空 CHANGELOG、非 root 运行、健康检查、Doctor、数据库升级、多架构元数据、provenance 与 SBOM 门禁，并生成可校验的发布资产；发布脚本纳入 shell 语法回归检查，amd64/arm64 候选镜像按各自 manifest digest 独立执行 smoke，避免本地镜像缓存混淆平台（[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)、[`1f3a9a3`](https://github.com/li88iioo/MediaFlux/commit/1f3a9a3)、[`1acda80`](https://github.com/li88iioo/MediaFlux/commit/1acda80)）。

### Fixed
- 修复 Agent 在自然跟进、多轮短指令、话题切换、较慢旧操作覆盖新结果、订阅单项超时和中断恢复等场景中的上下文误继承或状态丢失；存在 RSS 与媒体追更歧义时会先要求明确类别（[`4602ebf`](https://github.com/li88iioo/MediaFlux/commit/4602ebf)、[`ae3c1a5`](https://github.com/li88iioo/MediaFlux/commit/ae3c1a5)、[`3eff0c9`](https://github.com/li88iioo/MediaFlux/commit/3eff0c9)、[`7655df2`](https://github.com/li88iioo/MediaFlux/commit/7655df2)、[`826ef0a`](https://github.com/li88iioo/MediaFlux/commit/826ef0a)）。
- 修复 Jellyfin、Findroid、Yamby 与 Android ExoPlayer 等原生客户端的 302 直连、认证恢复和 `HEAD` 播放前探测兼容性，并完善 Jellyfin HLS Token 的大小写兼容（[`7f161b3`](https://github.com/li88iioo/MediaFlux/commit/7f161b3)、[`97b8ad7`](https://github.com/li88iioo/MediaFlux/commit/97b8ad7)、[`3439110`](https://github.com/li88iioo/MediaFlux/commit/3439110)、[`9e60d06`](https://github.com/li88iioo/MediaFlux/commit/9e60d06)、[`a5059f5`](https://github.com/li88iioo/MediaFlux/commit/a5059f5)）。
- 修复 Jellyfin Web 切换清晰度后黑屏或标题丢失的问题；直放来源会保持安全播放能力并避免重新落入会被浏览器跨域策略阻断的 HLS 路径（[`160587f`](https://github.com/li88iioo/MediaFlux/commit/160587f)、[`9fe7e12`](https://github.com/li88iioo/MediaFlux/commit/9fe7e12)）。
- 加强整理识别、人工确认、纠错回退与清理保护：歧义标题和高季数剧集必须获得充分证据，否则进入人工确认；任务取消后不会继续调用云盘写入或删除接口（[`2c9295e`](https://github.com/li88iioo/MediaFlux/commit/2c9295e)、[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)）。
- 修复 STRM 整理联动在连续任务、静默窗口、进程重启或初始化失败时可能遗漏变化或遗留运行锁的问题；变更会先持久化、合并，再按顺序恢复执行（[`2c9295e`](https://github.com/li88iioo/MediaFlux/commit/2c9295e)、[`dd1b534`](https://github.com/li88iioo/MediaFlux/commit/dd1b534)）。
- 数据库 schema 升级改为升级前备份和保存点内原子迁移，失败会完整回滚；恢复备份会核验真实数据库 schema，拒绝未来版本或缺少数据库载荷的完整恢复（[`924e2f8`](https://github.com/li88iioo/MediaFlux/commit/924e2f8)、[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)）。
- 确认动作与持久化整理队列增加崩溃恢复语义：中断中的动作会标记为“结果待核对”，避免重启后误报成功或重复执行；队列严格按创建顺序领取（[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)）。

### Security
- 加固媒体反代边界：规范化转发头、阻止 WebSocket 上游重定向、过滤内部直放地址与上游 Cookie，不向媒体服务器泄露 MediaFlux 登录会话，并拒绝跨服务器、目录穿越、重复编码和不安全相对跳转（[`183e249`](https://github.com/li88iioo/MediaFlux/commit/183e249)、[`b47e4b3`](https://github.com/li88iioo/MediaFlux/commit/b47e4b3)、[`6175a50`](https://github.com/li88iioo/MediaFlux/commit/6175a50)）。

## [0.1.4] - 2026-08-22

### Changed
- Jellyfin/Emby Web 的光鸭播放改为由 MediaFlux 提供同源流式中继，支持 `Range`、`If-Range`、`HEAD` 与 CDN 内部重定向；Infuse、VidHub、Fileball、Jellyfin 原生客户端等非浏览器客户端仍保持 302 直连 CDN。浏览器中继会占用 MediaFlux 所在设备的网络带宽，这是绕过上游 CDN 缺少 CORS 响应头所必需的兼容路径（[`0bec9c2`](https://github.com/li88iioo/MediaFlux/commit/0bec9c2)）。

### Fixed
- 修复 Jellyfin Web 的 HTML5 视频请求跟随光鸭 CDN 302 后，因目标 CDN 未返回 `Access-Control-Allow-Origin` 而被浏览器拦截、最终无法播放的问题；Web 播放会话现在会稳定保持同源中继策略，不依赖 `Sec-Fetch-*` 请求头（[`0bec9c2`](https://github.com/li88iioo/MediaFlux/commit/0bec9c2)）。
- 加固浏览器媒体中继的 signed URL 校验与资源释放：逐跳固定公网 DNS 地址，拒绝私网、CGNAT、链路本地、site-local、云元数据及带凭据目标，并过滤 Cookie、Authorization、媒体服务器 Token 与上游 `Set-Cookie`/`Location`（[`0bec9c2`](https://github.com/li88iioo/MediaFlux/commit/0bec9c2)）。

## [0.1.3] - 2026-08-22

### Fixed
- 修复 Jellyfin Web 经光鸭 302 直链播放时，因 PlaybackInfo 残留 HLS/转码字段而错误使用 HLS.js 跨域请求 CDN，最终被浏览器 CORS 策略拦截的问题；光鸭媒体源现在会完整清理转码元数据并继续通过带短时能力凭据的 DirectStream URL 安全跳转至 CDN（[`f89a41c`](https://github.com/li88iioo/MediaFlux/commit/f89a41c)）。

## [0.1.2] - 2026-08-22

### Added
- 本地媒体扫描支持把多层作品/季度目录展开为独立视频单元，并过滤非媒体文件、精确绑定同级字幕；单集识别异常不再阻塞同目录其他内容（[`c6b437c`](https://github.com/li88iioo/MediaFlux/commit/c6b437c)、[`bf31930`](https://github.com/li88iioo/MediaFlux/commit/bf31930)）。
- 本地媒体待确认任务接入 Telegram 原子确认流程，增强 qB 完成探测的重试、失败反馈和任务终态保护（[`7b339f1`](https://github.com/li88iioo/MediaFlux/commit/7b339f1)）。
- 新增整理后媒体规格异步补全队列；实时 `ffprobe` 失败时后台低并发重试，成功后安全重命名并触发 STRM 增量同步（[`9e46a82`](https://github.com/li88iioo/MediaFlux/commit/9e46a82)）。
- 整理与追更通知支持发送媒体封面，图片投递失败时自动降级为文本消息（[`8d826f6`](https://github.com/li88iioo/MediaFlux/commit/8d826f6)）。

### Changed
- 移除已停止维护的 Windows SMB 运行时、UNC 凭据输入和对应测试；本地媒体来源统一使用 Docker 容器绝对路径，启动时清空旧数据库中的 SMB 用户名与密码。qB 的 Windows/UNC 路径前缀映射仍保留（[`37c3ef2`](https://github.com/li88iioo/MediaFlux/commit/37c3ef2)）。
- 动画电影统一按电影类型归档，并收敛 STRM 文件命名，降低 Jellyfin/Emby 元数据识别歧义（[`dd49f9b`](https://github.com/li88iioo/MediaFlux/commit/dd49f9b)）。
- 高频网络、Telegram、302 与媒体接口日志增加限流和敏感信息脱敏；测试进程默认不再写入正式 `app.log`（[`2c015e9`](https://github.com/li88iioo/MediaFlux/commit/2c015e9)、[`37c3ef2`](https://github.com/li88iioo/MediaFlux/commit/37c3ef2)）。

### Fixed
- 修复浏览器经媒体 302 反代播放时 HTML5 视频请求缺少媒体服务器 Token、播放会话无法恢复、重复参数误判，以及普通标题含冒号或斜杠时名称丢失的问题（[`90cc553`](https://github.com/li88iioo/MediaFlux/commit/90cc553)）。
- 修复持续播放超过 15 分钟后短时授权、媒体源映射与播放许可过期的问题；活跃请求会滑动续期，并保留 12 小时绝对安全上限，视频数据继续通过 302 由终端直连云盘 CDN（[`e809255`](https://github.com/li88iioo/MediaFlux/commit/e809255)）。
- 修复人工确认整理日志仍显示跳过、错误提供批量回退操作，以及 Telegram 整理消息封面不稳定的问题（[`8d826f6`](https://github.com/li88iioo/MediaFlux/commit/8d826f6)）。
- 修复移动端弹窗、工具栏和虚拟键盘场景超出 visual viewport 或发生布局跳动的问题（[`8536281`](https://github.com/li88iioo/MediaFlux/commit/8536281)、[`49b27f7`](https://github.com/li88iioo/MediaFlux/commit/49b27f7)）。
- 探索、RSS 和全局搜索中的媒体档案改为页面内弹窗打开，避免查看详情时整页跳转和状态丢失（[`7fccf61`](https://github.com/li88iioo/MediaFlux/commit/7fccf61)）。

## [0.1.1] - 2026-08-21

### Added
- 新增本地媒体来源的多级子目录浏览、面包屑导航和整理任务详情，可查看文件映射与原子执行步骤（[`3471aac`](https://github.com/li88iioo/MediaFlux/commit/3471aac)）。
- 本地媒体手动刮削支持覆盖剧集季号与集数，并复用统一的媒体刮削与位置识别弹窗（[`ae51076`](https://github.com/li88iioo/MediaFlux/commit/ae51076)）。
- Media Agent 新增媒体追更订阅实时核对能力，可联动 TMDB、媒体库和索引器检查更新候选（[`689ed56`](https://github.com/li88iioo/MediaFlux/commit/689ed56)）。
- Docker 镜像内置 `ffprobe`，无需额外安装即可进行音视频规格探测（[`baa4a94`](https://github.com/li88iioo/MediaFlux/commit/baa4a94)）。

### Changed
- 剧集季目录改为不补零的标准形式，例如 `Season 1`；特别篇仍使用 `Specials`（[`efbb911`](https://github.com/li88iioo/MediaFlux/commit/efbb911)）。
- 重构光鸭整理、离线转存、STRM、媒体反代、分享转存、GCID 与本地媒体工作台的布局和响应式交互（[`de6d3ae`](https://github.com/li88iioo/MediaFlux/commit/de6d3ae)）。
- STRM 页面将播放地址操作改为候选发现与完整刷新流程，移除用户侧快速同步入口，并保留整理链路内部精准增量能力（[`a7290df`](https://github.com/li88iioo/MediaFlux/commit/a7290df)）。
- STRM 扫描增加并发校验与批量指纹补写，提升大规模目录同步和校准速度（[`6ed5cab`](https://github.com/li88iioo/MediaFlux/commit/6ed5cab)）。
- 优化 README 的项目标识、徽章链接和标题间距（[`f2b1a55`](https://github.com/li88iioo/MediaFlux/commit/f2b1a55)、[`70e3211`](https://github.com/li88iioo/MediaFlux/commit/70e3211)）。

### Fixed
- 修复 Telegram 富文本进度与终态消息换行被压缩的问题（[`76bb941`](https://github.com/li88iioo/MediaFlux/commit/76bb941)）。
- 修复 Telegram Bot Token 保存后直接测试时被误判为空或无效的问题（[`2a665d5`](https://github.com/li88iioo/MediaFlux/commit/2a665d5)）。
- 修复 Telegram 轮询冲突重复输出堆栈，并优化 NAS/CIFS 文件权限告警与错误提示（[`08dab05`](https://github.com/li88iioo/MediaFlux/commit/08dab05)）。
- 修复侧边栏图标加载时的布局抖动（[`1f0bc1f`](https://github.com/li88iioo/MediaFlux/commit/1f0bc1f)）。

## [0.1.0] - 2026-08-18

### 🚀 初始版本发布 (Initial Release)

MediaFlux 首个正式开源版本发布！致力于为家庭媒体中心提供一站式、全流程、安全可控的影视整理与流转编排方案。

#### 📥 下载编排与任务调度
- **多渠道任务接入**：支持 Mikan 等 RSS 自动追番订阅、Telegram Bot 快捷提交磁力/种子/分享链接，以及 Web 控制台手动推送。
- **双引擎分发**：无缝分发任务至本地 **qBittorrent** 下载或 **光鸭云盘** 离线转存。
- **全链路自动闭环**：下载完成后自动触发刮削、整理归档、STRM 生成以及 Jellyfin/Emby 媒体库刷新。

#### 🎯 TMDB 智能刮削与识别
- **高精度识别算法**：结合标题分词清洗、年份约束与拼音模糊匹配，电影使用独立 TMDB 目录，剧集标准化为 `Season NN`，特别篇进入 `Specials` 且文件使用 `S00E##` 统一命名。
- **人工复核保护**：低置信度结果自动进入人工待确认列表，拒绝误入库。
- **自定义规则与映射锁**：支持自定义正则重命名规则与 TMDB 永久映射锁，特殊命名源一次锁定、永久精准匹配。

#### 📂 本地媒体安全整理
- **事务性安全移动**：同文件系统执行毫秒级原子重命名；跨文件系统采用“先写入目标临时文件 → 校验完整性 → 确认入库 → 安全清理源文件”事务机制。
- **垃圾精准清理**：媒体入库且 qB 任务移除后，仅清理已识别的广告文档、sample 样片等垃圾文件；未知文件与外挂字幕/特效字体原地安全保留。

#### ⚡ 光鸭云盘管理与 STRM 302 直链
- **免 Key 登录**：Web 端支持手机验证码安全登录，Token 本地私密持久化并自动定时刷新。
- **302 直链零转码播放**：Jellyfin/Emby 读取本地 `.strm` 文件，MediaFlux 提供短时签名并 302 重定向至云盘 CDN 直链，视频播放不消耗本地服务器 CPU 与出口下行带宽。
- **增量防误删引擎**：基于本地 SQLite 索引增量维护 STRM，网络抖动或远端异常时自动熔断，坚决防止误删本地媒体库。

#### 🐳 容器化部署与运行时支持
- **Docker Compose**：支持容器化一键部署与多架构（`linux/amd64` + `linux/arm64`）镜像，内置高性能网络栈与非 root 安全隔离。
- **Python 源码运行**：支持标准 Python 3.11+ 生产环境直接运行。
- **Docker-Only 网络配置**：容器内部固定监听 `0.0.0.0:1258`，宿主机发布地址与端口统一由 Compose `.env` 管理，Web 设置页不再修改网络绑定或触发进程自重启。

#### 🛠️ CLI 运维与安全基线
- **内置 `mediaflux` 命令行运维工具**：支持服务状态查询、环境权限诊断 (`doctor`)、一致性数据备份/校验/恢复以及脱敏支持包导出。
- **本地运行与零遥测**：100% 独立运行在用户设备，无任何远程遥测或数据上报，所有凭据与数据库均保存在本地。
- **严格安全防护**：全局 CSRF 防护、Session 防篡改、首启绑定本地回环与生产密钥强制校验。

[Unreleased]: https://github.com/li88iioo/MediaFlux/compare/v0.1.15...HEAD
[0.1.15]: https://github.com/li88iioo/MediaFlux/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/li88iioo/MediaFlux/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/li88iioo/MediaFlux/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/li88iioo/MediaFlux/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/li88iioo/MediaFlux/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/li88iioo/MediaFlux/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/li88iioo/MediaFlux/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/li88iioo/MediaFlux/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/li88iioo/MediaFlux/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/li88iioo/MediaFlux/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/li88iioo/MediaFlux/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/li88iioo/MediaFlux/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/li88iioo/MediaFlux/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/li88iioo/MediaFlux/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/li88iioo/MediaFlux/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/li88iioo/MediaFlux/releases/tag/v0.1.0
