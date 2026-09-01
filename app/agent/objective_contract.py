"""单次 Media Agent 请求的最小目标合同。

合同只约束工具范围、调用预算和媒体实体连续性，不携带可执行句柄，
也不改变注册表的风险与确认边界。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
import unicodedata
from typing import Final


@dataclass(frozen=True, slots=True)
class AgentObjectiveContract:
    task_kind: str = "general"
    primary_domains: tuple[str, ...] = ()
    required_sources: tuple[str, ...] = ()
    forbidden_sources: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    entity_terms: tuple[str, ...] = ()
    max_provider_requests: int = 6
    max_tool_rounds: int = 5
    max_tool_calls: int = 8
    max_capabilities: int = 12
    parallel_reads: bool = True
    completion_rule: str = "回答用户当前明确问题；辅助检查失败不得覆盖已经成立的主结论。"

    def prompt_instruction(self) -> str:
        domain_text = "、".join(self.primary_domains) or "当前请求直接相关领域"
        source_text = "、".join(self.required_sources) or "最少必要数据源"
        forbidden_text = "、".join(self.forbidden_sources) or "无额外来源限制"
        entities = "、".join(self.entity_terms) or "从当前消息或唯一安全上下文解析"
        return (
            "本轮目标合同："
            f"任务={self.task_kind}；主领域={domain_text}；必需来源={source_text}；"
            f"禁止来源={forbidden_text}；媒体实体={entities}；"
            f"工具调用最多 {self.max_tool_calls} 次。"
            "只完成当前目标，不扩展为资源搜索、媒体库检查或系统巡检；"
            "除非这些范围已被当前消息明确要求。"
            "同一媒体实体在所有调用中必须保持标题、媒体类型、季度和外部 ID 一致，"
            "不得自行改成相似作品、另一版本或另一平台改编。"
            + self.completion_rule
        )


_RELEASE_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:上线|开播|首播|播出|定档|能看|正片).{0,10}(?:了吗|没有|没|吗|时间|日期|状态)|"
    r"(?:是否|有没有|有无|都|已经|现在|目前).{0,12}(?:上线|开播|首播|播出|定档|能看|正片)",
    re.IGNORECASE,
)
_MEDIA_RECOMMEND_TERMS = (
    r"电影|影片|剧集|电视剧|动画|动漫|综艺|纪录片|美剧|英剧|日剧|韩剧|"
    r"欧美剧|国产剧|国漫|国创|国产动画|科幻|悬疑|喜剧|动作|恐怖|爱情|"
    r"番剧|新剧|新番"
)
_RECOMMEND_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:片荒|有什么好看)|"
    rf"(?:(?:20[0-9]{{2}}|今年|明年|最近|近期).{{0,16}}(?:有|有哪些|推荐).{{0,8}}(?:新剧|新番|新动画|国漫|国创))|"
    rf"(?:推荐|安利|想看|值得看).{{0,24}}(?:{_MEDIA_RECOMMEND_TERMS})|"
    rf"(?:{_MEDIA_RECOMMEND_TERMS}).{{0,24}}"
    rf"(?:推荐|安利|想看|值得看|有什么好看)",
    re.IGNORECASE,
)
_NON_MEDIA_RELEASE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:服务|网站|实例|容器|应用|项目|接口|端口|镜像|版本|bot|agent|api)"
    r".{0,16}(?:上线|发布|部署|启动)",
    re.IGNORECASE,
)
_SERIES_UPDATE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:官方|平台|播到|更新到|更新至|多少集|几集).{0,36}(?:媒体库|本地|缺集|漏集|资源|推送|下载)|"
    r"(?:媒体库|本地|缺集|漏集).{0,36}(?:官方|平台|播到|更新到|更新至|资源|推送|下载)",
    re.IGNORECASE,
)
_TARGETED_MEDIA_UPDATE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:有没有更新|有无更新|是否有更新|有更新吗|更新了吗)",
    re.IGNORECASE,
)
_TARGETED_MEDIA_UPDATE_REJECT = (
    "我关注", "关注的", "全部", "所有", "今天", "最近", "现在", "订阅", "追更",
    "rss", "下载队列", "下载任务", "系统", "项目", "服务", "软件", "应用", "版本",
    "容器", "docker", "compose", "agent", "bot", "mediaflux", "api", "镜像",
    "固件", "驱动", "插件", "代码", "文档", "数据库", "python", "node", "ubuntu",
    "windows", "群晖",
)
_STRM_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"strm.{0,24}(?:状态|进度|历史|记录|结果|正常吗|有问题吗)|"
    r"(?:状态|进度|历史|记录|结果).{0,24}strm",
    re.IGNORECASE,
)
_STRM_SOURCE_LIST_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:有哪些|哪些|列出|查看|看看|显示).{0,20}strm.{0,16}(?:来源|目录)|"
    r"strm.{0,16}(?:有哪些|哪些|来源列表|目录列表|可同步)",
    re.IGNORECASE,
)
_STRM_SOURCE_SYNC_RE: Final[re.Pattern[str]] = re.compile(
    r"strm.{0,40}(?:同步|扫描)|(?:同步|扫描).{0,40}strm",
    re.IGNORECASE,
)
_TECHNICAL_DEPLOYMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:推荐|建议|怎么|怎样|如何).{0,28}(?:docker|compose|容器部署|部署方式|部署方案)|"
    r"(?:docker|compose|容器部署|部署方式|部署方案).{0,28}(?:推荐|建议|怎么|怎样|如何)",
    re.IGNORECASE,
)
_HOST_DRIVE_MAINTENANCE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:扫描|检查|清理|整理).{0,24}(?:[a-z]\s*[:：]?\s*盘|系统盘|宿主机磁盘)|"
    r"(?:[a-z]\s*[:：]?\s*盘|系统盘|宿主机磁盘).{0,24}(?:扫描|检查|清理|整理)",
    re.IGNORECASE,
)
_AMBIGUOUS_LIBRARY_SYNC_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:请|麻烦)?(?:帮我)?(?:同步|刷新|扫描|更新)(?:一下|一次)?"
    r"(?:媒体库|本地媒体库|jellyfin|emby)(?:吧|呢|啊)?[。！？!?]?)$",
    re.IGNORECASE,
)
_ORGANIZE_OBJECT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:整理|归档|刮削).{0,48}(?:目录|文件|视频|剧集|电影|光鸭)|"
    r"(?:目录|文件|视频|剧集|电影|光鸭).{0,48}(?:整理|归档|刮削)",
    re.IGNORECASE,
)
_LOCAL_MEDIA_SCOPE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:本地媒体|本地下载目录|下载目录|本机目录).{0,40}(?:整理|扫描|归档|刮削|任务|状态)|"
    r"(?:整理|扫描|归档|刮削).{0,40}(?:本地媒体|本地下载目录|下载目录|本机目录)",
    re.IGNORECASE,
)
_SYSTEM_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:查看|检查|看看)?(?:系统|项目|mediaflux).{0,28}"
    r"(?:状态|健康|正常吗|是否正常|有问题吗|简报|失败任务|运行任务)|"
    r"^(?:查看|检查|看看)?(?:当前)?运行状态(?:和|以及|并)?(?:最近)?(?:失败|异常|运行中)?任务?$|"
    r"(?:哪些|有什么).{0,10}(?:任务).{0,10}(?:正在运行|运行中)",
    re.IGNORECASE,
)
_INDEXER_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:索引站|资源站|资源搜索).{0,24}(?:正常吗|是否正常|状态|有问题吗|搜不到|失败|不可用)|"
    r"(?:查看|检查|看看|测试).{0,16}(?:索引站|资源站)(?:是否)?(?:正常|可用)?|"
    r"(?:为什么|怎么).{0,20}(?:索引站|资源站).{0,20}(?:搜不到|失败|不可用)",
    re.IGNORECASE,
)
_LIBRARY_WIDE_AUDIT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:巡检|检查|核对|扫描).{0,24}(?:整个|全部|全库|所有).{0,12}(?:媒体库|剧集).{0,24}(?:缺集|漏集|更新)?|"
    r"(?:整个|全部|全库|所有).{0,12}(?:媒体库|剧集).{0,24}(?:巡检|检查|核对|扫描|缺集|漏集)",
    re.IGNORECASE,
)
_LIBRARY_EPISODE_AUDIT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:检查|核对|看看|查一下)?.{1,60}(?:缺几集|缺多少集|漏了几集|有多少集|已有几集)|"
    r"(?:媒体库|jellyfin|emby).{0,50}(?:缺集|漏集|集数)",
    re.IGNORECASE,
)
_LOCAL_MEDIA_SCAN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:扫描|开始扫描|立即扫描).{0,24}(?:全部|所有|第?\s*\d+\s*个?)?.{0,8}(?:本地媒体来源|本地来源)|"
    r"(?:本地媒体来源|本地来源).{0,20}(?:扫描一次|立即扫描|开始扫描)",
    re.IGNORECASE,
)
_LIBRARY_REFRESH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:通知|让)?\s*(?:jellyfin|emby).{0,24}(?:扫描|刷新).{0,20}(?:媒体库|库)|"
    r"(?:扫描|刷新).{0,20}(?:jellyfin|emby).{0,20}(?:媒体库|库)",
    re.IGNORECASE,
)
_PROXY_RESTART_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:重启|重新启动).{0,18}(?:媒体反代|反代实例|jellyfin\s*反代|emby\s*反代)|"
    r"(?:媒体反代|反代实例|jellyfin\s*反代|emby\s*反代).{0,18}(?:重启|重新启动)",
    re.IGNORECASE,
)
_AGENT_CONTROL_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:请|帮我|请帮我|我想|我想要|我要|我需要|想|要)?\s*"
    r"(?:开启|打开|启用|关闭|停用|禁用)\s*(?:agent|智能助手)(?:吧|呢|啊)?[。！？!?]?$",
    re.IGNORECASE,
)
_PROXY_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:302|媒体反代|反代实例|jellyfin\s*反代|emby\s*反代|播放链路).{0,30}"
    r"(?:状态|失败|异常|正常吗|是否正常|是不是正常|有问题吗|诊断|测试|连通)|"
    r"(?:查看|检查|诊断|测试).{0,24}"
    r"(?:302|媒体反代|反代实例|jellyfin\s*反代|emby\s*反代|播放链路)",
    re.IGNORECASE,
)
_AGENT_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:查看|检查|看看)?\s*(?:agent|智能助手).{0,18}(?:状态|能力|能做什么|操作历史|任务)|"
    r"^(?:agent|智能助手).{0,10}(?:开启了吗|启用了吗|正常吗)$",
    re.IGNORECASE,
)
_DISCOVERY_METADATA_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?:搜索|查找|查一下|查询|看看).{{0,24}}(?:{_MEDIA_RECOMMEND_TERMS}|影视|片名).{{0,24}}(?:资料|信息|评分|高分)?|"
    rf"(?:{_MEDIA_RECOMMEND_TERMS}).{{0,24}}(?:资料|影视信息|元数据)|"
    r"(?:查一下|查询|看看).{2,80}(?:资料|影视信息|评分)$",
    re.IGNORECASE,
)
_DOWNLOAD_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:qb|qbittorrent|下载队列|下载任务|光鸭离线|离线任务).{0,30}(?:状态|进度|卡住|异常|失败|正常吗|有哪些|查看)|"
    r"(?:查看|检查|列出|看看).{0,20}(?:下载请求|下载任务|光鸭离线)",
    re.IGNORECASE,
)
_QB_REALTIME_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:qb|qbittorrent).{0,16}(?:实时|当前|现在).{0,12}(?:任务|队列|下载|速度|状态)|"
    r"(?:实时|当前|现在).{0,12}(?:qb|qbittorrent).{0,12}(?:任务|队列|下载|速度|状态)",
    re.IGNORECASE,
)
_MEDIA_LIBRARY_TOTAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:我的|当前|全部)?(?:jellyfin|emby|媒体服务器|媒体库).{0,18}"
    r"(?:媒体总数|媒体数量|总共有多少(?:个|项)?媒体|一共有多少(?:个|项)?媒体|"
    r"有多少(?:个|项)?媒体|多少(?:个|项)?媒体)|"
    r"(?:媒体总数|媒体数量).{0,18}(?:jellyfin|emby|媒体服务器|媒体库)",
    re.IGNORECASE,
)
_RSS_SCOPE_RE: Final[re.Pattern[str]] = re.compile(r"(?:rss|mikan|订阅源)", re.IGNORECASE)
_MEDIA_SUBSCRIPTION_SCOPE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:媒体追更|追更订阅|媒体订阅|(?:我的)?追更)|"
    r"^(?:订阅|追更|加入追更|添加追更|创建订阅).+|"
    r"(?:给|为).{1,80}(?:创建|添加|新建|建立).{0,24}(?:追更|媒体|影视)?订阅",
    re.IGNORECASE,
)
_MEDIA_SUBSCRIPTION_CREATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:请\s*)?(?:帮我\s*)?(?:(?:订阅|追更|加入追更|添加追更|创建订阅).+|"
    r"(?:给|为).{1,80}(?:创建|添加|新建|建立).{0,24}(?:追更|媒体|影视)?订阅)",
    re.IGNORECASE,
)
_CALENDAR_SCOPE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:追番|放送|动画|动漫).{0,10}日历|(?:今天|本周|星期[一二三四五六日天]).{0,16}(?:动画|动漫).{0,10}(?:更新|播出)",
    re.IGNORECASE,
)
_LIBRARY_HEALTH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:jellyfin|emby|媒体库).{0,30}(?:正常吗|健康|状态|连接|连通|有问题吗)",
    re.IGNORECASE,
)
_CONFIG_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:检查|诊断|查看).{0,16}(?:项目|系统|mediaflux)?配置|"
    r"(?:项目|系统|mediaflux)配置.{0,16}(?:正常吗|完整吗|有问题吗)",
    re.IGNORECASE,
)
_STRM_IMPLICIT_SOURCE_SYNC_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:只|仅)?同步.{1,40}来源$|^(?:只|仅)?扫描.{1,40}来源$",
    re.IGNORECASE,
)
_RESOURCE_SEARCH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:搜索|搜一下|查找|找一下|找找).{0,80}(?:资源|种子|磁力)|"
    r"(?:资源|种子|磁力).{0,30}(?:搜索|搜一下|查找|找一下)",
    re.IGNORECASE,
)
_RESOURCE_AVAILABILITY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:有没有|有无|是否有|能否找到).{0,24}(?:资源|种子|磁力)|"
    r"(?:资源|种子|磁力).{0,16}(?:有吗|有没有|存在吗|能找到吗)",
    re.IGNORECASE,
)
_GUANGYA_ORGANIZE_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:查看|检查|看看|查询).{0,20}(?:光鸭)?整理(?:状态|进度|记录|日志)|"
    r"(?:光鸭)?整理(?:状态|进度|记录|日志)",
    re.IGNORECASE,
)
_DOWNLOAD_CONTROL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:暂停|恢复|继续|删除|移除|重试).{0,24}(?:下载任务|下载请求|qb|qbittorrent|光鸭离线|离线任务)|"
    r"(?:下载任务|下载请求|qb|qbittorrent|光鸭离线|离线任务).{0,24}(?:暂停|恢复|继续|删除|移除|重试)",
    re.IGNORECASE,
)
_STRM_SCHEDULE_POLICY_RE: Final[re.Pattern[str]] = re.compile(
    r"strm.{0,28}(?:定时|调度|周期|间隔|计划|每\s*\d+\s*天)|"
    r"(?:定时|调度|周期|间隔|计划|每\s*\d+\s*天).{0,28}strm",
    re.IGNORECASE,
)
_STRM_FAILURE_WORKFLOW_RE: Final[re.Pattern[str]] = re.compile(
    r"strm.{0,24}(?:失败项|失败任务|失败记录|失败).{0,20}(?:重试|处理|诊断|分析|查看)?|"
    r"(?:重试|处理|诊断|分析|查看).{0,20}strm.{0,20}(?:失败项|失败任务|失败记录|失败)",
    re.IGNORECASE,
)
_LIBRARY_PATROL_POLICY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:媒体库|剧集|缺集).{0,24}(?:巡检).{0,24}(?:开启|关闭|间隔|周期|每\s*\d+\s*天|通知|上限|立即|现在)|"
    r"(?:开启|关闭|间隔|周期|每\s*\d+\s*天|立即|现在).{0,24}(?:媒体库|剧集|缺集).{0,16}巡检",
    re.IGNORECASE,
)
_LOCAL_MEDIA_TRIGGER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:本地媒体来源|本地来源).{0,30}(?:qb\s*完成触发|qb\s*下载完成).{0,16}(?:开启|关闭|启用|停用)?|"
    r"(?:开启|关闭|启用|停用).{0,20}(?:本地媒体来源|本地来源).{0,20}(?:qb\s*完成触发|qb\s*下载完成)",
    re.IGNORECASE,
)
_PROXY_CONTROL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:开启|关闭|启用|停用|禁用).{0,18}(?:媒体反代|反代实例|jellyfin\s*反代|emby\s*反代)|"
    r"(?:媒体反代|反代实例|jellyfin\s*反代|emby\s*反代).{0,18}(?:开启|关闭|启用|停用|禁用)",
    re.IGNORECASE,
)
_TELEGRAM_STATUS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:telegram|tg|机器人|bot).{0,18}(?:通知|消息|agent)?.{0,12}"
    r"(?:状态|开启了吗|启用了吗|是否开启|是否启用|正常吗|能用吗)|"
    r"(?:查看|检查|确认).{0,12}(?:telegram|tg|机器人|bot).{0,12}(?:状态|配置)",
    re.IGNORECASE,
)
_TELEGRAM_TEST_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:发送|发|推送|测试|检查).{0,10}(?:telegram|tg).{0,12}(?:测试通知|通知测试|通知|消息)|"
    r"(?:telegram|tg).{0,12}(?:发送|发|推送|测试|检查).{0,10}(?:通知|消息)",
    re.IGNORECASE,
)
_GUANGYA_CLEANUP_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:光鸭|整理来源|执行空间).{0,36}(?:空目录|空媒体目录|残留目录|垃圾图片|图片残留|严格垃圾|垃圾文件)|"
    r"(?:空目录|空媒体目录|残留目录|垃圾图片|图片残留|严格垃圾|垃圾文件).{0,36}(?:光鸭|整理来源|执行空间)",
    re.IGNORECASE,
)
_GUANGYA_RENAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:光鸭|云盘).{0,40}(?:名称|标题|前缀|域名).{0,20}(?:清理|去掉|移除|改名|重命名)|"
    r"(?:清理|去掉|移除|改名|重命名).{0,24}(?:光鸭|云盘).{0,30}(?:名称|标题|前缀|域名)",
    re.IGNORECASE,
)
_OBJECT_SCOPE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)/[^\s，。！？]{1,300}|"
    r"(?:光鸭|云盘)/[^\s，。！？]{1,300}|"
    r"(?:这个|那个|某个|指定|该|刚才的).{0,8}(?:目录|文件|视频|剧集|电影)",
    re.IGNORECASE,
)
_RESOURCE_MARKERS: Final[tuple[str, ...]] = (
    "资源", "种子", "磁力", "torrent", "magnet", "下载", "推送", "qb", "光鸭",
)
_LIBRARY_MARKERS: Final[tuple[str, ...]] = (
    "媒体库", "本地", "本地库", "本地媒体", "jellyfin", "emby", "入库", "本地收录", "已有多少集",
)
_PLAN_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:只|仅|先).{0,4}(?:生成|查看|看看|看一下|给出|做|预览)"
    r".{0,12}(?:计划|预览|方案)|"
    r"(?:不要|别|先别|暂不|暂时不).{0,6}"
    r"(?:执行|开始|整理|同步|移动|改名)",
    re.IGNORECASE,
)
_DEICTIC_ENTITIES: Final[frozenset[str]] = frozenset({
    "我问你", "这部", "这部剧", "这两部", "这两部剧", "这几部", "这几部剧",
    "它", "它们", "那个", "那些", "该剧", "该片", "上面两部", "刚才两部",
})



def _scope_is_negated(text: str, markers: tuple[str, ...]) -> bool:
    alternatives = "|".join(re.escape(marker) for marker in markers)
    return bool(re.search(
        rf"(?:不要|不用|别|无需|不必|不查|不看|不搜).{{0,10}}(?:{alternatives})",
        text,
        re.IGNORECASE,
    ))


def _normalize(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _compact_entity(value: str) -> str:
    text = " ".join(str(value or "").split()).strip(" ，。！？?、:：")
    text = re.sub(r"第\s*[一二三四五六七八九十百零〇两0-9]+\s*季", "", text)
    text = re.sub(r"(?:season\s*[0-9]+)$", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^(?:请|请问|帮我|麻烦|检查|核对|看看|查查|查询|搜索|确认|告诉我|我问你)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip(" ，。！？?、:：")
    if not 1 <= len(text) <= 80 or _normalize(text) in _DEICTIC_ENTITIES:
        return ""
    return text


def _quoted_entities(message: str) -> list[str]:
    return [
        entity
        for entity in (
            _compact_entity(item)
            for item in re.findall(r"[《「『\"']([^》」』\"']{1,100})[》」』\"']", message)
        )
        if entity
    ]


def _release_entities(message: str) -> list[str]:
    scope = re.sub(
        r"(?:现在|目前|已经|是否|有没有|有无|都|分别|请问|帮我|查一下|核对一下)",
        " ",
        message,
        flags=re.IGNORECASE,
    )
    scope = re.sub(
        r"(?:上线|开播|首播|播出|定档|能看|正片).*$",
        "",
        scope,
        flags=re.IGNORECASE,
    )
    return [
        entity
        for entity in (
            _compact_entity(item)
            for item in re.split(r"(?:和|跟|与|、|以及|还有|及|/|，|,)", scope)
        )
        if len(entity) >= 2
    ]


def _targeted_media_update_prefix(message: str) -> str:
    if not _TARGETED_MEDIA_UPDATE_RE.search(message):
        return ""
    prefix = _TARGETED_MEDIA_UPDATE_RE.split(message, maxsplit=1)[0]
    prefix = re.sub(
        r"^(?:请|请问|帮我|麻烦|检查|核对|看看|查查|查询)\s*",
        "",
        prefix,
        flags=re.IGNORECASE,
    )
    prefix = re.sub(
        r"(?:媒体库|本地库|本地媒体|jellyfin|emby)(?:里的|中的|里|中)?",
        " ",
        prefix,
        flags=re.IGNORECASE,
    )
    prefix = prefix.strip(" ，,。！？?、:：")
    prefix = re.sub(
        r"(?:现在|目前|当前|最新)?\s*(?:已经|一共|总共)?\s*"
        r"(?:更新到|更新至|播到|有)?\s*(?:多少|几)\s*集(?:了|呢|吗)?\s*$",
        "",
        prefix,
        flags=re.IGNORECASE,
    )
    compact = " ".join(prefix.split()).strip(" ，,。！？?、:：")
    if not compact or any(marker in compact for marker in _TARGETED_MEDIA_UPDATE_REJECT):
        return ""
    if compact in {"电影", "影片", "剧集", "电视剧", "动画", "动漫", "综艺", "纪录片"}:
        return ""
    return compact if 2 <= len(compact) <= 80 else ""


def _is_targeted_media_update(message: str) -> bool:
    if not _TARGETED_MEDIA_UPDATE_RE.search(message):
        return False
    return bool(_quoted_entities(message) or _targeted_media_update_prefix(message))


def _series_update_entities(message: str) -> list[str]:
    # 组合任务通常以剧名开头，后面接“官方/媒体库/缺集/资源”等检查范围。
    prefix = re.split(
        r"(?:官方|平台|媒体库|本地库|本地媒体|jellyfin|emby|已有多少集|"
        r"有多少集|多少集|缺集|漏集|资源|下载|推送|有没有更新|有无更新|"
        r"是否有更新|有更新吗|更新了吗|缺几集|缺多少集|漏了几集|有多少集|已有几集)",
        message,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    entity = _compact_entity(prefix)
    return [entity] if len(entity) >= 2 else []


def _entity_terms(message: str) -> tuple[str, ...]:
    entities = _quoted_entities(message)
    # 上线/开播问题优先按状态谓词截断；否则“不要查本地和资源”
    # 这类范围否定会被组合剧集规则误当成标题的一部分。
    if not entities and _RELEASE_STATUS_RE.search(message):
        entities = _release_entities(message)
    if not entities and _is_targeted_media_update(message):
        entity = _compact_entity(_targeted_media_update_prefix(message))
        entities = [entity] if entity else []
    if not entities and _SERIES_UPDATE_RE.search(message):
        entities = _series_update_entities(message)
    return tuple(dict.fromkeys(entities))[:6]


def infer_agent_objective(value: object) -> AgentObjectiveContract:
    text = _normalize(value)
    if not text:
        return AgentObjectiveContract()

    explicit_resource = any(marker in text for marker in _RESOURCE_MARKERS)
    explicit_library = any(marker in text for marker in _LIBRARY_MARKERS)
    resource_requested = explicit_resource and not _scope_is_negated(text, _RESOURCE_MARKERS)
    library_requested = explicit_library and not _scope_is_negated(text, _LIBRARY_MARKERS)
    entities = _entity_terms(text)
    targeted_media_update = _is_targeted_media_update(text)

    if _HOST_DRIVE_MAINTENANCE_RE.search(text):
        return AgentObjectiveContract(
            task_kind="host_drive_guidance",
            primary_domains=("guidance",),
            entity_terms=entities,
            max_provider_requests=1,
            max_tool_rounds=0,
            max_tool_calls=0,
            max_capabilities=0,
            parallel_reads=False,
            completion_rule=(
                "MediaFlux 只能处理容器内已挂载并配置的目录；不得假装能够扫描宿主机盘符。"
            ),
        )

    if _AMBIGUOUS_LIBRARY_SYNC_RE.search(text):
        return AgentObjectiveContract(
            task_kind="library_sync_clarification",
            primary_domains=("guidance",),
            entity_terms=entities,
            max_provider_requests=1,
            max_tool_rounds=0,
            max_tool_calls=0,
            max_capabilities=0,
            parallel_reads=False,
            completion_rule=(
                "先澄清用户指的是通知 Jellyfin/Emby 扫描、STRM 同步，还是 RSS/追更刷新；"
                "不得猜测并生成写操作票据。"
            ),
        )

    if _TECHNICAL_DEPLOYMENT_RE.search(text):
        return AgentObjectiveContract(
            task_kind="technical_guidance",
            primary_domains=("guidance",),
            entity_terms=entities,
            max_provider_requests=2,
            max_tool_rounds=1,
            max_tool_calls=0,
            max_capabilities=0,
            completion_rule=(
                "这是说明或方案咨询，不得调用媒体发现、下载、整理或配置写入工具。"
            ),
        )

    if _SYSTEM_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="system_status",
            primary_domains=("system", "jobs"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "workspace.briefing",
                "workspace.health",
                "workspace.todo",
                "workspace.next_actions",
                "automation.diagnose_pipeline",
            ),
            max_provider_requests=5,
            max_tool_rounds=2,
            max_tool_calls=5,
            max_capabilities=5,
            completion_rule="先给整体结论，再只列真正需要处理的异常或运行中事项。",
        )

    if _INDEXER_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="indexer_status",
            primary_domains=("indexer", "resource_search"),
            required_sources=("resource_index",),
            forbidden_sources=("public_web", "local_library", "metadata_catalog"),
            allowed_tools=("indexer.diagnose_readiness",),
            max_provider_requests=2,
            max_tool_rounds=1,
            max_tool_calls=1,
            max_capabilities=1,
            parallel_reads=False,
            completion_rule="只诊断索引配置、站点可用性和搜索准备状态，不自行搜索具体媒体。",
        )

    resource_availability = bool(_RESOURCE_AVAILABILITY_RE.search(text))
    if (_RESOURCE_SEARCH_RE.search(text) or resource_availability) and not (
        _SERIES_UPDATE_RE.search(text) and library_requested
    ):
        allowed = [
            "indexer.search_resources",
            "indexer.diagnose_readiness",
            "ingest.inspect",
            "ingest.submit",
            "ingest.status",
        ]
        required_sources = ["resource_index"]
        primary_domains = ["indexer", "resource_search"]
        subscription_requested = bool(
            _MEDIA_SUBSCRIPTION_SCOPE_RE.search(text) or "订阅更新" in text
        )
        if subscription_requested:
            allowed.insert(0, "media.subscription_updates")
            required_sources.append("system_state")
            primary_domains.insert(0, "subscriptions")
        if any(marker in text for marker in ("缺集", "补集", "漏集")):
            allowed.extend((
                "library.search_missing_episode_resources",
                "library.search_missing_season_resources",
            ))
        return AgentObjectiveContract(
            task_kind=(
                "subscription_resource_search"
                if subscription_requested else (
                    "resource_availability" if resource_availability
                    else "resource_search"
                )
            ),
            primary_domains=tuple(primary_domains),
            required_sources=tuple(required_sources),
            forbidden_sources=("public_web", "metadata_catalog"),
            allowed_tools=tuple(allowed),
            entity_terms=entities,
            max_provider_requests=5 if subscription_requested else 4,
            max_tool_rounds=3,
            max_tool_calls=5 if subscription_requested else 4,
            max_capabilities=len(allowed),
            completion_rule=(
                "先读取追更更新，再按需要搜索可审计资源候选；未选择候选和目标前不得生成下载提交票据。"
                if subscription_requested else (
                    "只回答资源是否存在并给出必要摘要；用户没有明确选择候选和目标时不得生成下载提交票据。"
                    if resource_availability else
                    "只返回可审计资源候选；未选择候选和目标前不得生成下载提交票据。"
                )
            ),
        )

    if _LOCAL_MEDIA_TRIGGER_RE.search(text):
        return AgentObjectiveContract(
            task_kind="local_media_source_control",
            primary_domains=("local_media", "config"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "local_media.source_summaries",
                "local_media.get_source_summary",
                "local_media.set_source_trigger_enabled",
            ),
            max_provider_requests=3,
            max_tool_rounds=2,
            max_tool_calls=3,
            max_capabilities=3,
            parallel_reads=False,
            completion_rule="必须按公开来源序号和明确触发类型生成启停确认；不得修改来源目录、归档目标或其他规则。",
        )

    if _LOCAL_MEDIA_SCAN_RE.search(text):
        return AgentObjectiveContract(
            task_kind="local_media_scan",
            primary_domains=("local_media", "organize"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "local_media.source_summaries",
                "local_media.scan_sources",
                "local_media.task_summaries",
            ),
            max_provider_requests=3,
            max_tool_rounds=3,
            max_tool_calls=3,
            max_capabilities=3,
            parallel_reads=False,
            completion_rule="只扫描全部来源或用户按公开序号明确选择的来源；不得接受任意路径，也不得把指定单个标题扩大为整来源扫描。",
        )

    if _LOCAL_MEDIA_SCOPE_RE.search(text):
        return AgentObjectiveContract(
            task_kind="local_media_workflow",
            primary_domains=("local_media", "organize"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "local_media.diagnose",
                "local_media.source_summaries",
                "local_media.review_queue_summary",
                "local_media.task_summaries",
                "local_media.inspect_task",
                "local_media.preview_task",
                "local_media.scan_sources",
                "local_media.retry_task",
            ),
            entity_terms=entities,
            max_provider_requests=5,
            max_tool_rounds=3,
            max_tool_calls=5,
            max_capabilities=8,
            parallel_reads=False,
            completion_rule=(
                "只处理本地媒体来源与本地整理任务；指定媒体名称时只能按名称筛选已配置来源候选，不得扩大为整来源处理，也不得转去搜索资源。"
            ),
        )

    if _GUANGYA_CLEANUP_RE.search(text):
        return AgentObjectiveContract(
            task_kind="guangya_cleanup_workflow",
            primary_domains=("organize", "cloud_files", "storage_hygiene"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "guangya.organize.cleanup.preview",
                "guangya.organize.cleanup.classify",
                "guangya.organize.cleanup.execute",
            ),
            max_provider_requests=4,
            max_tool_rounds=4,
            max_tool_calls=4,
            max_capabilities=3,
            parallel_reads=False,
            completion_rule="先生成冻结的残留清理计划；用户要求保留某项时必须先修改分类，再对剩余计划生成执行确认。",
        )

    if _GUANGYA_RENAME_RE.search(text):
        return AgentObjectiveContract(
            task_kind="guangya_rename_workflow",
            primary_domains=("organize", "cloud_files", "media_identity"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index"),
            allowed_tools=(
                "guangya.fs.query",
                "guangya.fs.change.preview",
                "guangya.fs.change.execute",
                "guangya.media_hygiene.preview",
                "guangya.rename.preview",
                "guangya.rename.execute",
            ),
            max_provider_requests=5,
            max_tool_rounds=4,
            max_tool_calls=5,
            max_capabilities=8,
            parallel_reads=False,
            completion_rule="先通过通用文件查询读取对象，再生成冻结变更计划；媒体名称清理优先使用专用高置信流程，执行前必须确认。",
        )

    if _GUANGYA_ORGANIZE_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="guangya_organize_status",
            primary_domains=("organize", "jobs"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "guangya.organize.status",
                "guangya.organize.schedule_policy",
                "organize.audit_logs",
            ),
            max_provider_requests=3,
            max_tool_rounds=2,
            max_tool_calls=3,
            max_capabilities=3,
            completion_rule="只回答整理运行态、调度策略和最近结果，不启动新整理。",
        )

    if _AGENT_CONTROL_RE.search(text):
        return AgentObjectiveContract(
            task_kind="agent_control_guidance",
            primary_domains=("agent",),
            required_sources=(),
            forbidden_sources=("public_web", "resource_index", "local_library", "metadata_catalog"),
            allowed_tools=(),
            max_provider_requests=0,
            max_tool_rounds=0,
            max_tool_calls=0,
            max_capabilities=0,
            parallel_reads=False,
            completion_rule="Agent 开关只通过 /agent 独立控制面处理，不生成普通 Agent 确认票据。",
        )

    if _PROXY_CONTROL_RE.search(text):
        return AgentObjectiveContract(
            task_kind="media_proxy_control",
            primary_domains=("playback", "config"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "media_proxy.status_summary",
                "media_proxy.set_instance_enabled",
            ),
            max_provider_requests=2,
            max_tool_rounds=2,
            max_tool_calls=2,
            max_capabilities=2,
            parallel_reads=False,
            completion_rule="必须按公开实例序号生成启停确认；不得修改上游地址、监听端口、路径或凭据。",
        )

    if _PROXY_RESTART_RE.search(text):
        return AgentObjectiveContract(
            task_kind="media_proxy_restart",
            primary_domains=("playback",),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "media_proxy.status_summary",
                "media_proxy.restart_instance",
            ),
            max_provider_requests=2,
            max_tool_rounds=2,
            max_tool_calls=2,
            max_capabilities=2,
            parallel_reads=False,
            completion_rule="先按公开序号锁定已启用实例，再生成重启确认票据；不得用停用/启用替代重启。",
        )

    if _PROXY_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="media_proxy_diagnosis",
            primary_domains=("playback",),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "media_proxy.status_summary",
                "media_proxy.playback_failure_summary",
                "media_proxy.test_instance",
            ),
            max_provider_requests=3,
            max_tool_rounds=2,
            max_tool_calls=3,
            max_capabilities=3,
            completion_rule="区分实例连通、PlaybackInfo、302 原画直链和客户端兼容问题，不混成单一故障。",
        )

    if _AGENT_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="agent_status",
            primary_domains=("agent", "jobs"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "agent.runtime_status",
                "agent.capabilities",
                "agent.action_history",
            ),
            max_provider_requests=2,
            max_tool_rounds=1,
            max_tool_calls=2,
            max_capabilities=3,
            parallel_reads=True,
            completion_rule="只说明当前可用能力和近期受控动作；Agent 开关由 /agent 控制面管理。",
        )

    if _TELEGRAM_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="telegram_status",
            primary_domains=("notifications", "agent"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "local_library", "metadata_catalog"),
            allowed_tools=("agent.runtime_status",),
            max_provider_requests=1,
            max_tool_rounds=1,
            max_tool_calls=1,
            max_capabilities=1,
            parallel_reads=False,
            completion_rule=(
                "只说明 Telegram Agent 接入是否启用；运行状态不能证明消息通道连通，"
                "需要连通测试时应让用户明确发送 Telegram 测试通知。"
            ),
        )

    if _TELEGRAM_TEST_RE.search(text):
        return AgentObjectiveContract(
            task_kind="telegram_test_notification",
            primary_domains=("notifications",),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "local_library", "metadata_catalog"),
            allowed_tools=("telegram.send_test_notification",),
            max_provider_requests=1,
            max_tool_rounds=1,
            max_tool_calls=1,
            max_capabilities=1,
            parallel_reads=False,
            completion_rule="只生成一次 Telegram 测试通知确认，不读取或展示 Bot Token、Chat ID 等配置值。",
        )

    if _CONFIG_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="config_diagnosis",
            primary_domains=("config", "system"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "config.diagnose",
                "config.feature_summary",
                "config.diagnose_media_servers",
                "config.indexer_sites_summary",
                "config.safe_policy_summary",
            ),
            max_provider_requests=5,
            max_tool_rounds=2,
            max_tool_calls=5,
            max_capabilities=5,
            completion_rule="只报告缺失或不一致的配置项，不返回配置值、地址、路径或凭据。",
        )

    if _DOWNLOAD_CONTROL_RE.search(text):
        return AgentObjectiveContract(
            task_kind="download_control",
            primary_domains=("downloads", "jobs"),
            required_sources=("provider_api",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "provider.capabilities",
                "provider.query",
                "provider.change.preview",
                "provider.change.execute",
                "provider.job.status",
                "downloads.request_summaries",
                "downloads.retry_submission",
            ),
            max_provider_requests=6,
            max_tool_rounds=4,
            max_tool_calls=6,
            max_capabilities=7,
            parallel_reads=False,
            completion_rule=(
                "先通过 qBittorrent 原生查询取得 owner 绑定对象引用，再冻结暂停、恢复或仅删除任务的写计划；"
                "删除始终保留文件，不得猜测 hash、名称或内部标识。下载请求重投仍使用项目持久请求记录。"
            ),
        )

    if _QB_REALTIME_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="qb_realtime_status",
            primary_domains=("downloads",),
            required_sources=("provider_api",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=("provider.capabilities", "provider.query"),
            max_provider_requests=4,
            max_tool_rounds=3,
            max_tool_calls=4,
            max_capabilities=2,
            parallel_reads=False,
            completion_rule=(
                "只报告 qBittorrent 原生 API 当前返回的任务、进度、速度和连接状态；"
                "不得用 MediaFlux 历史下载请求代替实时队列。"
            ),
        )

    if _DOWNLOAD_STATUS_RE.search(text):
        qb_only = not any(marker in text for marker in ("光鸭", "离线"))
        qb_tools = (
            "provider.capabilities",
            "provider.query",
            "downloads.request_summaries",
        )
        return AgentObjectiveContract(
            task_kind="download_status",
            primary_domains=("downloads", "jobs"),
            required_sources=(("provider_api",) if qb_only else ("provider_api", "system_state")),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                qb_tools
                if qb_only else qb_tools + (
                    "automation.diagnose_pipeline",
                    "guangya.connection_status",
                )
            ),
            max_provider_requests=5,
            max_tool_rounds=3,
            max_tool_calls=5,
            max_capabilities=3 if qb_only else 5,
            completion_rule=(
                "qB 当前状态必须来自 qBittorrent 原生 API；同时区分光鸭离线、整理和 STRM 阶段，"
                "不把已离线完成误报为仍在下载。"
            ),
        )

    if _MEDIA_LIBRARY_TOTAL_RE.search(text):
        return AgentObjectiveContract(
            task_kind="media_library_counts",
            primary_domains=("media_library",),
            required_sources=("provider_api",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=("provider.capabilities", "provider.query"),
            max_provider_requests=5,
            max_tool_rounds=3,
            max_tool_calls=5,
            max_capabilities=2,
            parallel_reads=False,
            completion_rule=(
                "从已配置媒体服务器的实时计数接口读取可播放项总数；如有细分，"
                "同时报告电影、剧集和单集数量，不得使用云盘文件数或本地数据库记录代替。"
            ),
        )

    if _CALENDAR_SCOPE_RE.search(text):
        include_subscriptions = bool(_MEDIA_SUBSCRIPTION_SCOPE_RE.search(text))
        allowed_tools = ["bangumi.calendar"]
        required_sources = ["metadata_catalog"]
        primary_domains = ["discovery"]
        if include_subscriptions:
            allowed_tools.extend((
                "media.subscription_summaries",
                "media.subscription_updates",
            ))
            required_sources.append("system_state")
            primary_domains.append("subscriptions")
        return AgentObjectiveContract(
            task_kind=(
                "calendar_subscription_overview"
                if include_subscriptions else "bangumi_calendar"
            ),
            primary_domains=tuple(primary_domains),
            required_sources=tuple(required_sources),
            forbidden_sources=("public_web", "resource_index", "local_library"),
            allowed_tools=tuple(allowed_tools),
            max_provider_requests=len(allowed_tools),
            max_tool_rounds=2,
            max_tool_calls=len(allowed_tools),
            max_capabilities=len(allowed_tools),
            completion_rule=(
                "分别展示放送日历与现有媒体追更，不把日历条目误报为已订阅。"
                if include_subscriptions else
                "只返回指定星期的 Bangumi 放送日历，不扩展为资源搜索或自动订阅。"
            ),
        )

    if _RSS_SCOPE_RE.search(text) and _MEDIA_SUBSCRIPTION_SCOPE_RE.search(text):
        return AgentObjectiveContract(
            task_kind="subscription_overview",
            primary_domains=("subscriptions",),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "media.subscription_summaries",
                "media.subscription_updates",
                "rss.subscription_summaries",
                "rss.recent_activity",
                "rss.diagnose",
            ),
            max_provider_requests=5,
            max_tool_rounds=2,
            max_tool_calls=5,
            max_capabilities=5,
            completion_rule="分别汇总媒体追更和 RSS，不把两类订阅混为同一对象。",
        )

    if _RSS_SCOPE_RE.search(text):
        return AgentObjectiveContract(
            task_kind="rss_workflow",
            primary_domains=("subscriptions",),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "rss.diagnose",
                "rss.subscription_summaries",
                "rss.get_subscription_summary",
                "rss.entry_summaries",
                "rss.recent_activity",
                "rss.create_subscription",
                "rss.update_subscription",
                "rss.refresh_subscription",
                "rss.refresh_subscriptions",
                "rss.mark_entries",
                "rss.submit_entries_to_qb",
                "rss.submit_pending_to_qb",
                "rss.retry_failed_to_qb",
                "rss.delete_subscription",
            ),
            max_provider_requests=7,
            max_tool_rounds=4,
            max_tool_calls=7,
            max_capabilities=16,
            parallel_reads=False,
            completion_rule="RSS 订阅源、条目和刷新动作必须保持同一订阅范围；提交、重试、标记和删除都必须锁定公开编号并确认。",
        )

    if _MEDIA_SUBSCRIPTION_SCOPE_RE.search(text):
        creating_subscription = bool(_MEDIA_SUBSCRIPTION_CREATE_RE.search(text))
        if creating_subscription:
            return AgentObjectiveContract(
                task_kind="media_subscription_create",
                primary_domains=("subscriptions", "media_identity"),
                required_sources=("metadata_catalog", "system_state"),
                forbidden_sources=("public_web", "resource_index", "local_library"),
                allowed_tools=("discovery.search", "media.create_subscription"),
                entity_terms=entities,
                max_provider_requests=3,
                max_tool_rounds=3,
                max_tool_calls=3,
                max_capabilities=2,
                parallel_reads=False,
                completion_rule="先搜索并锁定唯一影视身份，再生成创建追更的确认票据；不得只列现有订阅后结束。",
            )
        return AgentObjectiveContract(
            task_kind="media_subscription_workflow",
            primary_domains=("subscriptions", "media_identity"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "resource_index"),
            allowed_tools=(
                "media.subscription_summaries",
                "media.get_subscription_summary",
                "media.get_subscription_policy",
                "media.subscription_updates",
                "discovery.search",
                "media.create_subscription",
                "media.set_subscription_enabled",
                "media.set_subscription_policy",
                "media.delete_subscription",
            ),
            entity_terms=entities,
            max_provider_requests=6,
            max_tool_rounds=4,
            max_tool_calls=6,
            max_capabilities=9,
            parallel_reads=False,
            completion_rule="创建追更前先锁定唯一影视身份；查看、启停、策略和删除必须保持同一订阅对象。",
        )

    if _LIBRARY_REFRESH_RE.search(text):
        return AgentObjectiveContract(
            task_kind="media_library_refresh",
            primary_domains=("library",),
            required_sources=("provider_api",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "provider.capabilities",
                "provider.query",
                "provider.change.preview",
                "provider.change.execute",
                "provider.job.status",
                "config.diagnose_media_servers",
            ),
            entity_terms=entities,
            max_provider_requests=5,
            max_tool_rounds=4,
            max_tool_calls=5,
            max_capabilities=6,
            parallel_reads=False,
            completion_rule="必须通过媒体服务器原生 API 唯一定位媒体库对象，再冻结精准刷新计划；不得接收 URL、路径或猜测内部媒体库 ID，也不允许全库兜底。",
        )

    if _LIBRARY_HEALTH_RE.search(text):
        return AgentObjectiveContract(
            task_kind="library_health",
            primary_domains=("library", "system"),
            required_sources=("local_library",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "workspace.health",
                "config.diagnose_media_servers",
                "config.test_media_server",
                "local_media.diagnose",
            ),
            max_provider_requests=4,
            max_tool_rounds=2,
            max_tool_calls=4,
            max_capabilities=4,
            completion_rule="只核对媒体服务器连通、媒体库绑定和本地整理联动，不搜索具体媒体。",
        )

    if _STRM_SCHEDULE_POLICY_RE.search(text):
        return AgentObjectiveContract(
            task_kind="strm_schedule_policy",
            primary_domains=("strm", "config"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "local_library", "resource_index", "metadata_catalog"),
            allowed_tools=("strm.schedule_policy", "strm.set_schedule_policy"),
            entity_terms=(),
            max_provider_requests=2,
            max_tool_rounds=2,
            max_tool_calls=2,
            max_capabilities=2,
            parallel_reads=False,
            completion_rule="只查看或修改 STRM 定时同步白名单策略；修改策略不得顺带启动同步。",
        )

    if _STRM_FAILURE_WORKFLOW_RE.search(text):
        return AgentObjectiveContract(
            task_kind="strm_failure_workflow",
            primary_domains=("strm", "jobs"),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "local_library", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "strm.status",
                "strm.triage_failures",
                "strm.retry_failures",
            ),
            entity_terms=(),
            max_provider_requests=3,
            max_tool_rounds=3,
            max_tool_calls=3,
            max_capabilities=3,
            parallel_reads=False,
            completion_rule="先汇总失败类型和安全重试范围，再生成重试确认；不得扩大为全量同步。",
        )

    if _STRM_STATUS_RE.search(text):
        return AgentObjectiveContract(
            task_kind="strm_status",
            primary_domains=("strm",),
            required_sources=("system_state",),
            forbidden_sources=(
                "public_web", "local_library", "resource_index", "metadata_catalog"
            ),
            allowed_tools=("strm.status", "strm.diagnose", "strm.run_history"),
            entity_terms=(),
            max_provider_requests=3,
            max_tool_rounds=2,
            max_tool_calls=3,
            max_capabilities=3,
            parallel_reads=True,
            completion_rule=(
                "只回答当前 STRM 状态、进度或历史，不得生成同步确认票据。"
            ),
        )

    if _STRM_SOURCE_LIST_RE.search(text):
        return AgentObjectiveContract(
            task_kind="strm_source_catalog",
            primary_domains=("strm",),
            required_sources=("system_state",),
            forbidden_sources=(
                "public_web", "local_library", "resource_index", "metadata_catalog"
            ),
            allowed_tools=("strm.status",),
            entity_terms=(),
            max_provider_requests=2,
            max_tool_rounds=1,
            max_tool_calls=1,
            max_capabilities=1,
            parallel_reads=False,
            completion_rule=(
                "只列出当前配置中可选择的 STRM 来源显示名称和运行状态，不得启动同步。"
            ),
        )

    if _LIBRARY_PATROL_POLICY_RE.search(text):
        return AgentObjectiveContract(
            task_kind="library_patrol_control",
            primary_domains=("library", "jobs", "config"),
            required_sources=("local_library", "system_state"),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "library.patrol_status",
                "library.patrol_policy",
                "library.set_patrol_policy",
                "library.trigger_patrol_now",
            ),
            entity_terms=(),
            max_provider_requests=4,
            max_tool_rounds=3,
            max_tool_calls=4,
            max_capabilities=4,
            parallel_reads=False,
            completion_rule="策略修改与立即巡检是两个独立确认动作；不得把修改间隔解释为立即执行，也不得自动搜索或下载。",
        )

    if _LIBRARY_WIDE_AUDIT_RE.search(text):
        return AgentObjectiveContract(
            task_kind="library_wide_episode_audit",
            primary_domains=("library", "jobs"),
            required_sources=("local_library",),
            forbidden_sources=("public_web", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "library.audit_library_episodes",
                "library.patrol_status",
                "library.patrol_policy",
                "library.start_episode_audit",
                "library.trigger_patrol_now",
            ),
            entity_terms=(),
            max_provider_requests=3,
            max_tool_rounds=2,
            max_tool_calls=4,
            max_capabilities=5,
            parallel_reads=False,
            completion_rule="全库巡检只汇总已配置媒体服务器中的剧集缺集；立即执行时必须生成后台任务确认，不得扩展为资源搜索或下载。",
        )

    if (
        _LIBRARY_EPISODE_AUDIT_RE.search(text)
        and not _SERIES_UPDATE_RE.search(text)
        and not resource_requested
    ):
        return AgentObjectiveContract(
            task_kind="series_update_audit",
            primary_domains=("library", "official_progress"),
            required_sources=("local_library",),
            forbidden_sources=("resource_index",),
            allowed_tools=(
                "library.search",
                "library.count_series_episodes",
                "library.check_updates",
                "library.audit_episodes",
            ),
            entity_terms=entities,
            max_provider_requests=4,
            max_tool_rounds=3,
            max_tool_calls=4,
            max_capabilities=4,
            completion_rule=(
                "先锁定唯一剧集，再回答本地季集数或已播缺集；用户未要求资源时不得搜索下载候选。"
            ),
        )

    if (
        (_SERIES_UPDATE_RE.search(text) and library_requested)
        or targeted_media_update
    ):
        task_kind = (
            "series_missing_download_plan"
            if resource_requested
            else "series_update_audit"
        )
        domains = ["official_progress", "library"]
        required_sources = ["public_web", "local_library"]
        allowed_tools = [
            "web.search",
            "library.search",
            "library.count_series_episodes",
            "library.check_updates",
            "library.audit_episodes",
        ]
        forbidden_sources: list[str] = []
        if resource_requested:
            domains.append("resource_search")
            required_sources.append("resource_index")
            allowed_tools.extend((
                "library.search_missing_episode_resources",
                "library.search_missing_season_resources",
                "indexer.search_resources",
            ))
        else:
            forbidden_sources.append("resource_index")
        return AgentObjectiveContract(
            task_kind=task_kind,
            primary_domains=tuple(domains),
            required_sources=tuple(required_sources),
            forbidden_sources=tuple(forbidden_sources),
            allowed_tools=tuple(allowed_tools),
            entity_terms=entities,
            max_provider_requests=6 if resource_requested else 4,
            max_tool_rounds=5 if resource_requested else 3,
            max_tool_calls=7 if resource_requested else 5,
            max_capabilities=len(allowed_tools),
            completion_rule=(
                "先固定唯一剧集身份，再依次得到官方已播、本地已有和具体缺集；"
                + (
                    "只有确认存在缺集时才能搜索对应季集资源。资源搜索结果只能称为候选清单，"
                    "不得声称已经生成可执行推送票据；应让用户按候选序号选择 qB、光鸭或两边，"
                    "任何实际提交仍必须等待用户确认。"
                    if resource_requested
                    else "用户未要求资源时，不得启动索引搜索或生成下载候选。"
                )
            ),
        )

    if _RELEASE_STATUS_RE.search(text) and not _NON_MEDIA_RELEASE_RE.search(text):
        forbidden: list[str] = []
        required = ["public_web"]
        domains = ["official_progress"]
        allowed_tools = ["web.search"]
        if library_requested:
            required.append("local_library")
            domains.append("library")
            allowed_tools.extend(("library.search", "library.check_updates"))
        else:
            forbidden.append("local_library")
        if resource_requested:
            required.append("resource_index")
            domains.append("resource_search")
            allowed_tools.append("indexer.search_resources")
        else:
            forbidden.append("resource_index")
        entity_budget = min(4, max(2, len(entities) + 1))
        return AgentObjectiveContract(
            task_kind="official_release_status",
            primary_domains=tuple(domains),
            required_sources=tuple(required),
            forbidden_sources=tuple(forbidden),
            allowed_tools=tuple(allowed_tools),
            entity_terms=entities,
            max_provider_requests=entity_budget,
            max_tool_rounds=max(1, entity_budget - 1),
            max_tool_calls=max(1, min(3, len(entities) or 2)),
            max_capabilities=len(allowed_tools),
            completion_rule=(
                "对每个明确实体只给已上线、未上线或无法确认，并附绝对日期；"
                "不得因为未检查本地库或资源索引而返回部分完成。"
            ),
        )

    if _RECOMMEND_RE.search(text):
        years = [int(raw) for raw in re.findall(r"(?<![0-9])(20[0-9]{2})(?![0-9])", text)]
        time_sensitive = bool(
            any(year >= date.today().year for year in years)
            or any(marker in text for marker in ("今年", "最新", "近期", "即将", "待播"))
            or re.search(
                rf"(?:最近|近期).{{0,16}}(?:有|有哪些|推荐).{{0,8}}"
                rf"(?:新剧|新番|新动画|国漫|国创)",
                text,
                re.IGNORECASE,
            )
        )
        required_sources = (
            ("metadata_catalog", "public_web")
            if time_sensitive
            else ("metadata_catalog",)
        )
        allowed_tools = ["discovery.recommend"]
        if time_sensitive:
            allowed_tools.append("web.search")
        return AgentObjectiveContract(
            task_kind="media_recommendation",
            primary_domains=("discovery", "official_progress"),
            required_sources=required_sources,
            forbidden_sources=("local_library", "resource_index"),
            allowed_tools=tuple(allowed_tools),
            entity_terms=(),
            max_provider_requests=4 if time_sensitive else 3,
            max_tool_rounds=3 if time_sensitive else 2,
            max_tool_calls=3 if time_sensitive else 2,
            max_capabilities=len(allowed_tools),
            completion_rule=(
                "有明确年份、地区、题材或媒体类型时，必须把限制条件传给受控推荐列表；"
                "不得把筛选词拼成片名搜索，也不得丢失用户限制。"
                "时效请求必须同时取得一个影视元数据目录结果和一个公开网页结果；"
                "任一必需来源尚未成功前，不得重复调用已经成功的同类来源。"
                "必须区分已上线、已定档和仍在制作，不得逐部无界搜索。"
            ),
        )

    if _DISCOVERY_METADATA_RE.search(text) and not resource_requested:
        return AgentObjectiveContract(
            task_kind="media_metadata_search",
            primary_domains=("discovery", "media_identity", "rating"),
            required_sources=("metadata_catalog",),
            forbidden_sources=("resource_index", "local_library"),
            allowed_tools=("discovery.search", "discovery.lookup_rating"),
            entity_terms=entities,
            max_provider_requests=3,
            max_tool_rounds=2,
            max_tool_calls=2,
            max_capabilities=2,
            completion_rule="保留用户明确年份、地区、题材和媒体类型，只返回影视资料或评分，不扩展为资源下载。",
        )

    if _STRM_SOURCE_SYNC_RE.search(text) or _STRM_IMPLICIT_SOURCE_SYNC_RE.search(text):
        return AgentObjectiveContract(
            task_kind="strm_source_sync",
            primary_domains=("strm",),
            required_sources=("system_state",),
            forbidden_sources=("public_web", "local_library", "resource_index", "metadata_catalog"),
            allowed_tools=(
                "strm.status",
                "strm.diagnose",
                "strm.run_history",
                "strm.run_once",
            ),
            entity_terms=(),
            max_provider_requests=3,
            max_tool_rounds=2,
            max_tool_calls=3,
            max_capabilities=4,
            parallel_reads=False,
            completion_rule=(
                "只使用已配置 STRM 来源和安全来源引用；用户要求计划时不得启动同步。"
            ),
        )

    if _ORGANIZE_OBJECT_RE.search(text) and _OBJECT_SCOPE_RE.search(text):
        organize_tools = [
            "guangya.directory_scrape.inspect",
            "guangya.directory_scrape.search",
            "guangya.directory_scrape.preview",
        ]
        if not _PLAN_ONLY_RE.search(text):
            organize_tools.append("guangya.directory_scrape.run")
        return AgentObjectiveContract(
            task_kind="organize_object",
            primary_domains=("organize", "media_identity"),
            required_sources=("system_state", "metadata_catalog"),
            forbidden_sources=("public_web", "resource_index"),
            allowed_tools=tuple(organize_tools),
            entity_terms=entities,
            max_provider_requests=5,
            max_tool_rounds=4,
            max_tool_calls=5,
            max_capabilities=4,
            parallel_reads=False,
            completion_rule=(
                "先解析并冻结唯一云盘对象，再检查、识别和生成单一整理计划；"
                "不得把指定对象升级为全部来源整理。"
            ),
        )

    return AgentObjectiveContract(entity_terms=entities)
