"""Agent 写操作的稳定、脱敏确认展示契约。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.agent.models import RiskLevel, ToolResult
from app.agent.public_safety import public_tool_label, sanitize_public_text

CONTRACT_VERSION = 1

# 文案只描述可观察影响，不包含参数、路径、URL、票据或服务端内部标识。
_CONFIRMATION_COPY: dict[str, dict[str, str]] = {
    "activity.follow": {
        "action": "活动跟踪开启",
        "object": "本次选定的活动跟踪规则",
        "impact": "保存或停用周期观察；任务异常、阶段结束或到期时通知一次。不启动、重试或取消原任务。",
        "reversibility": "可以停用跟踪；已进入统一通知队列的消息不能由本操作撤回。",
    },
    "activity.unfollow": {
        "action": "活动跟踪停用",
        "object": "本次选定的活动跟踪规则",
        "impact": "保存或停用周期观察；任务异常、阶段结束或到期时通知一次。不启动、重试或取消原任务。",
        "reversibility": "可以停用跟踪；已进入统一通知队列的消息不能由本操作撤回。",
    },
    "action.undo.execute": {
        "action": "可逆操作回退",
        "object": "本次凭证绑定的媒体偏好或光鸭最近可逆文件操作",
        "impact": "只恢复已记录快照，拒绝覆盖后续更改；文件回退排入原整理执行器，不代表已经完成。",
        "reversibility": "不保证能再次撤销回退，不恢复永久删除、已提交下载或已发送通知。",
    },
    "media.user.mark_played": {
        "action": "标记媒体已看",
        "object": "当前媒体用户及预检的单个条目",
        "impact": "只改变确认卡所示的已看或收藏状态，写前复核状态并写后回读，不隐式展开整个剧集。",
        "reversibility": "可再次修改标记，但不能保证恢复原观看进度、播放计数或时间记录。",
    },
    "media.user.mark_unplayed": {
        "action": "标记媒体未看",
        "object": "当前媒体用户及预检的单个条目",
        "impact": "只改变确认卡所示的已看或收藏状态，写前复核状态并写后回读，不隐式展开整个剧集。",
        "reversibility": "可再次修改标记，但不能保证恢复原观看进度、播放计数或时间记录。",
    },
    "media.user.favorite": {
        "action": "收藏媒体",
        "object": "当前媒体用户及预检的单个条目",
        "impact": "只改变确认卡所示的已看或收藏状态，写前复核状态并写后回读，不隐式展开整个剧集。",
        "reversibility": "可再次修改标记，但不能保证恢复原观看进度、播放计数或时间记录。",
    },
    "media.user.unfavorite": {
        "action": "取消媒体收藏",
        "object": "当前媒体用户及预检的单个条目",
        "impact": "只改变确认卡所示的已看或收藏状态，写前复核状态并写后回读，不隐式展开整个剧集。",
        "reversibility": "可再次修改标记，但不能保证恢复原观看进度、播放计数或时间记录。",
    },
    "media.playlist.create": {
        "action": "创建播放列表",
        "object": "已确认的播放列表及成员",
        "impact": "只创建私有列表或增减列表成员，核对完整快照和写后结果；不删除媒体文件。",
        "reversibility": "可通过列表成员操作调整，但不保证恢复原成员次序；未知执行结果需先回读。",
    },
    "media.playlist.add_items": {
        "action": "添加播放列表成员",
        "object": "已确认的播放列表及成员",
        "impact": "只创建私有列表或增减列表成员，核对完整快照和写后结果；不删除媒体文件。",
        "reversibility": "可通过列表成员操作调整，但不保证恢复原成员次序；未知执行结果需先回读。",
    },
    "media.playlist.remove_items": {
        "action": "移除播放列表成员",
        "object": "已确认的播放列表及成员",
        "impact": "只创建私有列表或增减列表成员，核对完整快照和写后结果；不删除媒体文件。",
        "reversibility": "可通过列表成员操作调整，但不保证恢复原成员次序；未知执行结果需先回读。",
    },
    "automation.create_media_rule": {
        "action": "创建媒体自动追更规则",
        "object": "预检识别的媒体追更订阅",
        "impact": "按确认卡保存检查间隔、站点、下载目标和动作；若选择自动模式，之后匹配资源无需逐次确认即可提交。",
        "reversibility": "可暂停或删除订阅；已提交的下载无法由此撤回。不支持的画质/字幕硬过滤不会被保存。",
    },
    "automation.set_digest": {
        "action": "主动摘要规则保存",
        "object": "当前身份绑定的每日摘要规则",
        "impact": "按本地时区保存启停、时间和异常过滤；复用通知中心，不周期调用模型。",
        "reversibility": "可以再次修改或停用；已入队通知不撤回。",
    },
    "config.create_recognition_knowledge": {
        "action": "新增识别知识",
        "object": "发布组或尾部制作组识别知识",
        "impact": "只改指定用户知识条目或别名，不修改内置条目来源，也不批量重新整理已有媒体。",
        "reversibility": "可通过原知识配置界面修改；删除前请核对内容，不承诺通用撤销。",
    },
    "config.update_recognition_knowledge": {
        "action": "修改识别知识",
        "object": "发布组或尾部制作组识别知识",
        "impact": "只改指定用户知识条目或别名，不修改内置条目来源，也不批量重新整理已有媒体。",
        "reversibility": "可通过原知识配置界面修改；删除前请核对内容，不承诺通用撤销。",
    },
    "config.delete_recognition_knowledge": {
        "action": "删除识别知识",
        "object": "发布组或尾部制作组识别知识",
        "impact": "只改指定用户知识条目或别名，不修改内置条目来源，也不批量重新整理已有媒体。",
        "reversibility": "可通过原知识配置界面修改；删除前请核对内容，不承诺通用撤销。",
    },
    "config.create_local_source": {
        "action": "新增本地媒体来源",
        "object": "指定本地媒体来源配置",
        "impact": "复用本地来源生命周期修改配置，不主动整理或删除媒体文件；新来源默认仅预览且不开启qB自动接管。",
        "reversibility": "可重新配置，但删除条目的编号或历史关联不保证恢复。",
    },
    "config.update_local_source": {
        "action": "修改本地媒体来源",
        "object": "指定本地媒体来源配置",
        "impact": "复用本地来源生命周期修改配置，不主动整理或删除媒体文件；新来源默认仅预览且不开启qB自动接管。",
        "reversibility": "可重新配置，但删除条目的编号或历史关联不保证恢复。",
    },
    "config.delete_local_source": {
        "action": "删除本地媒体来源",
        "object": "指定本地媒体来源配置",
        "impact": "复用本地来源生命周期修改配置，不主动整理或删除媒体文件；新来源默认仅预览且不开启qB自动接管。",
        "reversibility": "可重新配置，但删除条目的编号或历史关联不保证恢复。",
    },
    "config.create_media_path_mapping": {
        "action": "新增媒体路径映射",
        "object": "指定媒体服务器的路径前缀映射",
        "impact": "使用Web相同的配置锁更新映射，保留其他服务器配置及凭据；不移动文件或触发扫描。",
        "reversibility": "可再次配置原映射；已发出的刷新请求不受本操作撤回。",
    },
    "config.update_media_path_mapping": {
        "action": "修改媒体路径映射",
        "object": "指定媒体服务器的路径前缀映射",
        "impact": "使用Web相同的配置锁更新映射，保留其他服务器配置及凭据；不移动文件或触发扫描。",
        "reversibility": "可再次配置原映射；已发出的刷新请求不受本操作撤回。",
    },
    "config.delete_media_path_mapping": {
        "action": "删除媒体路径映射",
        "object": "指定媒体服务器的路径前缀映射",
        "impact": "使用Web相同的配置锁更新映射，保留其他服务器配置及凭据；不移动文件或触发扫描。",
        "reversibility": "可再次配置原映射；已发出的刷新请求不受本操作撤回。",
    },

    "provider.change.execute": {
        "action": "执行 Provider 写计划",
        "object": "刚才预检并冻结的媒体服务器或 qBittorrent 目标",
        "impact": "只会执行写计划中已经冻结的精确操作；不能在确认阶段新增目标、修改参数或退化为全库刷新。qB 任务移除始终保留已下载文件。",
        "reversibility": "暂停和恢复可通过反向操作撤销；媒体刷新请求无法撤回；移除的 qB 任务不会由 MediaFlux 自动恢复，但本地文件会保留。",
    },
    "downloads.retry_submission": {
        "action": "重新提交下载请求",
        "object": "你指定的一条下载待处理记录",
        "impact": "会使用服务端保留的原始请求创建新的下载提交；不会在确认页暴露链接、种子、路径或凭据。",
        "reversibility": "已发出的下载请求无法撤回；提交失败时原记录会继续保留在待处理列表。",
    },
    "rss.create_subscription": {
        "action": "创建 RSS 订阅",
        "object": "你刚才提供并通过服务端校验的 RSS 订阅配置",
        "impact": "会保存订阅、过滤、刷新和下载目标配置并重新加载调度器；不会立即抓取历史条目或创建下载任务。",
        "reversibility": "可再次修改、停用或删除订阅；已经由后续刷新创建的下载请求不受删除订阅影响。",
    },
    "rss.update_subscription": {
        "action": "更新 RSS 订阅配置",
        "object": "你指定的一条 RSS 订阅",
        "impact": "会更新确认页列出的配置字段并重新加载调度器；不会立即刷新订阅或创建下载任务。",
        "reversibility": "可再次确认修改；确认前订阅若已变化，本次票据会自动失效。",
    },
    "media.set_subscription_enabled": {
        "action": "切换媒体追更状态",
        "object": "你指定的一条媒体追更订阅",
        "impact": "暂停会停止安排新检查并失效尚未提交的候选资源；恢复会让订阅重新进入检查队列。已提交下载任务和媒体文件不受影响。",
        "reversibility": "可再次确认暂停或恢复该订阅。",
    },
    "media.set_subscription_policy": {
        "action": "修改媒体追更策略",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会更新后续追更范围、动作模式、下载目标或检查周期，并失效旧候选；若启用自动模式，后续匹配资源会无需再次确认自动提交。本操作不会主动发起检查。",
        "reversibility": "可再次修改后续策略；已进入提交阶段或已经提交的下载任务无法由本次修改撤回。",
    },
    "media.set_preferences": {
        "action": "保存媒体偏好",
        "object": "当前登录用户的显式媒体偏好",
        "impact": "会保存媒体服务器、下载目标及画质/语言/题材/观看过滤偏好，供后续推荐和选源使用；单次明确要求优先。不会修改系统全局配置或立即创建下载。",
        "reversibility": "可再次修改，或确认清除后恢复产品默认值。",
    },
    "media.clear_preferences": {
        "action": "清除媒体偏好",
        "object": "当前登录用户已保存的显式媒体偏好",
        "impact": "会删除当前用户的偏好记录并恢复产品默认值；不会修改媒体服务器、下载器或已有任务。",
        "reversibility": "清除后可重新保存显式偏好。",
    },
    "media.set_subscription_notification_rule": {
        "action": "修改媒体追更通知规则",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会影响后续缺集、已满足或检查异常事件是否发送 Telegram 通知；不会立即巡检、下载或修改追更策略。",
        "reversibility": "可再次修改开关，或重置为默认关闭规则。",
    },
    "media.reset_subscription_notification_rule": {
        "action": "重置媒体追更通知规则",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会删除该订阅的显式通知规则并恢复默认关闭状态；已发送通知不会撤回。",
        "reversibility": "可再次确认创建新的通知规则。",
    },
    "media.create_subscription": {
        "action": "创建媒体追更订阅",
        "object": "你刚才选定的影视条目和季度",
        "impact": "会创建一条追更订阅并安排后续更新检查；不会立即搜索资源或提交下载。",
        "reversibility": "可暂停或删除这条订阅；已经提交的下载任务和媒体文件不受影响。",
    },
    "media.delete_subscription": {
        "action": "删除媒体追更订阅",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会删除订阅记录并停止后续更新检查；不会删除已提交的下载任务或媒体文件。",
        "reversibility": "删除不可撤销；如果只想暂时停止追更，请改为暂停订阅。",
    },
    "rss.delete_subscription": {
        "action": "永久删除 RSS 订阅",
        "object": "你指定的 RSS 订阅及其本地条目记录",
        "impact": "会删除订阅配置与本地条目记录；不会删除下载任务或已下载文件。",
        "reversibility": "删除不可撤销；如只想停止定时刷新，请改为停用订阅。",
    },
    "rss.refresh_subscription": {
        "action": "立即刷新 RSS 订阅",
        "object": "你选择的订阅源",
        "impact": "会抓取一次最新条目并更新订阅记录，不会自动创建下载任务。",
        "reversibility": "刷新请求无法撤回；新增条目仍可在订阅页查看和处理。",
    },
    "rss.refresh_subscriptions": {
        "action": "立即刷新多个 RSS 订阅",
        "object": "当前已启用且配置完整的订阅源",
        "impact": "会依次抓取一次最新条目并更新订阅记录，不会自动创建下载任务。",
        "reversibility": "刷新请求无法撤回；新增条目仍可在订阅页查看和处理。",
    },
    "rss.mark_entries": {
        "action": "标记 RSS 条目",
        "object": "本次预检选中的精确 RSS 条目",
        "impact": "只会更新本地处理状态，不会删除订阅、下载任务或媒体文件。",
        "reversibility": "可对仍允许修改的条目再次确认恢复处理状态。",
    },
    "rss.submit_entries_to_qb": {
        "action": "提交指定 RSS 条目",
        "object": "本次预检冻结的精确待处理条目集合",
        "impact": "会在 qBittorrent 创建下载任务并更新条目状态。",
        "reversibility": "可在下载器中暂停或删除任务；已发出的提交请求无法撤回。",
    },
    "rss.submit_pending_to_qb": {
        "action": "提交 RSS 待处理条目",
        "object": "本次预检选中的条目",
        "impact": "会在 qBittorrent 中创建下载任务，并改变这些条目的处理状态。",
        "reversibility": "可在下载器中暂停或删除任务；已发出的提交请求无法撤回。",
    },
    "rss.retry_failed_to_qb": {
        "action": "重试 RSS 失败条目",
        "object": "本次预检判定可安全重试的条目",
        "impact": "会再次向 qBittorrent 提交任务，并更新失败条目的处理状态。",
        "reversibility": "可在下载器中暂停或删除任务；已发出的重试请求无法撤回。",
    },
    "discovery.confirm_mapping": {
        "action": "确认影视身份映射",
        "object": "当前会话最近查询并重新核验的一个 TMDB 候选",
        "impact": "会保存一条确认过的跨来源身份映射；不会收藏、订阅、搜索资源或下载。",
        "reversibility": "可通过后续显式确认改正为另一候选；本次写入不会改动媒体文件。",
    },
    "discovery.add_watchlist": {
        "action": "加入探索收藏",
        "object": "最近探索结果中预检通过的一个影视条目",
        "impact": "只会写入本地探索收藏记录，不会搜索资源、创建订阅或提交下载。",
        "reversibility": "可再次确认移除该收藏；媒体文件和下载任务不受影响。",
    },
    "discovery.remove_watchlist": {
        "action": "移除探索收藏",
        "object": "你按精确收藏编号选择的一条本地收藏记录",
        "impact": "只会删除本地收藏记录，不会删除媒体文件、订阅或下载任务。",
        "reversibility": "移除后不能自动恢复，但可重新搜索并再次加入收藏。",
    },
    "config.set_indexer_sites": {
        "action": "更新资源检索站点范围",
        "object": "MediaFlux 的资源站选择",
        "impact": "后续资源检索只会使用新选择的站点，不会立即发起搜索或下载。",
        "reversibility": "可随时再次修改站点选择恢复。",
    },
    "config.set_feature_state": {
        "action": "切换项目功能状态",
        "object": "本次预检指向的功能",
        "impact": "保存后会改变该功能的可用状态；不会执行下载或整理任务。",
        "reversibility": "可再次切换回原状态；环境变量锁定的配置不会被覆盖。",
    },
    "config.set_safe_policy": {
        "action": "更新安全白名单策略",
        "object": "本次预检指定的一项非敏感策略",
        "impact": "保存后只会改变该策略及相关运行时缓存；登录壁纸策略可能在后台请求 TMDB 刷新图片，不会下载媒体、整理文件或读取凭据。",
        "reversibility": "可再次确认改回原值；由环境变量管理的配置不会被覆盖。",
    },
    "telegram.send_test_notification": {
        "action": "发送 Telegram 测试通知",
        "object": "当前已配置的 Telegram 通知会话",
        "impact": "只会发送一条固定的 MediaFlux 连接测试消息；不会附带业务数据、配置内容、Token 或 Chat ID。",
        "reversibility": "消息发送后不能由 MediaFlux 撤回；如需移除，请在 Telegram 中手动删除。",
    },
    "media_proxy.set_instance_enabled": {
        "action": "切换媒体反代实例状态",
        "object": "你按公开序号指定的媒体反代实例",
        "impact": "启用会按已保存配置启动反代监听；停用可能中断正在通过该实例播放的媒体。不会修改或展示地址、端口、路径与凭据。",
        "reversibility": "可再次确认切换回原状态；已中断的播放会话不会自动恢复。",
    },
    "media_proxy.restart_instance": {
        "action": "重启媒体反代实例",
        "object": "你按公开序号指定的已启用媒体反代实例",
        "impact": "会清理该实例短时直链缓存并重建运行时，正在播放的会话可能短暂中断；不会修改实例配置。",
        "reversibility": "运行时重建无法撤回，但实例仍使用原配置；失败时可重启 MediaFlux 恢复。",
    },
    "local_media.scan_sources": {
        "action": "扫描本地媒体来源",
        "object": "本次预检冻结的已配置本地媒体来源",
        "impact": "会把发现的媒体加入本地整理队列；后续文件操作继续服从现有来源与归档规则。",
        "reversibility": "扫描本身不修改文件；进入队列后的任务可在执行前通过来源控制停止后续接管。",
    },
    "local_media.set_source_trigger_enabled": {
        "action": "切换本地媒体来源触发方式",
        "object": "你按公开序号指定的本地媒体来源触发器",
        "impact": "只会启用或停用 qB 下载完成自动接管；不会修改或展示来源目录、目标目录、整理规则、媒体服务器绑定、下载器配置或凭据。停用时，等待执行且依赖该触发方式的任务可能失败。",
        "reversibility": "可再次确认切换回原状态；已经开始的文件操作不会被强制中断。",
    },
    "local_media.retry_task": {
        "action": "重新排队本地媒体任务",
        "object": "你按短期公开序号指定的失败或待人工确认任务",
        "impact": "任务会回到等待执行阶段并由调度器立即重新处理；本次确认不会直接移动、覆盖或删除文件。",
        "reversibility": "可在任务真正开始前停用对应来源触发器；已开始的文件操作不会被本次确认自动撤回。",
    },
    "local_media.refresh_task_library": {
        "action": "精准刷新绑定媒体库",
        "object": "已完成本地媒体任务唯一绑定的媒体服务器和媒体库",
        "impact": "只提交受限路径刷新；无法唯一定位会安全停止，绝不会退化为全库刷新。",
        "reversibility": "刷新请求无法撤回，但不会移动、覆盖或删除媒体文件。",
    },
    "recognition.set_rule_enabled": {
        "action": "切换识别规则状态",
        "object": "你按类型和编号指定的一条识别规则",
        "impact": "只会改变该规则是否参与后续识别；不会修改规则内容、匹配表达式、TMDB 映射、别名或优先级。",
        "reversibility": "可再次确认切换回原状态；已经完成的识别结果不会自动重跑。",
    },
    "strm.retry_failures": {
        "action": "重试 STRM 失败项",
        "object": "本次预检匹配的失败记录",
        "impact": "会重新生成或补齐对应 STRM 内容，并更新失败状态。",
        "reversibility": "可停止后续处理；已成功写入的结果不会自动回滚。",
    },
    "strm.run_once": {
        "action": "立即执行一次 STRM 同步",
        "object": "当前已配置的 STRM 来源",
        "impact": "会扫描来源并按现有规则创建或更新 STRM 内容。",
        "reversibility": "可停止后续运行；本次已写入的结果不会自动回滚。",
    },
    "strm.set_schedule_policy": {
        "action": "更新 STRM 定时同步策略",
        "object": "定时同步的启用状态、计划表达式或任务通知",
        "impact": "会重新安排后续定时同步，不会立即启动或中断同步任务。",
        "reversibility": "可再次修改策略恢复；环境变量锁定的配置不会被覆盖。",
    },
    "guangya.fs.change.execute": {
        "action": "执行光鸭文件变更",
        "object": "最近只读快照中冻结的改名、移动、复制、回收或新建目录操作",
        "impact": "只执行预览中固定的对象和目标；写前复核快照与冲突，回收操作只进入 Provider 回收站，写后逐项验证。",
        "reversibility": "回收项可在光鸭回收站恢复；复制会保留原对象；改名、移动和新建目录需依据私有执行清单手工恢复。",
    },
    "guangya.recycle.restore": {
        "action": "恢复光鸭回收站对象",
        "object": "刚才回收站快照中选定的对象",
        "impact": "只恢复确认计划冻结的对象；确认时会重新核对登录凭据、对象名称、类型、体积和快照状态。",
        "reversibility": "恢复后可再次通过受控文件变更移入回收站；若原目录或同名对象已变化，本次确认会失效。",
    },
    "guangya.recycle.clear": {
        "action": "永久清空光鸭回收站",
        "object": "预检时完整读取并冻结的全部回收站内容",
        "impact": "确认后会永久删除回收站中的全部对象；任一对象或登录凭据发生变化都会使计划失效。",
        "reversibility": "不可撤销，MediaFlux 和光鸭回收站都无法自动恢复被清空的对象。",
    },
    "guangya.share.create": {
        "action": "创建光鸭分享",
        "object": "最近目录快照中选定的云盘对象和冻结的分享策略",
        "impact": "确认后会创建新的外部分享链接，并应用确认页中的有效期、访问码、转存次数和下载权限。",
        "reversibility": "可在后续分享列表中撤销链接；撤销不会删除或移动原始云盘文件。",
    },
    "guangya.share.revoke": {
        "action": "撤销光鸭分享",
        "object": "刚才分享列表快照中选定的分享链接",
        "impact": "确认后原分享链接将停止提供访问；不会删除、移动或改名分享中的原始云盘对象。",
        "reversibility": "原链接通常不能恢复；如仍需分享，可对原始对象重新创建一个新链接。",
    },
    "guangya.rename.execute": {
        "action": "执行光鸭重命名",
        "object": "当前会话最近冻结并排除冲突的批量名称转换或媒体名称清理计划",
        "impact": "只应用冻结计划中的名称映射；不扩大范围、移动或删除对象，执行前复核快照，写后验证真实名称并按计划决定是否核对 STRM。",
        "reversibility": "私有清单会保留 file_id、原目录和原名称，可用于后续受控回滚；本次不会移动或删除文件。",
    },
    "guangya.organize.set_schedule_policy": {
        "action": "更新光鸭定时整理策略",
        "object": "定时整理的启用状态、计划表达式或任务通知",
        "impact": "会重新安排后续定时整理，不会立即启动或中断整理任务。",
        "reversibility": "可再次修改策略恢复；环境变量锁定的配置不会被覆盖。",
    },
    "guangya.directory_scrape.run": {
        "action": "执行光鸭目录刮削预览",
        "object": "当前会话最近生成并冻结的刮削计划",
        "impact": "会把计划提交到光鸭整理队列，并可能移动和重命名选中范围内的云盘文件。",
        "reversibility": "执行前会再次核对内容和计划；已经完成的云盘移动不能自动撤销。",
    },
    "guangya.organize.run_once": {
        "action": "立即执行一次光鸭整理",
        "object": "当前已配置的整理来源",
        "impact": "会按现有规则扫描、识别并移动或整理匹配内容。",
        "reversibility": "部分移动操作可能无法自动回滚，建议先核对预检结果。",
    },
    "guangya.organize.stop": {
        "action": "停止光鸭整理任务",
        "object": "当前正在运行的整理任务",
        "impact": "会协作停止后续文件处理，正在提交的单个操作可能先完成。",
        "reversibility": "停止后可重新启动；已经完成的移动不会自动回滚。",
    },
    "guangya.organize.cleanup.execute": {
        "action": "清理光鸭整理残留",
        "object": "最近预览并逐项复核后的真空目录和残留目录",
        "impact": "真空目录会在双重复核后进入回收站；仅明确选择隔离的非空残留目录会整体移动到 MediaFlux 隔离区。",
        "reversibility": "隔离目录可手工移回；空目录可从光鸭回收站恢复。明确保留、未完成复核、含媒体元数据或快照变化的目录不会进入任务。",
    },
    "ingest.submit": {
        "action": "提交资源接入任务",
        "object": "最近检查并冻结的下载直链、光鸭分享或资源候选",
        "impact": "会按确认页列出的来源、候选序号和目标创建幂等下载或转存请求；不会在确认页暴露链接、访问令牌、云端 file_id 或内部下载句柄。",
        "reversibility": "已发出的下载或转存请求无法从 Agent 撤回；可在对应下载器或光鸭任务中暂停、删除或人工处理。",
    },
    "library.trigger_patrol_now": {
        "action": "立即排队全库缺集巡检",
        "object": "当前巡检策略对应的后台单例任务",
        "impact": "只会把任务排到现在并唤醒调度器；不会修改策略、搜索资源或下载。",
        "reversibility": "已开始的只读巡检不会在此动作中取消；重复请求不会创建第二个任务。",
    },
    "library.set_patrol_policy": {
        "action": "更新全库缺集巡检策略",
        "object": "缺集巡检的启用、通知、间隔或单轮上限",
        "impact": "会影响后续自动巡检与通知，不会立即扫描全库。",
        "reversibility": "可再次修改策略恢复；保存后下一轮巡检使用新配置。",
    },
    "library.start_episode_audit": {
        "action": "开始后台全库剧集检查",
        "object": "当前媒体服务器中的剧集目录",
        "impact": "会在后台分批检查整个媒体库并核对 TMDB 已播集数；每批有上限，但任务会持续到全库完成。不会搜索资源、创建下载或修改媒体文件。",
        "reversibility": "任务支持保存进度与安全取消；取消时当前有界批次可能先完成。",
    },
    "agent.cancel_job": {
        "action": "取消后台全库剧集检查",
        "object": "当前会话选中的后台检查任务",
        "impact": "尚未运行的任务会立即取消；运行中的任务会在当前有界批次结束后停止。",
        "reversibility": "不会删除媒体文件或已保存的只读结果；需要时可重新发起检查。",
    },
}


def normalize_confirmation_timestamp(value: Any) -> str:
    """规范化带时区的 ISO 时间；无效或缺失时返回空字符串。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 80:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed.tzinfo is None or offset is None:
        return ""
    return parsed.isoformat(timespec="seconds")


