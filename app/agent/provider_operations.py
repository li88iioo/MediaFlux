"""MediaFlux 首批 Provider operation 目录。"""
from __future__ import annotations

from app.agent.models import RiskLevel
from app.agent.provider_catalog import ProviderCatalog
from app.agent.provider_models import ProviderOperationSpec

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def build_provider_catalog() -> ProviderCatalog:
    catalog = ProviderCatalog()
    specs = (
        ProviderOperationSpec(
            operation_id="media.system.info",
            provider="media",
            description="读取已配置 Jellyfin 或 Emby 的产品、版本和服务器状态。",
            risk=RiskLevel.READ,
            parameters=_EMPTY,
            result_kind="media_system",
            domains=("media_library", "system"),
            examples=("查看 Jellyfin 版本", "媒体服务器在线吗"),
        ),
        ProviderOperationSpec(
            operation_id="media.items.counts",
            provider="media",
            description="读取媒体服务器中的可播放媒体总数，以及电影、剧集和单集数量。",
            risk=RiskLevel.READ,
            parameters=_EMPTY,
            result_kind="media_counts",
            domains=("media_library",),
            examples=("我的媒体库有多少媒体", "查看 Jellyfin 媒体总数"),
        ),
        ProviderOperationSpec(
            operation_id="media.items.recent_added",
            provider="media",
            description="读取媒体服务器最近入库的电影、剧集或单集，按作品去除重复单集展示，并返回可用的已校验 open_url。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                    },
                },
                "additionalProperties": False,
            },
            result_kind="media_items",
            max_items=20,
            domains=("media_library", "discovery"),
            examples=("最近入库了什么", "查看 Jellyfin 最新添加内容"),
        ),
        ProviderOperationSpec(
            operation_id="media.items.recent_played",
            provider="media",
            description=(
                "读取媒体服务器用户的真实最近播放历史；优先使用明确配置用户，未配置时沿用"
                "服务器默认用户选择。这是 DatePlayed/播放事件，不是继续观看 Resume 列表；条目会返回可用的已校验 open_url。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                    },
                },
                "additionalProperties": False,
            },
            result_kind="media_playback_history",
            max_items=20,
            domains=("media_library", "discovery", "playback"),
            examples=("我最近看了什么", "查看 Emby 最近播放历史"),
        ),
        ProviderOperationSpec(
            operation_id="media.items.recommend_from_library",
            provider="media",
            description=(
                "从 Jellyfin 或 Emby 本地媒体库读取 Genres、Tags、评分和用户播放状态，"
                "结合最近播放题材信号排序，并可排除已播放或已开始的作品；候选会返回可用的已校验 open_url。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "media_type": {
                        "type": "string",
                        "enum": ["any", "movie", "tv"],
                        "default": "any",
                    },
                    "must_match": {
                        "type": "array",
                        "description": (
                            "必须同时满足的独立概念；同义词放在同一项并用 | 连接。"
                        ),
                        "maxItems": 6,
                        "default": [],
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                    "prefer": {
                        "type": "array",
                        "description": "用于排序的软偏好，不要求全部命中。",
                        "maxItems": 12,
                        "default": [],
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                    "exclude": {
                        "type": "array",
                        "maxItems": 8,
                        "default": [],
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                    "min_rating": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                        "default": 0,
                    },
                    "exclude_played": {"type": "boolean", "default": True},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                    },
                },
                "additionalProperties": False,
            },
            result_kind="media_library_recommendations",
            max_items=20,
            timeout_seconds=30,
            domains=("media_library", "discovery", "playback"),
            examples=(
                "从我的媒体库推荐没看过的爆笑动画",
                "结合最近观看记录推荐今晚能直接看的片",
                "心情低落，推荐本地不用动脑的喜剧",
            ),
        ),
        ProviderOperationSpec(
            operation_id="media.items.continue_watching",
            provider="media",
            description="读取媒体服务器用户尚未看完的继续观看 Resume 列表，并返回可用的已校验 open_url。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 8,
                    },
                },
                "additionalProperties": False,
            },
            result_kind="media_continue_watching",
            max_items=20,
            domains=("media_library", "playback"),
            examples=("继续观看", "Jellyfin 还有哪些没看完"),
        ),
        ProviderOperationSpec(
            operation_id="media.libraries.list",
            provider="media",
            description="列出指定媒体服务器中的媒体库，不返回真实目录路径。",
            risk=RiskLevel.READ,
            parameters=_EMPTY,
            result_kind="media_libraries",
            domains=("media_library",),
            examples=(
                "列出 Jellyfin 媒体库",
                "有哪些媒体库",
                "查找动漫媒体库以便统计",
            ),
        ),
        ProviderOperationSpec(
            operation_id="media.library.counts",
            provider="media",
            description=(
                "读取先前列出的指定媒体库中的可播放媒体总数，以及电影、剧集和单集数量；"
                "library_ref 必须来自 media.libraries.list。"
            ),
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["library_ref"],
                "properties": {
                    "library_ref": {
                        "type": "string",
                        "minLength": 8,
                        "maxLength": 64,
                    },
                },
                "additionalProperties": False,
            },
            result_kind="media_library_counts",
            reference_arguments={"library_ref": "media_library"},
            domains=("media_library",),
            examples=("动漫媒体库有多少部剧集", "统计刚才选中的媒体库"),
        ),
        ProviderOperationSpec(
            operation_id="media.items.search",
            provider="media",
            description="使用媒体服务器原生搜索查询电影、剧集或单集，并返回可用的已校验 open_url。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 12},
                },
                "additionalProperties": False,
            },
            result_kind="media_items",
            max_items=24,
            domains=("media_library",),
            examples=("在 Jellyfin 搜索暗芝居", "媒体库里有没有这部剧"),
        ),
        ProviderOperationSpec(
            operation_id="media.series.search",
            provider="media",
            description="在媒体服务器中搜索剧集候选，并返回 TMDB 映射、对象引用和可用的已校验 open_url。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 120},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6},
                },
                "additionalProperties": False,
            },
            result_kind="media_series",
            max_items=16,
            domains=("media_library", "episodes"),
            examples=("搜索媒体库剧集", "查找这部剧的本地条目"),
        ),
        ProviderOperationSpec(
            operation_id="media.series.episodes",
            provider="media",
            description="读取先前选中剧集的本地季集位置，用于集数和缺集核验。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["series_ref"],
                "properties": {
                    "series_ref": {"type": "string", "minLength": 8, "maxLength": 64},
                    "max_episodes": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 500},
                    "include_specials": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            result_kind="media_episodes",
            reference_arguments={"series_ref": "media_series"},
            max_items=500,
            timeout_seconds=30,
            domains=("media_library", "episodes"),
            examples=("查看刚才剧集有哪些集", "核对本地季集"),
        ),
        ProviderOperationSpec(
            operation_id="media.library.refresh",
            provider="media",
            description="精准提交先前选中媒体库的后台刷新，不允许退化为全库刷新。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["library_ref"],
                "properties": {
                    "library_ref": {"type": "string", "minLength": 8, "maxLength": 64},
                },
                "additionalProperties": False,
            },
            result_kind="media_refresh",
            reference_arguments={"library_ref": "media_library"},
            domains=("media_library",),
            examples=("刷新刚才选中的媒体库",),
        ),
        ProviderOperationSpec(
            operation_id="media.item.refresh",
            provider="media",
            description="精准提交先前搜索到的单个媒体条目刷新，不允许全库刷新。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["item_ref"],
                "properties": {
                    "item_ref": {"type": "string", "minLength": 8, "maxLength": 64},
                },
                "additionalProperties": False,
            },
            result_kind="media_refresh",
            reference_arguments={"item_ref": "media_item"},
            domains=("media_library",),
            examples=("刷新刚才选中的媒体条目",),
        ),
        ProviderOperationSpec(
            operation_id="qb.app.version",
            provider="qbittorrent",
            description="读取 qBittorrent 应用和 WebUI API 版本。",
            risk=RiskLevel.READ,
            parameters=_EMPTY,
            result_kind="qb_version",
            domains=("downloads", "system"),
            examples=("查看 qBittorrent 版本",),
        ),
        ProviderOperationSpec(
            operation_id="qb.transfer.info",
            provider="qbittorrent",
            description="读取 qBittorrent 全局传输速度、累计流量和连接状态。",
            risk=RiskLevel.READ,
            parameters=_EMPTY,
            result_kind="qb_transfer",
            domains=("downloads",),
            examples=("qB 现在速度怎么样", "检查下载器连接"),
        ),
        ProviderOperationSpec(
            operation_id="qb.torrents.info",
            provider="qbittorrent",
            description="列出 qBittorrent 下载任务的状态、进度、速度和分类。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 240, "default": ""},
                    "category": {"type": "string", "maxLength": 80, "default": ""},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "additionalProperties": False,
            },
            result_kind="qb_torrents",
            max_items=100,
            domains=("downloads",),
            examples=("检查下载队列", "哪些任务卡住了"),
        ),
        ProviderOperationSpec(
            operation_id="qb.torrents.pause",
            provider="qbittorrent",
            description="暂停或停止先前查询选中的一个或多个 qBittorrent 任务。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["torrent_refs"],
                "properties": {
                    "torrent_refs": {
                        "type": "array", "minItems": 1, "maxItems": 20,
                        "items": {"type": "string", "minLength": 8, "maxLength": 64},
                    },
                },
                "additionalProperties": False,
            },
            result_kind="qb_torrent_change",
            reference_arguments={"torrent_refs": "qb_torrent"},
            max_items=20,
            domains=("downloads",),
            examples=("暂停刚才这些下载任务",),
        ),
        ProviderOperationSpec(
            operation_id="qb.torrents.resume",
            provider="qbittorrent",
            description="恢复或开始先前查询选中的一个或多个 qBittorrent 任务。",
            risk=RiskLevel.LOW_WRITE,
            parameters={
                "type": "object",
                "required": ["torrent_refs"],
                "properties": {
                    "torrent_refs": {
                        "type": "array", "minItems": 1, "maxItems": 20,
                        "items": {"type": "string", "minLength": 8, "maxLength": 64},
                    },
                },
                "additionalProperties": False,
            },
            result_kind="qb_torrent_change",
            reference_arguments={"torrent_refs": "qb_torrent"},
            max_items=20,
            domains=("downloads",),
            examples=("恢复刚才这些下载任务",),
        ),
        ProviderOperationSpec(
            operation_id="qb.torrents.delete_task",
            provider="qbittorrent",
            description="只从 qBittorrent 移除先前查询选中的任务，始终保留已下载文件。",
            risk=RiskLevel.WRITE,
            parameters={
                "type": "object",
                "required": ["torrent_refs"],
                "properties": {
                    "torrent_refs": {
                        "type": "array", "minItems": 1, "maxItems": 20,
                        "items": {"type": "string", "minLength": 8, "maxLength": 64},
                    },
                },
                "additionalProperties": False,
            },
            result_kind="qb_torrent_change",
            reference_arguments={"torrent_refs": "qb_torrent"},
            max_items=20,
            domains=("downloads",),
            examples=("移除刚才这些 qB 任务但保留文件",),
        ),
        ProviderOperationSpec(
            operation_id="qb.torrents.files",
            provider="qbittorrent",
            description="读取先前选中 qB 任务的文件清单，不返回保存目录或任务 hash。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "required": ["torrent_ref"],
                "properties": {
                    "torrent_ref": {"type": "string", "minLength": 8, "maxLength": 64},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                "additionalProperties": False,
            },
            result_kind="qb_files",
            reference_arguments={"torrent_ref": "qb_torrent"},
            max_items=200,
            domains=("downloads",),
            examples=("查看刚才下载任务包含哪些文件",),
        ),
    )
    for spec in (*specs, *media_management_specs()):
        catalog.register(spec)
    return catalog


