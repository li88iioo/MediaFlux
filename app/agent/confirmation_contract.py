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
    "rss.download": {
        "action": "提交指定 RSS 条目",
        "object": "本次预检冻结的精确待处理条目集合",
        "impact": "会在 订阅目标下载器 创建下载任务并更新条目状态。",
        "reversibility": "可在下载器中暂停或删除任务；已发出的提交请求无法撤回。",
    },

    "rss.retry": {
        "action": "重试 RSS 失败条目",
        "object": "本次预检判定可安全重试的条目",
        "impact": "会再次向各条目指定的下载器提交任务，并更新失败条目的处理状态。",
        "reversibility": "可在下载器中暂停或删除任务；已发出的重试请求无法撤回。",
    },

    "rss.refresh": {
        "action": "立即刷新 RSS 订阅",
        "object": "你选择的订阅源，或全部已配置的订阅源",
        "impact": "会逐个抓取一次最新条目并更新订阅记录，不会自动创建下载任务。",
        "reversibility": "刷新请求无法撤回；新增条目仍可在订阅页查看和处理。",
    },

    "rss.mark": {
        "action": "标记 RSS 条目",
        "object": "本次预检选中的精确 RSS 条目",
        "impact": "只会更新本地处理状态，不会删除订阅、下载任务或媒体文件。",
        "reversibility": "可对仍允许修改的条目再次确认恢复处理状态。",
    },

    "media.refresh": {
        "action": "刷新媒体条目", "object": "指定服务器的真实电影、剧集或单集",
        "impact": "只对确认页列出的条目提交增量刷新；受理不代表完成，不刷新全库。",
        "reversibility": "已受理的刷新无法撤回；结果未知时不可重复提交。",
    },
    "library.refresh": {
        "action": "媒体库刷新",
        "object": "确认卡中指定的服务器和媒体库",
        "impact": "只提交该库的后台刷新，不退回全库扫描；已受理不代表刷新已完成。",
        "reversibility": "已受理的刷新请求不能撤回；结果未知时应查询服务器状态，不自动重复提交。",
    },
    "recognition.save_release_format": {
        "action": "保存发布格式教学",
        "object": "本次核对过的格式、样本和适用范围",
        "impact": "只保存以后识别使用的字段规则，不立即扫描、改名或移动文件，不绑定作品身份或改变季集映射。",
        "reversibility": "可在整理规则的高级手动配置中停用或删除；不会撤销既往整理结果。",
    },
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
    "activity.reorganize": {
        "action": "纠正识别并整理入库",
        "object": "指定整理日志中的视频及伴随文件",
        "impact": "按预览的媒体身份和季集移动、改名，更新原整理日志与待确认状态，并联动已配置的 STRM。",
        "reversibility": "保留逐项操作记录；回退需重新核验当前文件和目标目录，已发出的通知不能撤回。",
    },
    "activity.undo": {
        "action": "可逆操作回退",
        "object": "本次凭证绑定的媒体偏好或光鸭最近可逆文件操作",
        "impact": "只恢复已记录快照，拒绝覆盖后续更改；文件回退排入原整理执行器，不代表已经完成。",
        "reversibility": "不保证能再次撤销回退，不恢复永久删除、已提交下载或已发送通知。",
    },
    "library.set_played": {
        "action": "修改媒体已看状态",
        "object": "当前媒体用户及预检的单个条目",
        "impact": "只改变确认卡所示的已看或收藏状态，写前复核状态并写后回读，不隐式展开整个剧集。",
        "reversibility": "可再次修改标记，但不能保证恢复原观看进度、播放计数或时间记录。",
    },
    "library.set_favorite": {
        "action": "修改媒体收藏状态",
        "object": "当前媒体用户及预检的单个条目",
        "impact": "只改变确认卡所示的已看或收藏状态，写前复核状态并写后回读，不隐式展开整个剧集。",
        "reversibility": "可再次修改标记，但不能保证恢复原观看进度、播放计数或时间记录。",
    },
    "library.create_playlist": {
        "action": "创建播放列表",
        "object": "已确认的播放列表及成员",
        "impact": "只创建私有列表或增减列表成员，核对完整快照和写后结果；不删除媒体文件。",
        "reversibility": "可通过列表成员操作调整，但不保证恢复原成员次序；未知执行结果需先回读。",
    },
    "library.add_to_playlist": {
        "action": "添加播放列表成员",
        "object": "已确认的播放列表及成员",
        "impact": "只创建私有列表或增减列表成员，核对完整快照和写后结果；不删除媒体文件。",
        "reversibility": "可通过列表成员操作调整，但不保证恢复原成员次序；未知执行结果需先回读。",
    },
    "library.remove_from_playlist": {
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
    "recognition.create_knowledge": {
        "action": "新增识别知识",
        "object": "发布组或尾部制作组识别知识",
        "impact": "只改指定用户知识条目或别名，不修改内置条目来源，也不批量重新整理已有媒体。",
        "reversibility": "可通过原知识配置界面修改；删除前请核对内容，不承诺通用撤销。",
    },
    "recognition.update_knowledge": {
        "action": "修改识别知识",
        "object": "发布组或尾部制作组识别知识",
        "impact": "只改指定用户知识条目或别名，不修改内置条目来源，也不批量重新整理已有媒体。",
        "reversibility": "可通过原知识配置界面修改；删除前请核对内容，不承诺通用撤销。",
    },
    "recognition.delete_knowledge": {
        "action": "删除识别知识",
        "object": "发布组或尾部制作组识别知识",
        "impact": "只改指定用户知识条目或别名，不修改内置条目来源，也不批量重新整理已有媒体。",
        "reversibility": "可通过原知识配置界面修改；删除前请核对内容，不承诺通用撤销。",
    },
    # 历史卡片仅保留可读说明；旧动作不再可执行。
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
    "local.create_source": {
        "action": "新增本地媒体来源",
        "object": "按真实source_id冻结的本地媒体来源配置",
        "impact": "复用本地来源生命周期修改配置，不主动整理或删除媒体文件；新来源默认仅预览且不开启qB自动接管。",
        "reversibility": "可重新配置，但删除条目的编号或历史关联不保证恢复。",
    },
    "local.update_source": {
        "action": "修改本地媒体来源",
        "object": "按真实source_id冻结的本地媒体来源配置",
        "impact": "复用本地来源生命周期修改配置，不主动整理或删除媒体文件；新来源默认仅预览且不开启qB自动接管。",
        "reversibility": "可重新配置，但删除条目的编号或历史关联不保证恢复。",
    },
    "local.delete_source": {
        "action": "删除本地媒体来源",
        "object": "按真实source_id冻结的本地媒体来源配置",
        "impact": "复用本地来源生命周期修改配置，不主动整理或删除媒体文件；新来源默认仅预览且不开启qB自动接管。",
        "reversibility": "可重新配置，但删除条目的编号或历史关联不保证恢复。",
    },
    "library.create_path_mapping": {
        "action": "新增媒体路径映射",
        "object": "指定媒体服务器的路径前缀映射",
        "impact": "使用Web相同的配置锁更新映射，保留其他服务器配置及凭据；不移动文件或触发扫描。",
        "reversibility": "可再次配置原映射；已发出的刷新请求不受本操作撤回。",
    },
    "library.update_path_mapping": {
        "action": "修改媒体路径映射",
        "object": "指定媒体服务器的路径前缀映射",
        "impact": "使用Web相同的配置锁更新映射，保留其他服务器配置及凭据；不移动文件或触发扫描。",
        "reversibility": "可再次配置原映射；已发出的刷新请求不受本操作撤回。",
    },
    "library.delete_path_mapping": {
        "action": "删除媒体路径映射",
        "object": "指定媒体服务器的路径前缀映射",
        "impact": "使用Web相同的配置锁更新映射，保留其他服务器配置及凭据；不移动文件或触发扫描。",
        "reversibility": "可再次配置原映射；已发出的刷新请求不受本操作撤回。",
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

    "downloads.pause": {"action": "暂停 qB 下载任务", "object": "你选定的 qB 任务", "impact": "只对确认页列出的任务执行控制，不创建新下载；执行后按真实状态读回核验。", "reversibility": "可通过对应的恢复或暂停操作改变任务状态。"},
    "downloads.resume": {"action": "恢复 qB 下载任务", "object": "你选定的 qB 任务", "impact": "只对确认页列出的任务执行控制，不创建新下载；执行后按真实状态读回核验。", "reversibility": "可通过对应的恢复或暂停操作改变任务状态。"},
    "downloads.remove": {"action": "移除 qB 任务并保留文件", "object": "你选定的 qB 任务", "impact": "先停止关联 MF 未完成下载的本地跟踪，再移除选定 qB 任务；始终保留已下载文件。", "reversibility": "不会自动重新添加移除的任务。"},
    **dict.fromkeys(("downloads.retry_submission", "downloads.retry"), {
        "action": "重新提交下载请求",
        "object": "你指定的一条下载待处理记录",
        "impact": "会使用服务端保留的原始请求，补投原请求的失败目标或创建后继下载请求；不会重投结果未知的目标，也不会在确认页暴露链接、种子、路径或凭据。",
        "reversibility": "已发出的下载请求无法撤回；提交失败时原记录会继续保留在待处理列表。",
    }),
    "media.set_subscription_enabled": {
        "action": "切换媒体追更状态",
        "object": "你指定的一条媒体追更订阅",
        "impact": "暂停会停止安排新检查并失效尚未提交的候选资源；恢复会让订阅重新进入检查队列。已提交下载任务和媒体文件不受影响。",
        "reversibility": "可再次确认暂停或恢复该订阅。",
    },
    'subscriptions.update': {
        "action": "修改媒体追更策略",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会更新后续追更范围、动作模式、下载目标或检查周期，并失效旧候选；若启用自动模式，后续匹配资源会无需再次确认自动提交。本操作不会主动发起检查。",
        "reversibility": "可再次修改后续策略；已进入提交阶段或已经提交的下载任务无法由本次修改撤回。",
    },
    "media.set_subscription_policy": {
        "action": "修改媒体追更策略",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会更新后续追更范围、动作模式、下载目标或检查周期，并失效旧候选；若启用自动模式，后续匹配资源会无需再次确认自动提交。本操作不会主动发起检查。",
        "reversibility": "可再次修改后续策略；已进入提交阶段或已经提交的下载任务无法由本次修改撤回。",
    },
    **dict.fromkeys(("media.set_preferences", "preferences.update"), {
        "action": "保存媒体偏好",
        "object": "当前登录用户的显式媒体偏好",
        "impact": "会保存媒体服务器、下载目标及画质/语言/题材/观看过滤偏好，供后续推荐和选源使用；单次明确要求优先。不会修改系统全局配置或立即创建下载。",
        "reversibility": "可再次修改，或确认清除后恢复产品默认值。",
    }),
    **dict.fromkeys(("media.clear_preferences", "preferences.clear"), {
        "action": "清除媒体偏好",
        "object": "当前登录用户已保存的显式媒体偏好",
        "impact": "会删除当前用户的偏好记录并恢复产品默认值；不会修改媒体服务器、下载器或已有任务。",
        "reversibility": "清除后可重新保存显式偏好。",
    }),
    'subscriptions.set_notification': {
        "action": "修改媒体追更通知规则",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会影响后续缺集、已满足或检查异常事件是否发送 Telegram 通知；不会立即巡检、下载或修改追更策略。",
        "reversibility": "可再次修改开关，或重置为默认关闭规则。",
    },
    "media.set_subscription_notification_rule": {
        "action": "修改媒体追更通知规则",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会影响后续缺集、已满足或检查异常事件是否发送 Telegram 通知；不会立即巡检、下载或修改追更策略。",
        "reversibility": "可再次修改开关，或重置为默认关闭规则。",
    },
    'subscriptions.reset_notification': {
        "action": "重置媒体追更通知规则",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会删除该订阅的显式通知规则并恢复默认关闭状态；已发送通知不会撤回。",
        "reversibility": "可再次确认创建新的通知规则。",
    },
    "media.reset_subscription_notification_rule": {
        "action": "重置媒体追更通知规则",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会删除该订阅的显式通知规则并恢复默认关闭状态；已发送通知不会撤回。",
        "reversibility": "可再次确认创建新的通知规则。",
    },
    "subscriptions.create": {
        "action": "创建媒体追更订阅", "object": "确认卡中的精确媒体、季度、目标与策略",
        "impact": "创建或恢复订阅并安排后续检查，不会立即搜索资源或提交下载；若选择auto，之后匹配资源无需再次确认即可持续提交。只沿已有整理/STRM联动。",
        "reversibility": "可暂停或软删除；已提交下载与媒体文件不受影响。",
    },
    "media.create_subscription": {
        "action": "创建媒体追更订阅",
        "object": "你刚才选定的影视条目和季度",
        "impact": "会创建一条追更订阅并安排后续更新检查；不会立即搜索资源或提交下载。",
        "reversibility": "可暂停或删除这条订阅；已经提交的下载任务和媒体文件不受影响。",
    },
    'subscriptions.delete': {
        "action": "删除媒体追更订阅",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会删除订阅记录并停止后续更新检查；不会删除已提交的下载任务或媒体文件。",
        "reversibility": "可通过同媒体创建恢复订阅；已失效的未提交工作不恢复，已提交下载与文件不受影响。",
    },
    "media.delete_subscription": {
        "action": "删除媒体追更订阅",
        "object": "你指定的一条媒体追更订阅",
        "impact": "会删除订阅记录并停止后续更新检查；不会删除已提交的下载任务或媒体文件。",
        "reversibility": "删除不可撤销；如果只想暂时停止追更，请改为暂停订阅。",
    },
    "media.confirm_mapping": {
        "action": "确认影视身份映射",
        "object": "本次预检冻结的来源身份与显式 TMDB 目标",
        "impact": "会保存一条确认过的跨来源身份映射；不会收藏、订阅、搜索资源或下载。",
        "reversibility": "可通过后续显式确认改正为另一候选；本次写入不会改动媒体文件。",
    },
    **dict.fromkeys(("discovery.add_watchlist", "watchlist.add"), {
        "action": "加入探索收藏",
        "object": "最近探索结果中预检通过的一个影视条目",
        "impact": "只会写入本地探索收藏记录，不会搜索资源、创建订阅或提交下载。",
        "reversibility": "可再次确认移除该收藏；媒体文件和下载任务不受影响。",
    }),
    **dict.fromkeys(("discovery.remove_watchlist", "watchlist.remove"), {
        "action": "移除探索收藏",
        "object": "你按精确收藏编号选择的一条本地收藏记录",
        "impact": "只会删除本地收藏记录，不会删除媒体文件、订阅或下载任务。",
        "reversibility": "移除后不能自动恢复，但可重新搜索并再次加入收藏。",
    }),
    "resources.configure_sites": {
        "action": "更新资源检索站点范围",
        "object": "MediaFlux 的资源站选择",
        "impact": "后续资源检索只会使用新选择的站点，不会立即发起搜索或下载。",
        "reversibility": "可随时再次修改站点选择恢复。",
    },
    "settings.set_feature": {
        "action": "切换项目功能状态",
        "object": "本次预检指向的功能",
        "impact": "保存后会改变该功能的可用状态；不会执行下载或整理任务。",
        "reversibility": "可再次切换回原状态；环境变量锁定的配置不会被覆盖。",
    },
    "settings.set_policy": {
        "action": "更新安全白名单策略",
        "object": "本次预检指定的一项非敏感策略",
        "impact": "保存后只会改变该策略及相关运行时缓存；登录壁纸策略可能在后台请求 TMDB 刷新图片，不会下载媒体、整理文件或读取凭据。",
        "reversibility": "可再次确认改回原值；由环境变量管理的配置不会被覆盖。",
    },
    "settings.test_notification": {
        "action": "发送 Telegram 测试通知",
        "object": "当前已配置的 Telegram 通知会话",
        "impact": "只会发送一条固定的 MediaFlux 连接测试消息；不会附带业务数据、配置内容、Token 或 Chat ID。",
        "reversibility": "消息发送后不能由 MediaFlux 撤回；如需移除，请在 Telegram 中手动删除。",
    },
    "playback.set_enabled": {
        "action": "切换媒体反代实例状态",
        "object": "你按真实instance_id指定并冻结配置的媒体反代实例",
        "impact": "启用会按已保存配置启动反代监听；停用可能中断正在通过该实例播放的媒体。不会修改或展示地址、端口、路径与凭据。",
        "reversibility": "可再次确认切换回原状态；已中断的播放会话不会自动恢复。",
    },
    "playback.restart": {
        "action": "重启媒体反代实例",
        "object": "你按真实instance_id指定并冻结配置的已启用媒体反代实例",
        "impact": "会清理该实例短时直链缓存并重建运行时，正在播放的会话可能短暂中断；不会修改实例配置。",
        "reversibility": "运行时重建无法撤回，但实例仍使用原配置；失败时可重启 MediaFlux 恢复。",
    },
    "local.scan_sources": {
        "action": "扫描本地媒体来源",
        "object": "本次预检冻结的已配置本地媒体来源",
        "impact": "会把发现的媒体加入本地整理队列；后续文件操作继续服从现有来源与归档规则。",
        "reversibility": "扫描本身不修改文件；已入队任务沿既有调度器和归档规则执行，关闭qB接管不会取消手动扫描任务。",
    },
    "local.retry": {
        "action": "重试本地媒体任务或修正其季集映射",
        "object": "按真实task_id冻结的一项失败或待人工确认任务",
        "impact": "普通重试会原子重新排队；明确提供季集时会先更新该任务映射，再由现有调度器按当前来源、整理规则和运行模式重新处理。实际移动及媒体库联动以执行结果为准；不会开放任意路径改名，也不会修改其他任务。",
        "reversibility": "确认前不修改文件；若当前运行模式会执行归档，任务真正写入后不会自动回滚，映射仍不正确时需再次人工纠偏。",
    },
    "local.refresh_library": {
        "action": "精准刷新绑定媒体库",
        "object": "真实task_id对应的已完成本地媒体任务唯一绑定的媒体服务器和媒体库",
        "impact": "只提交受限路径刷新；无法唯一定位会安全停止，绝不会退化为全库刷新。",
        "reversibility": "刷新请求无法撤回，但不会移动、覆盖或删除媒体文件。",
    },
    "recognition.set_rule_enabled": {
        "action": "识别规则启停",
        "object": "你按规则类型和真实 ID 指定的一条识别规则",
        "impact": "只会改变该规则是否参与后续识别；不会修改规则内容、匹配表达式、TMDB 映射、别名或优先级。",
        "reversibility": "可再次确认切换回原状态；已经完成的识别结果不会自动重跑。",
    },
    "strm.retry_failures": {
        "action": "重试 STRM 失败项",
        "object": "本次明确选择并冻结的真实失败 ID",
        "impact": "按冻结配置重新生成或补齐所选 STRM 内容，并更新失败状态；其余记录不重试。",
        "reversibility": "可停止后续处理；已成功写入的结果不会自动回滚。",
    },
    "strm.run_once": {
        "action": "立即执行一次 STRM 同步",
        "object": "当前已配置的 STRM 来源",
        "impact": "会扫描来源并按现有规则创建或更新 STRM 内容。",
        "reversibility": "可停止后续运行；本次已写入的结果不会自动回滚。",
    },
    "strm.run": {
        "action": "立即执行一次 STRM 同步",
        "object": "当前已配置的 STRM 来源",
        "impact": "会扫描来源并按现有规则创建或更新 STRM 内容。",
        "reversibility": "可停止后续运行；本次已写入的结果不会自动回滚。",
    },
    "strm.metadata.set_enabled": {
        "action": "调整伴随元数据同步开关",
        "object": "云盘NFO、字幕、海报等伴随文件同步，不是Jellyfin自身刮削",
        "impact": "关闭后不再领取后续任务，后续启动的同步不再新增；已运行扫描可能继续入队、当前任务可能完成。开启后可继续处理积压。",
        "reversibility": "保留队列和所有落盘文件，可再次确认切换开关；已完成同步不会自动回滚。",
    },
    'strm.set_metadata_enabled': {
        "action": "调整伴随元数据同步开关",
        "object": "云盘NFO、字幕、海报等伴随文件同步，不是Jellyfin自身刮削",
        "impact": "关闭后不再领取后续任务，后续启动的同步不再新增；已运行扫描可能继续入队、当前任务可能完成。开启后可继续处理积压。",
        "reversibility": "保留队列和所有落盘文件，可再次确认切换开关；已完成同步不会自动回滚。",
    },
    "strm.metadata.cancel_pending": {
        "action": "取消伴随元数据历史积压",
        "object": "本次预览冻结的排队及等待重试任务",
        "impact": "确认后仅标记这些待办为取消；不删除NFO、字幕、海报、STRM或媒体，不影响正在运行任务、预览后新增任务或Jellyfin已有信息。",
        "reversibility": "不会开启同步或更改开关；取消记录保留。以后重新开启并扫描可能再次产生待办，本次不自动恢复队列。",
    },
    'strm.cancel_metadata_pending': {
        "action": "取消伴随元数据历史积压",
        "object": "本次预览冻结的排队及等待重试任务",
        "impact": "确认后仅标记这些待办为取消；不删除NFO、字幕、海报、STRM或媒体，不影响正在运行任务、预览后新增任务或Jellyfin已有信息。",
        "reversibility": "不会开启同步或更改开关；取消记录保留。以后重新开启并扫描可能再次产生待办，本次不自动恢复队列。",
    },
    **dict.fromkeys(("strm.set_schedule_policy", "strm.set_schedule"), {
        "action": "更新 STRM 定时同步策略",
        "object": "定时同步的启用状态、计划表达式或任务通知",
        "impact": "会重新安排后续定时同步，不会立即启动或中断同步任务。",
        "reversibility": "可再次修改策略恢复；环境变量锁定的配置不会被覆盖。",
    }),
    "cloud.change": {
        "action": "执行光鸭文件变更",
        "object": "本次确认卡冻结的完整改名、移动、复制、回收或新建目录计划",
        "impact": "准备确认卡不写云端；确认后只执行卡中固定的完整计划，写前复核快照与冲突，回收仅进入光鸭回收站，写后逐项验证。",
        "reversibility": "回收项可在光鸭回收站恢复；复制会保留原对象；改名、移动和新建目录需依据私有执行清单手工恢复。",
    },
    **dict.fromkeys(("guangya.recycle.restore", "cloud.restore"), {
        "action": "恢复光鸭回收站对象",
        "object": "刚才回收站快照中选定的对象",
        "impact": "只恢复确认计划冻结的对象；确认时会重新核对登录凭据、对象名称、类型、体积和快照状态。",
        "reversibility": "恢复后可再次通过受控文件变更移入回收站；若原目录或同名对象已变化，本次确认会失效。",
    }),
    **dict.fromkeys(("guangya.recycle.clear", "cloud.clear_recycle"), {
        "action": "永久清空光鸭回收站",
        "object": "预检时完整读取并冻结的全部回收站内容",
        "impact": "确认后会永久删除回收站中的全部对象；任一对象或登录凭据发生变化都会使计划失效。",
        "reversibility": "不可撤销，MediaFlux 和光鸭回收站都无法自动恢复被清空的对象。",
    }),
    "cloud.share": {
        "action": "创建光鸭分享",
        "object": "按真实 file_id 冻结的云盘对象和分享策略",
        "impact": "确认后会创建新的外部分享链接，并应用确认页中的有效期、访问码、转存次数和下载权限。",
        "reversibility": "可在后续分享列表中撤销链接；撤销不会删除或移动原始云盘文件。",
    },
    "cloud.revoke_share": {
        "action": "撤销光鸭分享",
        "object": "按真实管理 share_id 冻结的分享链接",
        "impact": "确认后原分享链接将停止提供访问；不会删除、移动或改名分享中的原始云盘对象。",
        "reversibility": "原链接通常不能恢复；如仍需分享，可对原始对象重新创建一个新链接。",
    },
    **dict.fromkeys(("guangya.organize.set_schedule_policy", "cloud.set_organize_schedule"), {
        "action": "更新光鸭定时整理策略",
        "object": "定时整理的启用状态、计划表达式或任务通知",
        "impact": "会重新安排后续定时整理，不会立即启动或中断整理任务。",
        "reversibility": "可再次修改策略恢复；环境变量锁定的配置不会被覆盖。",
    }),
    "cloud.scrape": {
        "action": "按明确媒体身份刮削光鸭文件",
        "object": "真实file_id与provider/external_id/tmdb_id绑定的完整冻结计划",
        "impact": "会把确认的移动、重命名及伴随文件计划提交到持久整理队列。",
        "reversibility": "执行前复验来源、目标、规则及计划；已经完成的云盘移动不能自动撤销。",
    },
    "guangya.directory_scrape.run": {
        "action": "执行光鸭目录刮削预览",
        "object": "当前会话最近生成并冻结的刮削计划",
        "impact": "会把计划提交到光鸭整理队列，并可能移动和重命名选中范围内的云盘文件。",
        "reversibility": "执行前会再次核对内容和计划；已经完成的云盘移动不能自动撤销。",
    },
    "cloud.organize": {
        "action": "立即执行一次光鸭整理",
        "object": "当前已配置的整理来源",
        "impact": "会按现有规则扫描、识别并移动或整理匹配内容。",
        "reversibility": "部分移动操作可能无法自动回滚，建议先核对预检结果。",
    },
    "cloud.stop_organize": {
        "action": "停止光鸭整理任务",
        "object": "run_id 对应的当前整理任务",
        "impact": "会协作停止后续文件处理，正在提交的单个操作可能先完成。",
        "reversibility": "停止后可重新启动；已经完成的移动不会自动回滚。",
    },
    "resources.download": {
        "action": "提交资源下载",
        "object": "确认卡中逐项列出的资源与接收目录",
        "impact": "整批核对后向指定下载器提交；各项独立幂等，部分失败不回滚成功项。",
        "reversibility": "已提交请求可在下载器中处理；取消此确认卡不会提交任何下载。",
    },
    "downloads.submit": {
        "action": "提交下载链接",
        "object": "确认卡中识别的资源和冻结的下载目标",
        "impact": "创建幂等下载请求并提交所选后端；已有活跃请求不会隐式补投失败目标。受理不等于下载或入库完成。",
        "reversibility": "取消确认不会提交下载；已发出请求需在对应下载器处理，未知受理不得自动重投。",
    },
    "cloud.transfer_share": {
        "action": "转存光鸭分享", "object": "确认卡列出的真实文件与冻结的配置目标",
        "impact": "创建幂等转存请求；后续整理和STRM沿用现有配置，转存成功不等于已入库。",
        "reversibility": "取消确认不会转存；已受理或结果未知的请求不得自动重投，请核对原请求。",
    },
    "ingest.submit": {
        "action": "提交资源接入任务",
        "object": "最近检查并冻结的光鸭分享或资源候选",
        "impact": "会按确认页列出的来源、候选序号和目标创建幂等下载或转存请求；不会在确认页暴露链接、访问令牌、云端 file_id 或内部下载句柄。",
        "reversibility": "已发出的下载或转存请求无法从 Agent 撤回；可在对应下载器或光鸭任务中暂停、删除或人工处理。",
    },
    "library.run_patrol": {
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
    "library.start_audit": {
        "action": "开始后台全库剧集检查",
        "object": "当前媒体服务器中的剧集目录",
        "impact": "会在后台分批检查整个媒体库并核对 TMDB 已播集数；每批有上限，但任务会持续到全库完成。不会搜索资源、创建下载或修改媒体文件。",
        "reversibility": "任务支持保存进度与安全取消；取消时当前有界批次可能先完成。",
    },
    "library.cancel_audit": {
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
    normalized_name = (tool_name or "").strip()
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