def _build_preflight_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    normalized = normalize_confirmation_timestamp(value)
    if not normalized:
        raise ValueError("invalid confirmation preflight timestamp")
    return normalized


def _safe_contract_text(value: Any, *, fallback: str) -> str:
    return sanitize_public_text(value, limit=128) or fallback


def build_confirmation_contract(
    *,
    tool_name: str,
    risk: RiskLevel,
    preview: ToolResult,
    preflight_at: str | None = None,
) -> dict[str, Any]:
    """为一次预检生成可持久化、版本化且不含原始参数的确认文案。"""
    normalized_name = str(tool_name or "").strip()
    copy = _CONFIRMATION_COPY.get(normalized_name, {})
    label = public_tool_label(normalized_name)
    contract = {
        "version": CONTRACT_VERSION,
        "action": _safe_contract_text(copy.get("action"), fallback=label),
        "object": _safe_contract_text(
            copy.get("object"), fallback="当前预检选中的对象"
        ),
        "impact": _safe_contract_text(
            copy.get("impact"), fallback="确认后会执行服务端预检通过的受控操作。"
        ),
        "reversibility": _safe_contract_text(
            copy.get("reversibility"), fallback="执行后可能需要在对应功能页手动撤销。"
        ),
        "preflight_at": _build_preflight_timestamp(preflight_at),
        "risk": risk.value,
    }
    # 预检摘要只作为补充说明，完全通过公开文本净化；不安全时直接省略。
    preview_summary = sanitize_public_text(preview.summary, limit=160)
    if preview_summary:
        contract["preflight_summary"] = preview_summary
    return contract