def media_management_specs() -> tuple[ProviderOperationSpec, ...]:
    """有界质量检查及用户写操作；原子 Schema 也复用于 Kernel 的领域目录。"""
    ref = {"type": "string", "minLength": 8, "maxLength": 64}
    page = {"start_index": {"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 20}}

    def spec(operation, description, properties, required=(), refs=None, risk=RiskLevel.READ, examples=()):
        return ProviderOperationSpec(operation_id=operation, provider="media", description=description, risk=risk,
            parameters={"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
            result_kind="media_quality" if operation == "media.library.quality" else "media_user_state",
            reference_arguments=refs or {}, max_items=25, timeout_seconds=30, domains=("media_library", "playback"), examples=examples)

    items = [
        spec("media.library.quality", "分页只读检查媒体库视频版本、分辨率、中文字幕和服务器报告缺失状态；未知字段保持 unknown，仅本页查重，不将分页样本当全库结果，不播放或探测 STRM。", {**page, "library_ref": ref, "min_resolution": {"type": "integer", "enum": [720, 1080, 2160], "default": 1080}, "subtitle_language": {"type": "string", "enum": ["any", "chinese"], "default": "chinese"}}, required=("library_ref",), refs={"library_ref": "media_library"}, examples=("媒体库有哪些低清或缺中文字幕", "检查重复版本和失效媒体")),
        spec("media.user.inspect", "读取搜索结果中单个媒体条目的已看、收藏和观看进度状态，创建绑定用户的短期状态引用；修改前必须读取。", {"item_ref": ref}, ("item_ref",), {"item_ref": "media_item"}, examples=("这部我看过吗", "准备收藏这部电影")),
        spec("media.playlists.list", "分页列出当前媒体服务器用户可访问的播放列表，并返回短期列表引用。", page, examples=("我的播放列表有哪些",)),
        spec("media.playlist.inspect", "读取播放列表及完整成员快照，最多250项；条目分页展示；返回修改用的列表快照引用和移除用的成员引用。", {**page, "playlist_ref": ref}, ("playlist_ref",), {"playlist_ref": "media_playlist"}, examples=("查看周末看片列表里的电影",)),
    ]
    for operation, action in (("mark_played", "标记已看"), ("mark_unplayed", "标记未看"), ("favorite", "收藏"), ("unfavorite", "取消收藏")):
        items.append(spec(f"media.user.{operation}", f"人工确认后{action}单个媒体条目；item_ref 必须来自 media.user.inspect，不接受搜索结果旧引用。确认前复查状态，写后回读验证，不隐式批量改变整部剧集的观看状态。", {"item_ref": ref}, ("item_ref",), {"item_ref": "media_user_item"}, RiskLevel.LOW_WRITE, (f"{action}刚才的电影",)))
    media_refs = {"type": "array", "items": ref, "minItems": 1, "maxItems": 20, "uniqueItems": True}
    items.extend([
        spec("media.playlist.create", "人工确认后为配置用户创建私有 Jellyfin 视频播放列表；可放入最多20个明确视频，不展开剧集。Emby API密钥无法验证新列表归属时安全拒绝。", {"name": {"type": "string", "minLength": 1, "maxLength": 120}, "item_refs": media_refs}, ("name", "item_refs"), {"item_refs": "media_item"}, RiskLevel.WRITE, ("把这几部电影放入新建的周末看片播放列表",)),
        spec("media.playlist.add_items", "人工确认后把明确视频加入已有播放列表，跳过已经存在的条目；playlist_ref 来自 media.playlist.inspect。列表成员变化须重做确认。", {"playlist_ref": ref, "item_refs": media_refs}, ("playlist_ref", "item_refs"), {"playlist_ref": "media_playlist_snapshot", "item_refs": "media_item"}, RiskLevel.LOW_WRITE, ("把刚才的电影加入周末看片",)),
        spec("media.playlist.remove_items", "人工确认后按成员引用移出播放列表，只改成员关系，绝不删除媒体文件；两个引用均来自 media.playlist.inspect。", {"playlist_ref": ref, "entry_refs": media_refs}, ("playlist_ref", "entry_refs"), {"playlist_ref": "media_playlist_snapshot", "entry_refs": "media_playlist_entry"}, RiskLevel.LOW_WRITE, ("从周末看片列表移除第一部，保留文件",)),
    ])
    return tuple(items)