def sanitize_confirmation_contract(value: Any) -> dict[str, Any]:
    """重新投影已有契约，供 API、Telegram 和审计记录安全复用。"""
    if not isinstance(value, Mapping):
        return {}
    raw_version = value.get("version")
    if type(raw_version) is not int or raw_version != CONTRACT_VERSION:
        return {}
    risk = str(value.get("risk") or "").strip().lower()
    preflight_at = normalize_confirmation_timestamp(value.get("preflight_at"))
    if risk not in {"low_write", "write", "danger"} or not preflight_at:
        return {}
    projected = {
        "version": CONTRACT_VERSION,
        "action": _safe_contract_text(value.get("action"), fallback="执行受控操作"),
        "object": _safe_contract_text(
            value.get("object"), fallback="当前预检选中的对象"
        ),
        "impact": _safe_contract_text(
            value.get("impact"), fallback="确认后会执行服务端预检通过的受控操作。"
        ),
        "reversibility": _safe_contract_text(
            value.get("reversibility"), fallback="执行后可能需要在对应功能页手动撤销。"
        ),
        "preflight_at": preflight_at,
        "risk": risk,
    }
    preview_summary = sanitize_public_text(value.get("preflight_summary"), limit=160)
    if preview_summary:
        projected["preflight_summary"] = preview_summary
    return projected
