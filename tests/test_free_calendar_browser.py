"""追漫日历浏览器回归：官方离线排期与合成元数据边界分开；无应用/DB/外网。"""
from __future__ import annotations

import copy
import json
import mimetypes
import os
import re
import time as clock
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

# 必须先建立测试配置隔离，再导入任何项目帮助；不创建 Flask 应用。
import tests  # noqa: F401
from tests.test_agent_kernel_browser import _chromium_executable, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / '.superpowers/tmp/anime-calendar-completion-20260910/ui'
ORIGIN = 'https://mediaflux.test'
PATH = '/api/discovery/calendar'
TODAY = '2026-09-09'
START = '2026-09-07'
NAMES = {'tencent': '腾讯视频', 'iqiyi': '爱奇艺', 'youku': '优酷'}


# 官方离线 Youku webcomic 周历投影：47 节目 / 92 事件 / 91 条有时间。
# 2026-09-09 从用户提供的 webcomic-week-events.json 提取，无真实 TMDB 元数据。
# 列：节目 ID、标题、日期、星期、时间、原排期、排期受众、节目角标（不得作为免费排期依据）。
OFFICIAL_ROWS = [
    ('bbdcfbf516104f9dbd65', '被家族抛弃，我觉醒九亿属性点 第二季', '2026-09-07', 1, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('bcce2a04465a472bb38a', '开心锤锤Story：锤星食界', '2026-09-07', 1, '10:00', '10:00更新1话', '未注明', ''),
    ('babb02b2839847ff901c', '一具枯骨，逆天修成仙', '2026-09-07', 1, '10:00', '10:00更新20话', '未注明', 'VIP'),
    ('abeebfa336a443f48d2d', '十殿阎罗吾即是红伞鬼仙', '2026-09-07', 1, '10:00', '10:00更新20话', '未注明', ''),
    ('daaad3bb331546659410', '从乱葬岗到幽冥之主', '2026-09-07', 1, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('cabaed8188924762ac6b', '控分学神', '2026-09-07', 1, '10:00', '10:00更新5话', '未注明', ''),
    ('ddbb0dbd0c944f29aed2', '她们的故事', '2026-09-07', 1, '19:00', '19:00更新1话', '未注明', 'VIP'),
    ('caead4f9818946e183dc', '战神的兔子阿姐', '2026-09-07', 1, '10:00', '10:00更新3话', '未注明', 'VIP'),
    ('cfafd913da434706ab70', '丑妻仙缘', '2026-09-07', 1, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('accfa0e7d8c643b69e9c', '予你深情侵入余生', '2026-09-07', 1, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('aece48d4fa4548078219', '斗罗大陆4终极斗罗 合集', '2026-09-07', 1, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('acbcdc8ad79c4d71b623', '红妆送君葬', '2026-09-07', 1, '11:00', '11:00更新3话', '未注明', ''),
    ('cbbc52fb1d494393b0ae', '制霸联盟：绝杀球我十中十', '2026-09-07', 1, '10:00', '10:00更新3话', '未注明', ''),
    ('fdca5177ae604c66bdb8', '一斩苍穹', '2026-09-08', 2, '10:00', '10:00更新1话', '未注明', '独播'),
    ('fcef13b1cea4491688ee', '永夜之王', '2026-09-08', 2, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('dcfc601098b54c379a57', '全民御兽：开局山海经，我横扫全球', '2026-09-08', 2, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('abeebfa336a443f48d2d', '十殿阎罗吾即是红伞鬼仙', '2026-09-08', 2, '10:00', '10:00更新20话', '未注明', ''),
    ('dcfc646c60664dab978b', '邪魔墨然', '2026-09-08', 2, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('bcce2a04465a472bb38a', '开心锤锤Story：锤星食界', '2026-09-08', 2, '10:00', '10:00更新2话', '未注明', ''),
    ('babb02b2839847ff901c', '一具枯骨，逆天修成仙', '2026-09-08', 2, '10:00', '10:00更新20话', '未注明', 'VIP'),
    ('cabaed8188924762ac6b', '控分学神', '2026-09-08', 2, '10:00', '10:00更新5话', '未注明', ''),
    ('bdfaac2cedf6447a8e31', '穿越兽世，兔子也能打', '2026-09-08', 2, '10:00', '10:00更新3话', '未注明', 'VIP'),
    ('cfafd913da434706ab70', '丑妻仙缘', '2026-09-08', 2, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('cabfc2f4a49447c5b208', '掌门低调点', '2026-09-08', 2, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('cebcebd964e644cf8837', '误入诡宗后，我成了全宗团宠', '2026-09-08', 2, '11:30', '11:30更新1话', '未注明', 'VIP'),
    ('cacf63f037604089b341', '长生猫', '2026-09-08', 2, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('badbb5792f934ddb82fd', '师兄啊师兄', '2026-09-09', 3, '10:00', '10:00 SVIP更新1话', 'SVIP', '独播'),
    ('fcef13b1cea4491688ee', '永夜之王', '2026-09-09', 3, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('dcab66069929483186bc', '开心锤锤3D', '2026-09-09', 3, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('ffed8094008e47acad25', '余烬之后', '2026-09-09', 3, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('aefb6e73b0e245dfa2ee', '斗罗大陆5重生唐三', '2026-09-09', 3, '12:00', '12:00更新1话', '未注明', 'VIP'),
    ('abeebfa336a443f48d2d', '十殿阎罗吾即是红伞鬼仙', '2026-09-09', 3, '10:00', '10:00更新20话', '未注明', ''),
    ('cabaed8188924762ac6b', '控分学神', '2026-09-09', 3, '10:00', '10:00更新5话', '未注明', ''),
    ('dbaf555e7e714841bc2c', '时间捡史之《西游》', '2026-09-09', 3, '12:00', '12:00更新1话', '未注明', 'VIP'),
    ('bcce2a04465a472bb38a', '开心锤锤Story：锤星食界', '2026-09-09', 3, '10:00', '10:00更新2话', '未注明', ''),
    ('caead4f9818946e183dc', '战神的兔子阿姐', '2026-09-09', 3, '10:00', '10:00更新3话', '未注明', 'VIP'),
    ('dfee1c0ae9c54f7584d3', '帅府福崽，卯卯驾到', '2026-09-09', 3, '10:00', '10:00更新5话', '未注明', 'VIP'),
    ('cfafd913da434706ab70', '丑妻仙缘', '2026-09-09', 3, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('acbcdc8ad79c4d71b623', '红妆送君葬', '2026-09-09', 3, '11:00', '11:00更新3话', '未注明', ''),
    ('daed76f43b5244bd9f9f', '我在豪门摆地摊', '2026-09-09', 3, '10:00', '10:00更新5话', '未注明', 'VIP'),
    ('eabedc9465724a09b03d', '东大高武学院', '2026-09-10', 4, '10:00', '10:00更新1话', '未注明', '独播'),
    ('badbb5792f934ddb82fd', '师兄啊师兄', '2026-09-10', 4, '10:00', '10:00更新1话', '未注明', '独播'),
    ('ffedc9f2f95d41c9a238', '机械飞升', '2026-09-10', 4, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('dcab66069929483186bc', '开心锤锤3D', '2026-09-10', 4, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('afbe218f2b834b8698e7', '轩辕小豆之山海奇缘', '2026-09-10', 4, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('cdcbdabcc35e4e4e8d7b', '雪王来了！', '2026-09-10', 4, '11:00', '11:00更新1话', '未注明', 'VIP'),
    ('afedf8e291394eccb06e', '是王者啊？第六季', '2026-09-10', 4, '10:00', '10:00更新2话', '未注明', ''),
    ('ebadea91856e40a4abc3', '掌天仙葫', '2026-09-10', 4, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('abeebfa336a443f48d2d', '十殿阎罗吾即是红伞鬼仙', '2026-09-10', 4, '10:00', '10:00更新20话', '未注明', ''),
    ('bcce2a04465a472bb38a', '开心锤锤Story：锤星食界', '2026-09-10', 4, '10:00', '10:00更新2话', '未注明', ''),
    ('eafb203e6efd442eadfd', '戏妖', '2026-09-10', 4, '10:00', '10:00更新1话', '未注明', '独播'),
    ('caabc3bf10da41a38ff4', '太玄胎珠传', '2026-09-10', 4, '10:00', '10:00更新20话', '未注明', 'VIP'),
    ('cabaed8188924762ac6b', '控分学神', '2026-09-10', 4, '10:00', '10:00更新5话', '未注明', ''),
    ('cfafd913da434706ab70', '丑妻仙缘', '2026-09-10', 4, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('adaec02f09f9469ab8d8', '沧元图', '2026-09-11', 5, '10:00', '10:00 SVIP更新1话', 'SVIP', '独播'),
    ('bdbcd9ea582547d1b014', '封天契', '2026-09-11', 5, '10:00', '10:00更新1话', '未注明', '独播'),
    ('ffedc9f2f95d41c9a238', '机械飞升', '2026-09-11', 5, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('ebbc85b6c57d404792f9', '斗罗大陆3龙王传说 合集', '2026-09-11', 5, '12:00', '12:00更新1话', '未注明', '逐集限免'),
    ('dcfc646c60664dab978b', '邪魔墨然', '2026-09-11', 5, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('dcab66069929483186bc', '开心锤锤3D', '2026-09-11', 5, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('bcce2a04465a472bb38a', '开心锤锤Story：锤星食界', '2026-09-11', 5, '10:00', '10:00更新2话', '未注明', ''),
    ('abeebfa336a443f48d2d', '十殿阎罗吾即是红伞鬼仙', '2026-09-11', 5, '10:00', '10:00更新20话', '未注明', ''),
    ('acbcdc8ad79c4d71b623', '红妆送君葬', '2026-09-11', 5, '11:00', '11:00更新3话', '未注明', ''),
    ('cabaed8188924762ac6b', '控分学神', '2026-09-11', 5, '10:00', '10:00更新5话', '未注明', ''),
    ('cfafd913da434706ab70', '丑妻仙缘', '2026-09-11', 5, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('cdcbdabcc35e4e4e8d7b', '雪王来了！', '2026-09-11', 5, '11:00', '11:00更新1话', '未注明', 'VIP'),
    ('adaec02f09f9469ab8d8', '沧元图', '2026-09-12', 6, '10:00', '10:00更新1话', '未注明', '独播'),
    ('caab6ed59a5547a78fb1', '光阴之外', '2026-09-12', 6, '10:00', '10:00SVIP更新1话', 'SVIP', '独播'),
    ('addedfedd8b64f2b8b69', '如果历史是一群喵 大明皇朝篇', '2026-09-12', 6, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('eebee0c071bc4933adff', '深渊之上', '2026-09-12', 6, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('fabf217080064c44a086', '诸葛九九之虚拟游戏', '2026-09-12', 6, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('cc003400962411de83b1', '名侦探柯南', '2026-09-12', 6, '19:30', '19:30更新1话', '未注明', 'VIP'),
    ('bcce2a04465a472bb38a', '开心锤锤Story：锤星食界', '2026-09-12', 6, '', '草根厨神守护美食真心', '未解析', ''),
    ('abeebfa336a443f48d2d', '十殿阎罗吾即是红伞鬼仙', '2026-09-12', 6, '10:00', '10:00更新20话', '未注明', ''),
    ('eafb203e6efd442eadfd', '戏妖', '2026-09-12', 6, '10:00', '10:00更新1话', '未注明', '独播'),
    ('caabc3bf10da41a38ff4', '太玄胎珠传', '2026-09-12', 6, '10:00', '10:00更新20话', '未注明', 'VIP'),
    ('fedc6078b84c49869944', '高武进化：从觉醒怪兽之王开始', '2026-09-12', 6, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('cabaed8188924762ac6b', '控分学神', '2026-09-12', 6, '10:00', '10:00更新5话', '未注明', ''),
    ('acbcdc8ad79c4d71b623', '红妆送君葬', '2026-09-12', 6, '11:00', '11:00更新3话', '未注明', ''),
    ('caab6ed59a5547a78fb1', '光阴之外', '2026-09-13', 7, '10:00', '10:00更新1话', '未注明', '独播'),
    ('dcfc646c60664dab978b', '邪魔墨然', '2026-09-13', 7, '10:00', '10:00更新1话', '未注明', 'VIP'),
    ('bbdcfbf516104f9dbd65', '被家族抛弃，我觉醒九亿属性点 第二季', '2026-09-13', 7, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('eebee0c071bc4933adff', '深渊之上', '2026-09-13', 7, '10:00', '10:00更新1话', '未注明', '逐集限免'),
    ('abeebfa336a443f48d2d', '十殿阎罗吾即是红伞鬼仙', '2026-09-13', 7, '10:00', '10:00更新20话', '未注明', ''),
    ('cabaed8188924762ac6b', '控分学神', '2026-09-13', 7, '10:00', '10:00更新5话', '未注明', ''),
    ('bcce2a04465a472bb38a', '开心锤锤Story：锤星食界', '2026-09-13', 7, '10:00', '10:00更新2话', '未注明', ''),
    ('ebadea91856e40a4abc3', '掌天仙葫', '2026-09-13', 7, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('acbcdc8ad79c4d71b623', '红妆送君葬', '2026-09-13', 7, '11:00', '11:00更新3话', '未注明', ''),
    ('cfdb7a74d34944d18ec4', '仙帝见我也得跪', '2026-09-13', 7, '10:00', '10:00更新10话', '未注明', '逐集限免'),
    ('cfafd913da434706ab70', '丑妻仙缘', '2026-09-13', 7, '10:00', '10:00更新10话', '未注明', 'VIP'),
    ('acba3b0995164e0fac8b', '修真界宠翻啦', '2026-09-13', 7, '10:00', '10:00更新5话', '未注明', 'VIP'),
    ('fedc6078b84c49869944', '高武进化：从觉醒怪兽之王开始', '2026-09-13', 7, '10:00', '10:00更新1话', '未注明', 'VIP'),
]


def blank_payload():
    return {'timezone': 'Asia/Shanghai', 'today': TODAY, 'week_start': START,
        'days': [{'date': f'2026-09-{7+i:02}', 'weekday': i+1, 'label': f'周{i+1}', 'is_today': i == 2, 'items': []} for i in range(7)],
        'unscheduled': [], 'sources': [{'id': source, 'name': name, 'status': 'ok', 'message': '测试来源公开排期',
            'fetched_at': '2026-09-09T10:20:00+08:00', 'sampled': 10, 'ignored': 0} for source, name in NAMES.items()],
        'refreshing': False, 'updated_at': '2026-09-09T10:20:00+08:00', 'retry_after': 5, 'items_count': 0}


def synthetic_entry(number=1, source='tencent', **overrides):
    hosts = {'tencent': 'v.qq.com', 'iqiyi': 'www.iqiyi.com', 'youku': 'v.youku.com'}
    item = {'stable_id': f'{source}:fixture-{number}', 'source': source, 'source_id': f'fixture-{number}',
        'title': f'测试动漫 · 山海来信 {number}', 'category': 'animation', 'year': '2026', 'url': f'https://{hosts[source]}/fixture-{number}',
        'poster_url': f'/discovery-poster/tmdb/fixture-{number}', 'tmdb_id': 12300+number, 'rating': 8.1,
        'overview': '合成浏览器元数据，不是真实 TMDB 匹配', 'free_progress': '', 'stale': False,
        'update_time': '18:00', 'schedule': '18:00更新1集', 'schedule_audience': 'unknown', 'events': []}
    item.update(overrides)
    item.setdefault('poster_urls', [])
    tmdb=str(item.get('tmdb_id') or '')
    douban=str(item.get('douban_id') or '')
    provider='tmdb' if tmdb.isdigit() else 'douban' if douban.isdigit() else ''
    identity=tmdb if provider=='tmdb' else douban if provider=='douban' else ''
    item.setdefault('detail_url', f'/discovery?detail_provider={provider}&detail_type=tv&detail_id={identity}' if provider else '')
    item.setdefault('watchlist', {'provider': provider,'external_id': identity,'media_type': 'tv','poster_token': f'{provider}-fixture-token','in_watchlist': False} if provider else None)
    return item


def synthetic_payload():
    data=blank_payload()
    for day in data['days']:
        for number in range(1, 7):
            item=synthetic_entry(number, ['tencent','iqiyi','youku'][number % 3],
                schedule_audience=['unknown','member','free'][number % 3])
            item['events']=[{'date': day['date'], 'weekday': day['weekday'], 'update_time': item['update_time'],
                                'schedule': item['schedule'], 'audience': item['schedule_audience']}]
            day['items'].append(item)
    data['unscheduled']=[synthetic_entry(99, title='不可作为日历内容的库存条目')]
    data['items_count']=6
    return data


def official_payload():
    data=blank_payload()
    grouped={}
    for identity, title, date, weekday, time, schedule, raw_audience, access_label in OFFICIAL_ROWS:
        key=(date,identity)
        if key not in grouped:
            # 只保留官方事件；不杜撰 TMDB 评分、匹配或免费进度。
            item={'stable_id': f'youku:{identity}', 'source': 'youku', 'source_id': identity, 'title': title,
                'category': 'animation', 'url': '',  # 事件投影未含节目链接，不根据 ID 杜撰原页。
                'poster_url': '', 'tmdb_id': '', 'rating': None, 'overview': '', 'free_progress': '', 'stale': False, 'events': []}
            grouped[key]=item
            data['days'][weekday-1]['items'].append(item)
        item=grouped[key]
        mode='member' if raw_audience in ('SVIP','VIP') else 'unknown'
        item['events'].append({'date': date, 'weekday': weekday, 'update_time': time, 'schedule': schedule, 'audience': mode})
    for item in grouped.values():
        primary=next((event for event in item['events'] if event['audience']=='free'), item['events'][0])
        item.update(update_time=primary['update_time'], schedule=primary['schedule'], schedule_audience=primary['audience'])
    data['items_count']=len({row[0] for row in OFFICIAL_ROWS})
    data['sources'][0].update(status='unavailable', message='此离线夹具仅包含优酷周历', fetched_at='')
    data['sources'][1].update(status='unavailable', message='此离线夹具仅包含优酷周历', fetched_at='')
    data['sources'][2].update(status='partial', message='官方 webcomic 周历样本：47 节目、92 事件；非全站覆盖', sampled=47)
    return data

def render_template(template='free_calendar.html', *, resource_results_enabled=False):
    env = Environment(loader=FileSystemLoader(ROOT / 'app/templates'),
                      autoescape=select_autoescape(['html']))
    env.globals.update(
        static_url=lambda path: '/static/' + path,
        csrf_token=lambda: 'fixture-csrf-token',
        url_for=lambda endpoint, **kwargs: {
            'pages.discovery': '/discovery', 'pages.free_calendar': '/discovery/calendar',
        }.get(endpoint, '/' + endpoint.split('.')[-1]),
    )
    return env.get_template(template).render(
        calendar_today=TODAY, calendar_week_start=START, active='discovery',
        discovery_enabled=True, agent_enabled=False,
        resource_results_enabled=resource_results_enabled,
    )


class FixtureNetwork:
    """Context 级 fail-closed HTTP/WS 拦截，包括弹窗；没有 continue/fetch。"""

    def __init__(self, initial, *, resource_results_enabled=False):
        self.data = copy.deepcopy(initial)
        self.calls = []
        self.pending = []
        self.hold = False
        self.status = 200
        self.unexpected = []
        self.websockets = []
        self.watch_calls = []
        self.watch_pending = []
        self.watch_hold = False
        self.watch_status = 200
        self.watch_success = True
        self.poster_failures = set()
        self.poster_calls = []
        self.poster_holds = set()
        self.poster_pending = []
        self.platform_pages = set()
        self.platform_calls = []
        self.indexer_calls = []
        self.indexer_payload = None  # 用例须显式登记；默认不放行资源检索。
        self.html = render_template(resource_results_enabled=resource_results_enabled)
        self.static_paths = {
            '/static/' + path: ROOT / 'app/static' / path for path in (
                'css/main.css', 'css/free-calendar.css', 'js/free-calendar.js',
                'js/viewport-inset.js', 'js/lucide.min.js', 'js/app.js', 'js/discovery.js', 'favicon.svg',
            )
        }

    def respond(self, route):
        route.fulfill(status=self.status, content_type='application/json',
                      body=json.dumps(self.data, ensure_ascii=False))

    def release(self):
        pending, self.pending = self.pending, []
        for route in pending:
            self.respond(route)

    def respond_watch(self, route):
        route.fulfill(status=self.watch_status,content_type='application/json',body=json.dumps({'success':self.watch_success}))

    def release_watch(self):
        pending,self.watch_pending=self.watch_pending,[]
        for route in pending: self.respond_watch(route)

    def respond_poster(self, route):
        path = urlsplit(route.request.url).path
        if path in self.poster_failures:
            route.fulfill(status=503, content_type='text/plain', body='fixture poster failure')
            return
        # 本地生成的图形仅为加载/宽高占位测试，不包含真实海报。
        token = path.rsplit('/', 1)[-1]
        suffix = token.rsplit('-', 1)[-1]
        number = int(suffix) if suffix.isdigit() and len(suffix) < 5 else len(token)
        width, height = (640, 360) if path.startswith('/discovery-calendar-poster/') else (200, 300)
        colors = ['#62766b', '#616b84', '#976d53', '#577879']
        route.fulfill(status=200, content_type='image/svg+xml', body=f'''
            <svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 200 300">
              <rect width="200" height="300" fill="{colors[number % 4]}"/>
              <circle cx="140" cy="95" r="65" fill="#f5e5ba" opacity=".32"/>
              <path d="M0 190L80 92 155 220 200 135V300H0" fill="#101c22" opacity=".5"/>
              <text x="18" y="248" fill="#fff" font-size="17">测试动漫 {number:02}</text>
              <text x="18" y="273" fill="#eee" font-size="10">BROWSER FIXTURE ONLY</text>
            </svg>''')

    def release_poster(self, path):
        self.poster_holds.discard(path)
        pending, self.poster_pending = self.poster_pending, []
        for route in pending:
            if urlsplit(route.request.url).path == path:
                self.respond_poster(route)
            else:
                self.poster_pending.append(route)

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        self.calls.append((request.method, request.url, request.headers))
        if request.url in self.platform_pages and request.method == 'GET' and request.is_navigation_request():
            # 只放行用例显式登记的原页导航，依然本地 fulfill，不连接平台。
            self.platform_calls.append(request.url)
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body='<meta charset="utf-8"><title>离线平台原页夹具</title>')
        elif f'{url.scheme}://{url.netloc}' != ORIGIN:
            self.unexpected.append(request.url)
            route.abort()
        elif path == '/discovery/calendar':
            route.fulfill(status=200, content_type='text/html', body=self.html)
        elif path in (PATH, PATH + '/refresh'):
            if self.hold:
                self.pending.append(route)
            else:
                self.respond(route)
        elif path.startswith('/api/discovery/detail/'):
            provider,media_type,identity=path.rsplit('/',3)[-3:]
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'detail':{'provider': provider,'media_type': media_type,'external_id': identity,'title': f'详情夹具 {provider} {identity}','year': '2026','poster_url': '','overview': '通过既有 profileOnly 详情弹层显示','tmdb_id': identity if provider=='tmdb' else '','rating': 8.1}}))
        elif path == '/api/indexers/search' and request.method == 'POST' and self.indexer_payload is not None:
            self.indexer_calls.append({'method': request.method, 'path': path,
                'headers': request.headers, 'body': json.loads(request.post_data)})
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps(self.indexer_payload, ensure_ascii=False))
        elif path == '/api/discovery/map':
            route.fulfill(status=200,content_type='application/json',body='{"candidates": []}')
        elif path.startswith('/api/discovery/watchlist'):
            self.watch_calls.append({'method': request.method,'path': path,'headers': request.headers,'body': request.post_data})
            if self.watch_hold: self.watch_pending.append(route)
            else: self.respond_watch(route)
        elif path in self.static_paths:
            file = self.static_paths[path]
            route.fulfill(status=200, body=file.read_bytes(),
                          content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')
        elif not url.query and not url.fragment and re.fullmatch(
            r'/(?:discovery-poster/(?:tmdb|douban)|discovery-calendar-poster/(?:tencent|iqiyi|youku))/[A-Za-z0-9_.-]{1,2048}', path
        ):
            self.poster_calls.append(path)
            if path in self.poster_holds:
                self.poster_pending.append(route)
            else:
                self.respond_poster(route)
        elif path == '/api/config':
            route.fulfill(status=200, content_type='application/json', body='{}')
        else:
            self.unexpected.append(request.url)
            route.fulfill(status=404, body='Blocked by browser fixture')

    def websocket(self, socket):
        self.websockets.append(socket.url)
        # 不 connect_to_server：全部消息留在测试上下文，避免真实握手。
        socket.on_message(lambda message: None)

    def api_calls(self):
        return [call for call in self.calls if urlsplit(call[1]).path in (PATH, PATH + '/refresh')]


@unittest.skipIf(sync_playwright is None, '系统环境未安装 Playwright')
class FreeCalendarBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        options = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
        executable = _chromium_executable(cls.playwright)
        if executable:
            options['executable_path'] = executable
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def make_page(self, data=None, width=1440, hold=False, poster_failures=(), poster_holds=(),
                  *, resource_results_enabled=False):
        network = FixtureNetwork(data if data is not None else synthetic_payload(),
                                 resource_results_enabled=resource_results_enabled)
        network.hold = hold
        network.poster_failures = set(poster_failures)
        network.poster_holds = set(poster_holds)
        context = self.browser.new_context(viewport={'width': width, 'height': 1050},
                                           color_scheme='dark', service_workers='block')
        context.route('**/*', network.route)
        context.route_web_socket('**/*', network.websocket)
        page = context.new_page()
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        self.addCleanup(context.close)
        self.addCleanup(lambda: self.assertEqual(errors, [], '浏览器脚本异常'))
        self.addCleanup(lambda: self.assertEqual(network.unexpected, [], '非夹具请求被阻止'))
        page.goto(ORIGIN + '/discovery/calendar', wait_until='domcontentloaded')
        if not hold:
            page.wait_for_function("document.querySelector('#calendar-count').textContent !== '读取中'")
        return page, network

    def assert_no_overflow(self, page):
        self.assertTrue(page.evaluate('''() => document.documentElement.scrollWidth <= innerWidth
          && [...document.querySelectorAll('[data-weekly-calendar] *:not(.sr-only)')].filter(el => el.getClientRects().length)
          .every(el => el.getBoundingClientRect().right <= innerWidth + 1 && el.getBoundingClientRect().left >= -1)'''))

    def screenshot(self, page, name):
        if os.environ.get('FREE_CALENDAR_SCREENSHOTS') != '1':
            return
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        scroll = page.evaluate('scrollY')
        page.evaluate('window.scrollTo(0, 0)')
        page.evaluate('''() => {
          const label = document.createElement('div'); label.id = 'fixture-watermark';
          label.textContent = '浏览器测试夹具 · 非生产数据';
          label.style.cssText = 'position:fixed;right:8px;top:6px;z-index:9999;background:#111;color:#fff;padding:5px 8px;font:11px sans-serif;border:1px solid #666;border-radius:4px;pointer-events:none';
          document.body.append(label);
        }''')
        page.screenshot(path=str(EVIDENCE / name), full_page=True, animations='disabled')
        page.locator('#fixture-watermark').evaluate('(node) => node.remove()')
        page.evaluate('(value) => window.scrollTo(0, value)', scroll)

    def test_official_youku_week_structure_dates_and_access_are_not_free(self):
        data=official_payload()
        self.assertEqual(len(OFFICIAL_ROWS),92)
        self.assertEqual(data['items_count'],47)
        self.assertEqual(sum(bool(row[4]) for row in OFFICIAL_ROWS),91)
        page, network=self.make_page(data)
        self.assertEqual(page.locator('#calendar-heading').inner_text(),'追漫日历')
        self.assertEqual(page.locator('.wc-weekday').count(),7)
        self.assertEqual(page.locator('.wc-grid:visible').count(),1)
        self.assertEqual(page.locator('.wc-grid:visible').get_attribute('data-date'),TODAY)
        self.assertEqual(page.locator('.wc-card:visible').count(),len(data['days'][2]['items']))
        self.assertEqual(page.locator('.wc-card').count(),len({(row[0],row[2]) for row in OFFICIAL_ROWS}))
        self.assertEqual(page.locator('.wc-audience[data-audience="free"]').count(),0)
        card=page.locator('.wc-grid:visible .wc-card').filter(has_text='师兄啊师兄')
        self.assertIn('会员排期',card.inner_text())
        self.assertEqual(card.locator('.wc-update-time').inner_text(),'10:00')
        self.assertIn('SVIP',card.locator('.wc-caption').text_content())
        self.assertEqual(page.locator('#calendar-source-panel:visible').count(),0)
        self.assertEqual(page.locator('#calendar-pending,.fc-sources,.fc-notice,.fc-week-grid').count(),0)
        self.assertEqual(page.locator('.wc-card .discovery-poster img[src]').count(),0)
        self.assertEqual(page.locator('.wc-card a[href]').count(),0)
        self.assertEqual(page.locator('.wc-card .discovery-watchlist-action:not([disabled])').count(),0)
        self.assertEqual(len(network.api_calls()),1)
        self.assert_no_overflow(page)
        self.screenshot(page,'anime-offline-youku-desktop-1440.png')

    def test_calendar_name_and_discovery_entry_use_anime_label(self):
        page,_=self.make_page()
        self.assertEqual(page.title(),'追漫日历 - MediaFlux')
        self.assertEqual(page.get_by_role('heading',name='追漫日历',exact=True).count(),1)
        self.assertNotIn('追剧日历',page.content())
        discovery_html=render_template('discovery.html')
        self.assertNotIn('追剧日历',discovery_html)
        entry=page.evaluate('''(markup)=>{
          const doc=new DOMParser().parseFromString(markup,'text/html');
          const link=doc.querySelector('a[href="/discovery/calendar"]');
          return link && {label:link.getAttribute('aria-label'),title:link.getAttribute('title')};
        }''',discovery_html)
        self.assertEqual(entry,{'label':'追漫日历','title':'追漫日历'})

    def test_non_animation_payload_is_rejected_on_load_and_refresh(self):
        data=synthetic_payload()
        invalid_categories=['tv','movie','variety','Animation','animation ','',None,['animation'],{'category':'animation'}]
        rejected=[]
        for index,category in enumerate(invalid_categories,100):
            rejected.append(synthetic_entry(index,list(NAMES)[index % 3],category=category,title=f'脏数据 {index}'))
        missing=synthetic_entry(199,title='脏数据：缺失分类')
        del missing['category']
        rejected.append(missing)
        changed_id=data['days'][2]['items'][0]['stable_id']
        for day in data['days']:
            # 相同 ID 的脏 TV 放在合法动漫之前，不能占用去重键或操作身份。
            collision=copy.deepcopy(day['items'][0])
            collision.update(category='tv',title='脏数据：冲突 TV',poster_url='/discovery-poster/tmdb/fixture-999')
            day['items']=[collision,*copy.deepcopy(rejected),*day['items']]
        page,network=self.make_page(data)
        self.assertEqual(page.locator('.wc-card').count(),42)
        self.assertEqual(page.locator('#calendar-count').inner_text(),'6 部')
        self.assertNotIn('脏数据',page.locator('[data-weekly-calendar]').text_content())
        self.assertEqual(set(page.locator('.wc-card .discovery-card-source > span:first-child').all_text_contents()),{'动漫 · 2026'})
        page.evaluate('''(id)=>{
          const cards=[...document.querySelectorAll('.wc-card')];
          window.rejectedCards=cards.filter(card=>card.dataset.stableId===id);
          window.retainedCards=cards.filter(card=>card.dataset.stableId!==id);
          window.retainedImages=retainedCards.map(card=>card.querySelector('img'));
        }''',changed_id)
        for day in network.data['days']:
            for item in day['items']:
                if item['stable_id']==changed_id:
                    item.update(category='tv',title='脏数据：刷新变为 TV')
        page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(page.locator('.wc-card').count(),35)
        self.assertEqual(page.locator('#calendar-count').inner_text(),'5 部')
        self.assertTrue(page.evaluate('rejectedCards.length===7 && rejectedCards.every(card=>!card.isConnected)'))
        self.assertTrue(page.evaluate('retainedCards.every((card,index)=>card.isConnected && card.querySelector("img")===retainedImages[index])'))
        self.assertNotIn('脏数据',page.locator('[data-weekly-calendar]').text_content())
        network.data=blank_payload()
        for day in network.data['days']:
            day['items']=copy.deepcopy(rejected)
        page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(page.locator('.wc-card').count(),0)
        self.assertEqual(page.locator('#calendar-count').inner_text(),'0 部')
        self.assertIn('当前平台下暂无明确动漫排期',page.locator('.wc-empty:visible').inner_text())
        self.assertNotIn('类型筛选',page.locator('.wc-empty:visible').inner_text())
        self.assertNotIn('脏数据',page.locator('[data-weekly-calendar]').text_content())
        self.assertTrue(all(int(path.rsplit('-',1)[-1])<100 for path in network.poster_calls))
        self.assertEqual(network.watch_calls,[])

    def test_service_primary_free_branch_and_optional_times_are_authoritative(self):
        data=blank_payload()
        data['days'][2]['items']=[synthetic_entry(1, free_weekdays=[], update_time='20:00',
            schedule='20:00非会员更新1集', schedule_audience='free', events=[
                {'date': TODAY,'weekday': 3,'update_time': '10:00','schedule': 'VIP抢先看','audience': 'member'},
                {'date': TODAY,'weekday': 3,'update_time': '20:00','schedule': '非会员更新1集','audience': 'free'}]),
            synthetic_entry(2, update_times=['12:00','18:00'], update_time='12:00', schedule='当日两次更新')]
        data['unscheduled']=[synthetic_entry(90,title='无排期库存不要显示')]
        page,_=self.make_page(data)
        first=page.locator('.wc-card').first
        self.assertEqual(first.locator('.wc-update-time').inner_text(),'20:00')
        self.assertEqual(first.locator('.wc-audience').inner_text(),'免费排期')
        self.assertIn('20:00非会员更新1集',first.locator('.wc-caption').text_content())
        self.assertIn('10:00 · 会员排期',first.locator('.wc-description').text_content())
        self.assertEqual(page.locator('.wc-card').nth(1).locator('.wc-update-time').inner_text(),'12:00 / 18:00')
        self.assertEqual(page.locator('.wc-card').count(),2)
        self.assertNotIn('无排期库存',page.locator('[data-weekly-calendar]').inner_text())
        self.assertNotIn('免费进度待确认',page.locator('[data-weekly-calendar]').inner_text())

    def test_unknown_caption_and_ordinary_progress_never_invent_free(self):
        data=blank_payload()
        data['days'][2]['items']=[synthetic_entry(1,update_time='',schedule='平台尚未给出时间',
            schedule_audience='unknown',free_progress='',progress='VIP更新至第18集',events=[])]
        page,_=self.make_page(data)
        card=page.locator('.wc-card')
        self.assertEqual(card.locator('.wc-audience').inner_text(),'平台排期')
        self.assertEqual(card.locator('.wc-update-time').inner_text(),'时间未注明')
        self.assertNotIn('免费',card.inner_text())
        self.assertNotIn('18集',card.text_content())

    def test_loading_then_cards_reuse_tabs_and_controls(self):
        page,network=self.make_page(hold=True)
        page.wait_for_function('document.querySelectorAll(".wc-weekday").length === 7')
        page.evaluate('window.savedTab=document.querySelector(".wc-weekday")')
        before=page.locator('#calendar-refresh').bounding_box()
        top=page.locator('#calendar-stage').bounding_box()['y']
        self.assertEqual(page.locator('.wc-grid:visible').count(),1)
        self.assertIn('正在读取',page.locator('.wc-grid:visible').inner_text())
        network.release()
        page.locator('.wc-card:visible').first.wait_for()
        self.assertEqual(page.locator('#calendar-refresh').bounding_box(),before)
        self.assertEqual(page.locator('#calendar-stage').bounding_box()['y'],top)
        self.assertTrue(page.evaluate('savedTab===document.querySelector(".wc-weekday")'))
        self.assertEqual(page.locator('.discovery-skeleton').count(),0)

    def test_mobile_and_desktop_anime_only_platform_and_weekday_filters(self):
        for width in (1440,390,320):
            with self.subTest(width=width):
                page,network=self.make_page(width=width)
                self.assert_no_overflow(page)
                self.assertEqual(page.locator('.wc-grid:visible').count(),1)
                self.assertLess(page.locator('.wc-card:visible').first.bounding_box()['y'],480)
                if width < 500:
                    widths=page.locator('.wc-card:visible').evaluate_all('(cards)=>cards.slice(0,2).map(c=>c.getBoundingClientRect().x)')
                    self.assertLess(widths[0],widths[1])
                self.assertEqual(page.locator('.wc-categories, button[data-category], [aria-label="节目类型"]').count(),0)
                self.assertEqual(set(page.locator('.wc-card .discovery-card-source > span:first-child').all_text_contents()),{'动漫 · 2026'})
                page.evaluate('window.filterCards=[...document.querySelectorAll(".wc-card")]')
                page.locator('.wc-weekday[data-date="2026-09-08"]').click()
                for source in NAMES:
                    page.locator('#calendar-platform').select_option(source)
                    self.assertEqual(page.locator('.wc-card:visible').count(),2)
                    self.assertEqual(set(page.locator('.wc-card:visible').evaluate_all('(cards)=>cards.map(c=>c.dataset.source)')),{source})
                self.assertEqual(page.locator('.wc-grid:visible').get_attribute('data-date'),'2026-09-08')
                self.assertEqual(len(network.api_calls()),1)
                page.locator('.wc-weekday[aria-selected="true"]').focus()
                page.keyboard.press('ArrowRight')
                self.assertEqual(page.locator('.wc-grid:visible').get_attribute('data-date'),TODAY)
                page.locator('#calendar-platform').select_option('all')
                self.assertEqual(page.locator('.wc-card:visible').count(),6)
                self.assertTrue(page.evaluate('filterCards.every((card,index)=>card===document.querySelectorAll(".wc-card")[index])'))
                self.assert_no_overflow(page)
                self.screenshot(page,f'anime-synthetic-{"desktop" if width == 1440 else "mobile"}-{width}.png')

    def test_unavailable_sources_are_explicit_without_mislabeling_partial_or_stale(self):
        for width in (1440, 390, 320):
            with self.subTest(width=width):
                data = synthetic_payload()
                for day in data['days']:
                    day['items'] = [item for item in day['items'] if item['source'] != 'youku']
                data['sources'][0]['status'] = 'partial'
                data['sources'][1]['status'] = 'stale'
                data['sources'][2].update(status='unavailable', message='离线夹具：本周排期不可用')
                data.update(refreshing=True, retry_after=3600)
                page, network = self.make_page(data, width=width)
                status = page.locator('#calendar-status')
                self.assertEqual(status.inner_text(), '同步中 · 已保留当前内容')
                page.evaluate('window.sourceNodes=[...document.querySelectorAll(".wc-card, .wc-card img, .wc-weekday, .wc-empty, #calendar-status")]')
                heading = page.locator('.wc-results-heading').bounding_box()
                stage_y = page.locator('#calendar-stage').bounding_box()['y']
                button = page.locator('#calendar-refresh').bounding_box()
                # 服务已 settled；通过既有详情关闭 GET 同步，不等待真实定时器。
                network.data['refreshing'] = False
                page.locator('#discovery-detail-dialog').dispatch_event('close')
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                message = '优酷暂不可用 · 可切换其他来源'
                self.assertEqual(status.inner_text(), message)
                self.assertEqual(status.get_attribute('title'), message)
                self.assertEqual(status.get_attribute('aria-label'), message)
                self.assertEqual(status.get_attribute('data-tone'), 'warning')
                self.assertEqual(page.locator('.wc-card:visible').count(), 4)
                self.assertEqual(heading, page.locator('.wc-results-heading').bounding_box())
                self.assertEqual(stage_y, page.locator('#calendar-stage').bounding_box()['y'])
                self.assertEqual(button, page.locator('#calendar-refresh').bounding_box())
                self.assertIn('腾讯视频部分数据', page.locator('#calendar-source-toggle').get_attribute('aria-label'))
                self.assertIn('爱奇艺旧缓存', page.locator('#calendar-source-toggle').get_attribute('aria-label'))
                calls = len(network.api_calls())
                page.locator('#calendar-platform').select_option('youku')
                empty = page.locator('.wc-empty:visible')
                self.assertEqual(empty.locator('strong').inner_text(), '优酷排期暂不可用')
                self.assertIn('未能获取优酷排期', empty.inner_text())
                self.assertNotIn('暂无可确认', empty.inner_text())
                self.assertEqual(page.locator('#calendar-count').inner_text(), '0 部')
                self.assertEqual(len(network.api_calls()), calls)
                geometry = empty.bounding_box()
                self.assert_no_overflow(page)
                self.screenshot(page, f'anime-ui-source-unavailable-{width}.png')
                # 仅验证合成状态转换，不代表真实优酷恢复；partial 即使空也不误报为 unavailable。
                network.data['sources'][2]['status'] = 'partial'
                page.locator('#calendar-refresh').click()
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                self.assertEqual(status.inner_text(), '以平台排期为准 · 不代表已播出')
                self.assertEqual(status.get_attribute('title'), status.inner_text())
                self.assertEqual(status.get_attribute('aria-label'), status.inner_text())
                self.assertEqual(status.get_attribute('data-tone'), 'normal')
                self.assertIn('暂无可确认的周三更新', empty.inner_text())
                self.assertNotIn('不可用', empty.inner_text())
                self.assertEqual(geometry, empty.bounding_box())
                self.assertEqual(heading, page.locator('.wc-results-heading').bounding_box())
                self.assertEqual(button, page.locator('#calendar-refresh').bounding_box())
                self.assertTrue(page.evaluate('sourceNodes.every(node=>node.isConnected)'))
                self.assertEqual(page.locator('.discovery-skeleton').count(), 0)
                self.assert_no_overflow(page)
        # 多个及全部来源不可用：逐个点名，无可显示的其余来源时不得声称仍有覆盖。
        data = blank_payload()
        for source in data['sources']:
            source['status'] = 'partial' if source['id'] == 'tencent' else 'unavailable'
        page, network = self.make_page(data, width=320)
        self.assertEqual(page.locator('#calendar-status').inner_text(), '爱奇艺、优酷暂不可用 · 可切换其他来源')
        page.locator('#calendar-platform').select_option('iqiyi')
        self.assertEqual(page.locator('.wc-empty:visible strong').inner_text(), '爱奇艺排期暂不可用')
        page.locator('#calendar-platform').select_option('tencent')
        self.assertIn('暂无可确认', page.locator('.wc-empty:visible strong').inner_text())
        page.locator('#calendar-platform').select_option('all')
        geometry = page.locator('.wc-empty:visible').bounding_box()
        network.data['sources'][0]['status'] = 'unavailable'
        page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        message = '腾讯视频、爱奇艺、优酷暂不可用 · 请稍后重试'
        self.assertEqual(page.locator('#calendar-status').inner_text(), message)
        self.assertEqual(page.locator('#calendar-status').get_attribute('aria-label'), message)
        self.assertEqual(page.locator('.wc-empty:visible strong').inner_text(), '排期暂不可用')
        self.assertEqual(geometry, page.locator('.wc-empty:visible').bounding_box())
        self.assert_no_overflow(page)
        self.screenshot(page, 'anime-ui-sources-all-unavailable-320.png')

    def test_source_popover_accessibility_and_geometry(self):
        data=synthetic_payload(); data['sources'][0]['status']='stale'; data['sources'][1]['status']='unavailable'
        for width in (1440,320):
            with self.subTest(width=width):
                page,_=self.make_page(data,width=width)
                geometry=page.locator('.wc-grid:visible').bounding_box()
                button=page.locator('#calendar-source-toggle')
                self.assertIn('腾讯视频旧缓存',button.get_attribute('aria-label'))
                button.click()
                self.assertEqual(button.get_attribute('aria-expanded'),'true')
                self.assertEqual(page.locator('#calendar-source-panel:visible').count(),1)
                self.assertEqual(geometry,page.locator('.wc-grid:visible').bounding_box())
                self.assert_no_overflow(page)
                page.keyboard.press('Escape')
                self.assertEqual(button.get_attribute('aria-expanded'),'false')
                self.assertEqual(page.evaluate('document.activeElement.id'),'calendar-source-toggle')
                button.click(); page.locator('#calendar-heading').click()
                self.assertEqual(page.locator('#calendar-source-panel:visible').count(),0)

    def test_refresh_stale_fresh_identity_geometry_selection_and_failure(self):
        for width in (1440,390,320):
            with self.subTest(width=width):
                page,network=self.make_page(width=width)
                page.evaluate('window.oldCards=[...document.querySelectorAll(".wc-card")]; window.oldImages=oldCards.map(c=>c.querySelector("img")); window.scrollTo(0,200)')
                button=page.locator('#calendar-refresh').bounding_box(); scroll=page.evaluate('scrollY')
                sizes=page.locator('.wc-card:visible').evaluate_all('(cards)=>cards.map(c=>[c.offsetWidth,c.offsetHeight])')
                network.hold=True
                page.locator('#calendar-refresh').evaluate('(button)=>button.click()')
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "true"')
                self.assertEqual(button,page.locator('#calendar-refresh').bounding_box())
                for day in network.data['days']:
                    for item in day['items']: item['stale']=True
                network.release()
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                self.assertEqual(page.locator('.wc-cache:visible').count(),6)
                self.assertIn('上次核验',page.locator('.wc-card:visible .wc-description').first.text_content())
                self.assertTrue(page.evaluate('oldCards.every((c,i)=>c===document.querySelectorAll(".wc-card")[i] && oldImages[i]===c.querySelector("img"))'))
                self.assertEqual(scroll,page.evaluate('scrollY'))
                self.assertEqual(sizes,page.locator('.wc-card:visible').evaluate_all('(cards)=>cards.map(c=>[c.offsetWidth,c.offsetHeight])'))
                self.assertEqual(network.api_calls()[-1][2]['x-csrf-token'],'fixture-csrf-token')
                network.hold=False; network.status=503
                page.locator('#calendar-refresh').evaluate('(button)=>button.click()')
                page.wait_for_function('document.querySelector("#calendar-status").dataset.tone === "error"')
                self.assertTrue(page.evaluate('oldCards.every(c=>c.isConnected)'))
                self.assertEqual(button,page.locator('#calendar-refresh').bounding_box())
                self.assertEqual(scroll,page.evaluate('scrollY'))
                if width == 1440: self.screenshot(page,'anime-retained-error-1440.png')
                network.status=200
                for day in network.data['days']:
                    for item in day['items']: item['stale']=False
                page.locator('#calendar-refresh').evaluate('(button)=>button.click()')
                page.wait_for_function('document.querySelectorAll(".wc-card[data-stale=true]").length === 0')
                self.assertTrue(page.evaluate('oldCards.every(c=>c.isConnected)'))
                self.assertEqual(sizes,page.locator('.wc-card:visible').evaluate_all('(cards)=>cards.map(c=>[c.offsetWidth,c.offsetHeight])'))

    def test_inflight_response_keeps_new_date_and_platform_filter(self):
        page,network=self.make_page(width=390)
        network.hold=True
        page.locator('#calendar-refresh').click()
        page.locator('#calendar-platform').select_option('iqiyi')
        page.locator('.wc-weekday[data-date="2026-09-11"]').click()
        network.release()
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(page.locator('#calendar-platform').input_value(),'iqiyi')
        self.assertEqual(page.locator('.wc-grid:visible').get_attribute('data-date'),'2026-09-11')
        self.assertEqual(page.locator('.wc-card:visible').count(),2)
        self.assertEqual(set(page.locator('.wc-card:visible').evaluate_all('(cards)=>cards.map(c=>c.dataset.source)')),{'iqiyi'})

    def test_empty_partial_error_recovery_and_raw_json_contract(self):
        data=blank_payload(); data['sources'][0]['status']='partial'
        page,network=self.make_page(data)
        self.assertEqual(page.locator('.wc-empty:visible').count(),1)
        self.assertIn('暂无可确认的周三更新',page.locator('.wc-empty:visible').inner_text())
        self.assertEqual(page.locator('.wc-card').count(),0)
        network.status=503; page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector("#calendar-status").dataset.tone === "error"')
        self.assertIn('排期暂不可用',page.locator('.wc-empty:visible').inner_text())
        network.status=200; network.data=synthetic_payload()
        page.locator('.wc-empty:visible button').click(); page.locator('.wc-card:visible').first.wait_for()
        network.data={'data':synthetic_payload()}
        page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector("#calendar-status").dataset.tone === "error"')
        self.assertEqual(page.locator('.wc-card:visible').count(),6)
        network.status=503; page.reload(wait_until='domcontentloaded')
        page.wait_for_function('document.querySelector("#calendar-status").dataset.tone === "error"')
        self.assertEqual(page.locator('#calendar-source-panel [data-status="unavailable"]').count(),3)

    def test_synthetic_malicious_urls_titles_overview_and_rating_are_inert(self):
        data=blank_payload()
        urls=['javascript:alert(1)','https://v.qq.com.evil.test/a','https://www.iqiyi.com/a','https://v.qq.com@evil.test/a','http://v.qq.com/a','https://v.qq.com:444/a']
        posters=['https://evil.test/a.png','//evil.test/a','/discovery-poster/tmdb/../../api/config','/discovery-poster/tmdb/\\evil.test/a']
        for index,url in enumerate(urls):
            data['days'][2]['items'].append(synthetic_entry(index+100,url=url,poster_url=posters[index%4],
                tmdb_id='1/../x',rating=[None,-1,11,True,'8.2',{}][index],title='<img src=x onerror="window.pwned=1">恶意标题',
                overview='<svg onload="window.pwned=1">简介',schedule='<script>window.pwned=1</script>',update_time='25:99'))
        page,network=self.make_page(data)
        self.assertEqual(page.locator('.wc-card a[href]').count(),0)
        self.assertEqual(page.locator('.wc-card img[src]').count(),0)
        self.assertEqual(page.locator('.discovery-card-title img,.wc-description svg,.wc-caption script').count(),0)
        self.assertTrue(all(value=='★ 暂无' for value in page.locator('.discovery-rating').all_text_contents()))
        self.assertEqual(page.locator('.wc-update-time').first.inner_text(),'时间未注明')
        self.assertIn('<img',page.locator('.discovery-card-title').first.inner_text())
        self.assertIsNone(page.evaluate('window.pwned'))
        self.assertEqual(network.unexpected,[])

    def test_valid_link_whitelist_and_score_boundaries(self):
        data=blank_payload()
        hosts={'tencent':['v.qq.com','m.v.qq.com'],'iqiyi':['www.iqiyi.com','m.iqiyi.com'],'youku':['www.youku.com','v.youku.com','m.youku.com','youku.com']}
        for source,values in hosts.items():
            for host in values:
                data['days'][2]['items'].append(synthetic_entry(len(data['days'][2]['items'])+1,source,url=f'https://{host}/fixture',tmdb_id='00123',rating=0 if source=='tencent' else 10))
        page,_=self.make_page(data)
        self.assertEqual(page.locator('.wc-card a[href]').count(),16)
        self.assertEqual(page.locator('a[href="/discovery?detail_provider=tmdb&detail_type=tv&detail_id=00123"]').count(),16)
        self.assertEqual(page.locator('.discovery-rating').first.inner_text(),'★ 0.0')
        self.assertEqual(page.locator('.discovery-rating').last.inner_text(),'★ 10.0')
        self.assertTrue(page.locator('.wc-card a[href]').evaluate_all('(links)=>links.every(a=>a.hasAttribute("data-media-profile-link") && new URL(a.href).origin===location.origin && a.target!=="_blank")'))

    def test_hidden_poll_minimum_and_bfcache_resume(self):
        data=synthetic_payload(); data['refreshing']=True; data['retry_after']=0
        page,network=self.make_page(data,width=390)
        page.clock.install(); page.reload(wait_until='domcontentloaded')
        page.wait_for_function('document.querySelectorAll(".wc-card").length === 42')
        # 从暂停时钟的确定起点重新排 poll，排除详情脚本/图标加载消耗的真实时间。
        page.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:true}); document.dispatchEvent(new Event("visibilitychange"))')
        page.clock.pause_at(page.evaluate('Date.now()') / 1000 + 1)
        page.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:false}); document.dispatchEvent(new Event("visibilitychange"))')
        before=len(network.api_calls()); page.clock.run_for(4999)
        self.assertEqual(len(network.api_calls()),before)
        page.clock.run_for(2); page.wait_for_timeout(10)
        self.assertEqual(len(network.api_calls()),before+1)
        page.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:true}); document.dispatchEvent(new Event("visibilitychange"))')
        before=len(network.api_calls()); page.clock.run_for(20000)
        self.assertEqual(len(network.api_calls()),before)
        network.data['refreshing']=False
        page.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:false}); document.dispatchEvent(new Event("visibilitychange"))')
        page.clock.run_for(5001); page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        page.locator('.wc-weekday[data-date="2026-09-11"]').click()
        page.evaluate('window.savedCards=[...document.querySelectorAll(".wc-card")]; window.dispatchEvent(new PageTransitionEvent("pagehide",{persisted:true}))')
        before=len(network.api_calls()); page.clock.run_for(20000)
        self.assertEqual(len(network.api_calls()),before)
        page.evaluate('window.dispatchEvent(new PageTransitionEvent("pageshow",{persisted:true}))')
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(len(network.api_calls()),before+1)
        self.assertEqual(page.locator('.wc-grid:visible').get_attribute('data-date'),'2026-09-11')
        self.assertTrue(page.evaluate('savedCards.every((c,i)=>c===document.querySelectorAll(".wc-card")[i])'))
        before=len(network.api_calls()); page.clock.run_for(20000)
        self.assertEqual(len(network.api_calls()),before)
        self.assertTrue(all(call[0]=='GET' for call in network.api_calls()))

    def test_week_rollover_and_no_invented_inventory_dates(self):
        page,network=self.make_page(width=320)
        page.locator('#calendar-platform').select_option('youku')
        network.data['week_start']='2026-09-14'; network.data['today']='2026-09-16'
        for index,day in enumerate(network.data['days']):
            day['date']=f'2026-09-{14+index:02}'
            for item in day['items']:
                for event in item['events']: event['date']=day['date']
        page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector(".wc-grid:not([hidden])").dataset.date === "2026-09-16"')
        self.assertEqual(page.locator('.wc-weekday').count(),7)
        self.assertEqual(page.locator('#calendar-platform').input_value(),'youku')
        self.assertEqual(page.locator('[data-day="2026-09-09"]').count(),0)
        self.assertNotIn('不可作为日历内容',page.locator('[data-weekly-calendar]').inner_text())
        self.assert_no_overflow(page)

    def test_light_reduced_motion_and_websockets_are_isolated(self):
        page,network=self.make_page(width=320)
        page.emulate_media(reduced_motion='reduce'); page.evaluate('document.documentElement.dataset.theme="light"')
        self.assert_no_overflow(page)
        self.assertEqual(page.locator('script[src="/static/js/free-calendar.js"][defer]').count(),1)
        self.assertEqual(page.locator('script[src="/static/js/discovery.js"][defer]').count(),1)
        platform = page.locator('#calendar-platform')
        before = platform.bounding_box()
        platform.focus()
        focus = platform.evaluate('(el)=>({outline:getComputedStyle(el).outlineStyle, shadow:getComputedStyle(el).boxShadow})')
        self.assertEqual(focus['outline'], 'none')
        self.assertNotEqual(focus['shadow'], 'none')
        self.assertEqual(platform.bounding_box(), before)
        page.evaluate('new WebSocket("wss://blocked.invalid/fixture")'); page.wait_for_timeout(20)
        self.assertEqual(network.websockets,['wss://blocked.invalid/fixture'])

    def test_profile_only_existing_dialog_and_no_video_links(self):
        data=blank_payload()
        data['days'][2]['items']=[synthetic_entry(1),synthetic_entry(2,tmdb_id='',douban_id='1292052')]
        page,network=self.make_page(data, resource_results_enabled=False)
        paths=[urlsplit(call[1]).path for call in network.calls]
        self.assertFalse(any(path in ['/api/discovery/sections','/api/discovery/items','/api/discovery/search','/api/discovery/watchlist'] for path in paths))
        self.assertEqual(page.locator('[data-discovery-profile-host="true"]').count(),1)
        self.assertEqual(page.locator('#discovery-detail-dialog').count(),1)
        self.assertTrue(page.locator('.wc-card a[href]').evaluate_all('(links)=>links.every(a=>new URL(a.href).origin===location.origin && new URL(a.href).pathname==="/discovery")'))
        for index,provider in [(0,'tmdb'),(1,'douban')]:
            page.locator('.wc-card').nth(index).locator('.discovery-card-open').click()
            page.locator('#discovery-detail-dialog[open]').wait_for()
            page.wait_for_function('(provider)=>document.querySelector("#discovery-detail-body").textContent.includes("详情夹具 "+provider)',arg=provider)
            self.assertEqual(page.url,ORIGIN+'/discovery/calendar')
            self.assertEqual(len(page.context.pages),1)
            page.locator('[data-discovery-dialog-close]').click()
            page.wait_for_function('!document.querySelector("#discovery-detail-dialog").open && document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(network.unexpected,[])
        self.assertFalse(any(urlsplit(call[1]).path.startswith('/api/indexers/') for call in network.calls))
        detail_paths=[urlsplit(call[1]).path for call in network.calls if urlsplit(call[1]).path.startswith('/api/discovery/detail/')]
        self.assertEqual(detail_paths,['/api/discovery/detail/tmdb/tv/12301','/api/discovery/detail/douban/tv/1292052'])
        self.assertFalse(any(urlsplit(call[1]).path in ['/api/discovery/sections','/api/discovery/items'] for call in network.calls))

    def test_card_resource_switch_uses_shared_dialog_and_keeps_calendar_context(self):
        self._assert_card_resource_switch_context(keyboard=False)

    def test_keyboard_reopen_ignores_previous_close_event_in_both_resource_modes(self):
        self._assert_card_resource_switch_context(keyboard=True)

    def _assert_card_resource_switch_context(self, *, keyboard):
        for enabled in (False, True):
            for width in (1440, 390):
                with self.subTest(resource_results_enabled=enabled, width=width):
                    data = blank_payload()
                    data['days'][2]['items'] = [
                        synthetic_entry(1, douban_id='1292052'),  # 双身份必须优先 TMDB。
                        synthetic_entry(2, tmdb_id='', douban_id='1292052'),
                        synthetic_entry(3, 'youku', tmdb_id='', douban_id='', watchlist=None,
                                        mapping_status='unmatched', poster_url=''),
                    ]
                    page, network = self.make_page(data, width=width, resource_results_enabled=enabled)
                    calendar_url = page.url
                    self.assertEqual(page.locator('[data-discovery-profile-host]').get_attribute(
                        'data-resource-results-enabled'), str(enabled).lower())
                    self.assertEqual(page.locator('script[src="/static/js/discovery.js"][defer]').count(), 1)
                    self.assertEqual(page.locator('#discovery-detail-dialog').count(), 1)
                    for index, (provider, identity) in enumerate((('tmdb', '12301'), ('douban', '1292052'))):
                        title = f'详情夹具 {provider} {identity}'
                        # 同 test_indexer_api / test_discovery_search_ui 的公开资源契约；不含下载地址。
                        network.indexer_payload = {
                            'query': title, 'page': 1, 'has_more': False, 'partial': False, 'cached': False,
                            'items': [{'result_id': 'opaque-result', 'site_id': 'nyaa', 'site_name': 'Nyaa',
                                       'title': 'Demo.Show.S01E01.1080p.WEB-DL', 'size_text': '1 GiB',
                                       'seeders': 12, 'download_state': 'ready', 'download_kinds': ['magnet']}],
                            'sites_attempted': ['nyaa'], 'sites_succeeded': ['nyaa'], 'errors': [],
                            'site_statuses': [{'site_id': 'nyaa', 'site_name': 'Nyaa', 'status': 'success',
                                               'count': 1, 'query': title, 'attempts': 1,
                                               'pagination_supported': False, 'has_more': False}],
                        }
                        link = page.locator('.wc-card').nth(index).locator('.discovery-card-open')
                        self.assertEqual(link.get_attribute('href'),
                                         f'/discovery?detail_provider={provider}&detail_type=tv&detail_id={identity}')
                        if keyboard:
                            link.focus()
                            if index == 1:
                                # 用户已选择下一张卡片，再执行上一轮待恢复焦点的帧回调。
                                page.evaluate('''() => {
                                    window.requestAnimationFrame = window.originalCloseFrame;
                                    for (const callback of window.pendingCloseFrames.splice(0)) callback(performance.now());
                                }''')
                                self.assertTrue(link.evaluate('(e) => document.activeElement === e'))
                            page.keyboard.press('Enter')
                        else:
                            link.click()
                        page.locator('#discovery-detail-dialog[open]').wait_for()
                        if keyboard and index == 1:
                            # 原生 close 事件可晚于下一张 showModal；确定性补派旧事件，不能取消新请求。
                            page.evaluate("document.querySelector('#discovery-detail-dialog').dispatchEvent(new Event('close'))")
                        if enabled:
                            row = page.locator('[data-resource-result-id="opaque-result"]')
                            row.wait_for()
                            self.assertIn('Demo.Show.S01E01.1080p.WEB-DL', row.inner_text())
                            self.assertIn(title, page.locator('#discovery-detail-title').inner_text())
                            self.assertEqual(page.locator('[data-discovery-resource-panel]').count(), 1)
                            self.assertIn('检索成功', page.locator('[data-resource-site-filter="nyaa"]').get_attribute('aria-label'))
                            self.assertEqual(page.locator('.discovery-detail-layout').count(), 0)
                            call = network.indexer_calls[-1]
                            self.assertEqual((call['method'], call['path']), ('POST', '/api/indexers/search'))
                            self.assertEqual(call['headers']['content-type'], 'application/json')
                            self.assertEqual(call['headers']['x-csrf-token'], 'fixture-csrf-token')
                            self.assertEqual(call['body'], {
                                'title': title, 'original_title': '', 'english_title': '', 'aliases': [],
                                'year': '2026', 'media_type': 'tv', 'sort_mode': 'relevance_desc', 'page': 1,
                            })
                        else:
                            page.locator('.discovery-detail-layout').wait_for()
                            self.assertIn(title, page.locator('.discovery-detail-layout').inner_text())
                            self.assertEqual(page.locator('[data-discovery-resource-panel]').count(), 0)
                        self.assertEqual(len(network.indexer_calls), index + 1 if enabled else 0)
                        self.assertEqual(page.url, calendar_url)
                        self.assertEqual(len(page.context.pages), 1)
                        if keyboard and index == 0:
                            page.evaluate('''() => {
                                window.originalCloseFrame = window.requestAnimationFrame;
                                window.pendingCloseFrames = [];
                                window.requestAnimationFrame = (callback) => window.pendingCloseFrames.push(callback);
                            }''')
                        page.locator('[data-discovery-dialog-close]').click()
                        page.wait_for_function('!document.querySelector("#discovery-detail-dialog").open && document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"', polling=10)
                        page.wait_for_function('(index) => document.activeElement === document.querySelectorAll(".wc-card .discovery-card-open")[index]', arg=index, polling=10)
                        if keyboard and index == 0:
                            page.wait_for_function('window.pendingCloseFrames.length > 0', polling=10)
                        self.assertEqual(page.url, calendar_url)
                        self.assertEqual(len(network.indexer_calls), index + 1 if enabled else 0)
                    # 无媒体身份仍只打开已验证的平台原页，不尝试档案/资源搜索。
                    original_url = data['days'][2]['items'][2]['url']
                    network.platform_pages = {original_url}
                    original = page.locator('.wc-card').nth(2).locator('.discovery-card-open')
                    self.assertEqual(original.get_attribute('href'), original_url)
                    self.assertIsNone(original.get_attribute('data-media-profile-link'))
                    with page.expect_popup() as popup_event:
                        original.click()
                    popup = popup_event.value
                    popup.wait_for_load_state('domcontentloaded')
                    self.assertEqual(popup.url, original_url)
                    self.assertTrue(popup.evaluate('window.opener === null'))
                    popup.close()
                    self.assertEqual(network.platform_calls, [original_url])
                    self.assertEqual(page.url, calendar_url)
                    self.assertFalse(page.locator('#discovery-detail-dialog').evaluate('(dialog) => dialog.open'))
                    api_calls = [(method, urlsplit(url).path) for method, url, _ in network.calls
                                 if urlsplit(url).path.startswith('/api/')]
                    self.assertEqual([path for _, path in api_calls if path.startswith('/api/discovery/detail/')],
                                     ['/api/discovery/detail/tmdb/tv/12301', '/api/discovery/detail/douban/tv/1292052'])
                    self.assertEqual([call for call in api_calls if call[1].startswith('/api/indexers/')],
                                     [('POST', '/api/indexers/search')] * (2 if enabled else 0))
                    self.assertTrue(all(method == 'GET' or path in ('/api/indexers/search', '/api/discovery/map')
                                        for method, path in api_calls), api_calls)
                    self.assertFalse(any(path in ('/api/discovery/sections', '/api/discovery/items',
                                                  '/api/discovery/search', '/api/discovery/watchlist')
                                         for _, path in api_calls))
                    self.assert_no_overflow(page)

    def test_mapping_status_labels_and_tmdb_id_priority_keep_footer_nodes_and_height(self):
        for width in (1440, 320):
            with self.subTest(width=width):
                data = blank_payload()
                data['days'][2]['items'] = [
                    synthetic_entry(1, tmdb_id='', poster_url='', mapping_status='pending'),
                    synthetic_entry(2, tmdb_id='', poster_url='', mapping_status='unmatched'),
                    synthetic_entry(3, tmdb_id='', poster_url='', mapping_status='not_configured'),
                    synthetic_entry(4, tmdb_id='', douban_id='1292052', poster_url='', mapping_status='matched'),
                    synthetic_entry(5, poster_url='', mapping_status='pending'),
                    synthetic_entry(6, poster_url='', mapping_status='not_configured'),
                ]
                page, network = self.make_page(data, width=width)
                links = page.locator('.wc-tmdb-link')
                self.assertEqual(links.locator('span').all_text_contents(), [
                    'TMDB 匹配中', 'TMDB 未匹配', 'TMDB 未配置', 'TMDB 未匹配', 'TMDB 已映射', 'TMDB 已映射',
                ])
                self.assertIn('已匹配豆瓣资料', links.nth(3).get_attribute('title'))
                for index in range(6):
                    self.assertIn(links.nth(index).inner_text(), links.nth(index).get_attribute('aria-label'))
                    self.assertEqual(links.nth(index).get_attribute('href') is not None, index >= 4)
                page.evaluate('window.mappingNodes=[...document.querySelectorAll(".wc-card, .wc-card img, .wc-tmdb-link, .wc-tmdb-link span, .discovery-watchlist-action")]')
                measure = '(cards)=>cards.map(c=>[c.offsetWidth,c.offsetHeight,c.querySelector(".discovery-card-footer").offsetHeight])'
                geometry = page.locator('.wc-card').evaluate_all(measure)
                self.assert_no_overflow(page)
                self.screenshot(page, f'anime-ui-mapping-states-{width}.png')
                for state, label in [('unmatched', 'TMDB 未匹配'), ('not_configured', 'TMDB 未配置'),
                                     ('pending', 'TMDB 匹配中'), ('matched', 'TMDB 未匹配'),
                                     (None, 'TMDB 未匹配'), ('__proto__', 'TMDB 未匹配')]:
                    for item in network.data['days'][2]['items']:
                        if state is None:
                            item.pop('mapping_status', None)
                        else:
                            item['mapping_status'] = state
                    page.locator('#calendar-refresh').click()
                    page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                    self.assertEqual(links.locator('span').all_text_contents(), [label] * 4 + ['TMDB 已映射'] * 2)
                    self.assertTrue(page.evaluate('mappingNodes.every(node=>node.isConnected)'))
                    self.assertEqual(geometry, page.locator('.wc-card').evaluate_all(measure))
                self.assertEqual(network.poster_calls, [])
                self.assertEqual(network.watch_calls, [])
                self.assert_no_overflow(page)

    def test_poster_notes_distinguish_unmatched_missing_and_failed_without_replacing_nodes(self):
        primary = '/discovery-poster/douban/fixture-81'
        fallback = '/discovery-poster/tmdb/fixture-82'
        success = '/discovery-poster/tmdb/fixture-83'
        for width in (1440, 390, 320):
            with self.subTest(width=width):
                data = blank_payload()
                data['days'][2]['items'] = [
                    synthetic_entry(1, 'iqiyi', title='未匹配 · 无可用海报地址', tmdb_id='', douban_id='', mapping_status='unmatched',
                                    poster_url='https://bad.invalid/poster.png', poster_urls=[
                                        '//bad.invalid/poster.png', '/discovery-poster/tmdb/../../api/config',
                                        '/discovery-poster/tmdb/fixture-80?redirect=evil', '/discovery-poster/unknown/fixture-80',
                                    ]),
                    synthetic_entry(2, title='已匹配 TMDB · 暂无海报', poster_url='', mapping_status='matched'),
                    synthetic_entry(3, title='合法封面及备用图加载失败', tmdb_id='', mapping_status='unmatched',
                                    poster_urls=[primary, fallback, primary], poster_url=primary),
                    synthetic_entry(4, title='已匹配豆瓣 · 暂无海报', tmdb_id='', douban_id='1292052', poster_url='', mapping_status='matched'),
                    synthetic_entry(5, title='有效 TMDB ID 优先于旧状态', poster_url='', mapping_status='unmatched'),
                ]
                page, network = self.make_page(data, width=width, poster_failures=[primary, fallback])
                page.wait_for_function('document.querySelectorAll(".wc-poster-note")[2].textContent === "封面加载失败"')
                cards = page.locator('.wc-card')
                notes = cards.locator('.wc-poster-note')
                self.assertEqual(notes.all_text_contents(), ['暂未匹配海报', '暂无海报', '封面加载失败', '暂无海报', '暂无海报'])
                self.assertEqual(page.locator('.wc-poster-note:visible').count(), 5)
                self.assertIn('尚未匹配 TMDB 或豆瓣', notes.nth(0).get_attribute('title'))
                self.assertIn('尚无可用海报地址', notes.nth(1).get_attribute('title'))
                self.assertIn('刷新日历重试', notes.nth(2).get_attribute('title'))
                self.assertEqual(network.poster_calls, [primary, fallback])
                self.assertEqual(cards.nth(0).locator('img[src]').count(), 0)
                self.assertEqual(cards.nth(4).locator('.wc-tmdb-link').inner_text(), 'TMDB 已映射')
                page.evaluate('window.posterNodes=[...document.querySelectorAll(".wc-card, .wc-card img, .wc-poster-note, .wc-tmdb-link, .discovery-watchlist-action")]')
                measure = '(cards)=>cards.map(c=>[c.offsetWidth,c.offsetHeight,c.querySelector(".discovery-poster").offsetHeight])'
                geometry = cards.evaluate_all(measure)
                self.assertEqual(notes.evaluate_all('(notes)=>notes.map(note=>note.offsetHeight)'), [14] * 5)
                self.assert_no_overflow(page)
                self.screenshot(page, f'anime-ui-poster-states-{width}.png')
                # 同一节点：合法图可用就加载，不因 unmatched 文案挡图；撤销地址后不能遗留旧网络失败提示。
                network.data['days'][2]['items'][0].update(poster_url=success, poster_urls=[])
                network.data['days'][2]['items'][2].update(poster_url='', poster_urls=[])
                page.locator('#calendar-refresh').click()
                page.wait_for_function('document.querySelector(".wc-card img").naturalWidth > 0 && !document.querySelector(".wc-card img").hidden')
                self.assertTrue(notes.nth(0).is_hidden())
                self.assertEqual(notes.nth(2).inner_text(), '暂未匹配海报')
                self.assertEqual(cards.nth(2).locator('img[src]').count(), 0)
                self.assertEqual(network.poster_calls, [primary, fallback, success])
                self.assertTrue(page.evaluate('posterNodes.every(node=>node.isConnected)'))
                self.assertEqual(geometry, cards.evaluate_all(measure))
                network.data['days'][2]['items'][0].update(poster_url='', poster_urls=[])
                network.data['days'][2]['items'][2]['mapping_status'] = 'pending'
                page.locator('#calendar-refresh').click()
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                self.assertEqual(notes.all_text_contents(), ['暂未匹配海报', '暂无海报', '暂无海报', '暂无海报', '暂无海报'])
                self.assertEqual(cards.locator('img[src]').count(), 0)
                self.assertEqual(network.poster_calls, [primary, fallback, success])
                self.assertTrue(page.evaluate('posterNodes.every(node=>node.isConnected)'))
                self.assertEqual(geometry, cards.evaluate_all(measure))
                self.assert_no_overflow(page)

    def test_mixed_poster_fallback_is_bounded_and_keeps_success_on_refresh(self):
        data=blank_payload()
        primary='/discovery-poster/douban/fixture-50'
        fallback='/discovery-poster/tmdb/fixture-51'
        data['days'][2]['items']=[synthetic_entry(1,poster_urls=['https://bad.invalid/evil',primary,fallback,primary],poster_url=primary)]
        page,network=self.make_page(data,poster_failures=[primary])
        page.wait_for_function('document.querySelector(".wc-card img").naturalWidth>0')
        image=page.locator('.wc-card img'); before=image.bounding_box()
        self.assertTrue(image.get_attribute('src').endswith(fallback))
        self.assertEqual(network.poster_calls,[primary,fallback])
        page.evaluate('window.fallbackImage=document.querySelector(".wc-card img")')
        page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertTrue(page.evaluate('fallbackImage===document.querySelector(".wc-card img")'))
        self.assertTrue(image.get_attribute('src').endswith(fallback))
        self.assertEqual(network.poster_calls,[primary,fallback])
        self.assertEqual(image.bounding_box(),before)
        # 全部失败不会无限循环；明确手工刷新允许一次有限重试。
        other=blank_payload(); other['days'][2]['items']=[synthetic_entry(1,poster_urls=[primary,fallback],poster_url=primary)]
        page2,network2=self.make_page(other,poster_failures=[primary,fallback])
        page2.wait_for_function('document.querySelector(".wc-card img").hidden && document.querySelector(".wc-card img").getAttribute("src")')
        self.assertEqual(network2.poster_calls,[primary,fallback])
        self.assertEqual(page2.locator('.wc-poster-note').inner_text(),'封面加载失败')
        failed_geometry=page2.locator('.wc-card').bounding_box()
        # 普通 GET 不重试已全失败候选；手动刷新每次最多尝试这一轮候选。
        page2.locator('#discovery-detail-dialog').dispatch_event('close')
        page2.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(network2.poster_calls,[primary,fallback])
        page2.locator('#calendar-refresh').click()
        page2.wait_for_function('document.querySelector(".wc-card img").hidden && document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        page2.wait_for_timeout(50)
        self.assertEqual(network2.poster_calls,[primary,fallback,primary,fallback])
        self.assertEqual(page2.locator('.wc-poster-note').inner_text(),'封面加载失败')
        self.assertEqual(page2.locator('.wc-card').bounding_box(),failed_geometry)
        network2.poster_failures.clear()
        page2.locator('#calendar-refresh').click()
        page2.wait_for_function('document.querySelector(".wc-card img").naturalWidth>0 && !document.querySelector(".wc-card img").hidden')
        self.assertEqual(network2.poster_calls,[primary,fallback,primary,fallback,primary])
        self.assertTrue(page2.locator('.wc-poster-note').is_hidden())
        self.assertEqual(page2.locator('.wc-card').bounding_box(),failed_geometry)

    def test_watchlist_identity_token_and_cross_day_single_flight(self):
        data=synthetic_payload()
        # 收藏用 TMDB 身份，但主海报来自豆瓣；不能从图片路径提取收藏 token。
        for day in data['days']:
            day['items'][0]['poster_urls']=['/discovery-poster/douban/fixture-70']
            day['items'][0]['poster_url']='/discovery-poster/douban/fixture-70'
        page,network=self.make_page(data)
        network.watch_hold=True
        button=page.locator('.wc-grid:visible .discovery-watchlist-action').first
        geometry=button.bounding_box()
        key=button.get_attribute('data-watchlist-key')
        buttons=page.locator(f'.discovery-watchlist-action[data-watchlist-key="{key}"]')
        self.assertEqual(buttons.count(),7)
        # 隔离收藏状态几何，不把探索卡片已有的 hover 位移混入前后对比。
        button.evaluate('(b)=>b.click()')
        self.assertTrue(buttons.evaluate_all('(buttons)=>buttons.every(b=>!b.disabled && b.getAttribute("aria-disabled")==="true" && b.getAttribute("aria-pressed")==="true" && b.getAttribute("aria-busy")==="true")'))
        buttons.last.evaluate('(b)=>b.click()')
        page.wait_for_timeout(20)
        self.assertEqual(len(network.watch_calls),1)
        call=network.watch_calls[0]; body=json.loads(call['body'])
        self.assertEqual(call['method'],'POST')
        self.assertEqual(call['headers']['x-csrf-token'],'fixture-csrf-token')
        self.assertEqual(body['provider'],'tmdb')
        self.assertEqual(body['media_type'],'tv')
        self.assertEqual(key,'tmdb:tv:12301')
        self.assertEqual(body['external_id'],'12301')
        self.assertEqual(body['poster_token'],'tmdb-fixture-token')
        self.assertEqual(button.bounding_box(),geometry)
        network.release_watch()
        page.wait_for_function('document.querySelector(".wc-grid:not([hidden]) .discovery-watchlist-action").getAttribute("aria-busy") === "false"')
        self.assertTrue(buttons.evaluate_all('(buttons)=>buttons.every(b=>b.getAttribute("aria-pressed")==="true")'))
        self.assertEqual(button.bounding_box(),geometry)
        network.watch_hold=False
        button.click()
        page.wait_for_function('document.querySelector(".wc-grid:not([hidden]) .discovery-watchlist-action").getAttribute("aria-busy")==="false"')
        self.assertEqual(network.watch_calls[-1]['path'],'/api/discovery/watchlist/tmdb/tv/12301')
        self.assertEqual(network.watch_calls[-1]['method'],'DELETE')
        self.assertTrue(buttons.evaluate_all('(buttons)=>buttons.every(b=>b.getAttribute("aria-pressed")==="false")'))

    def test_watchlist_old_calendar_response_cannot_erase_click_and_failure_rolls_back(self):
        page,network=self.make_page()
        network.hold=True; network.watch_hold=True
        page.locator('#calendar-refresh').click()
        button=page.locator('.wc-grid:visible .discovery-watchlist-action').first
        button.click(); network.release_watch()
        page.wait_for_function('document.querySelector(".wc-grid:not([hidden]) .discovery-watchlist-action").getAttribute("aria-busy") === "false"')
        network.release()
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(button.get_attribute('aria-pressed'),'true')
        network.watch_hold=False; network.watch_status=503
        button.click()
        page.wait_for_function('document.querySelector("#calendar-status").textContent.includes("收藏操作失败")')
        self.assertEqual(button.get_attribute('aria-pressed'),'true')
        self.assertEqual(button.get_attribute('aria-busy'),'false')
        self.assertFalse(button.is_disabled())
        # 后续新发起的日历请求可重新采用实时 DB 状态，而非永远覆盖远端变化。
        network.hold=False; page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(button.get_attribute('aria-pressed'),'false')

    def test_douban_watchlist_and_unmatched_or_forged_identity_are_safe(self):
        data=blank_payload()
        data['days'][2]['items']=[synthetic_entry(1,tmdb_id='',douban_id='1292052',poster_url='/discovery-poster/tmdb/fixture-1'),
            synthetic_entry(2,tmdb_id='',douban_id='',watchlist=None,detail_url=''),
            synthetic_entry(3,tmdb_id='',douban_id='',watchlist={'provider': 'tmdb','external_id': 'fixture-3','media_type': 'tv','poster_token': '','in_watchlist': False},detail_url='/discovery?detail_provider=tmdb&detail_type=tv&detail_id=999'),
            synthetic_entry(4,detail_url='https://v.qq.com/x',watchlist={'provider': 'tmdb','external_id': '12304','media_type': 'tv','poster_token': '','in_watchlist': True})]
        page,network=self.make_page(data)
        cards=page.locator('.wc-card')
        self.assertIn('TMDB 未匹配',cards.first.inner_text())
        cards.first.locator('.discovery-watchlist-action').click()
        page.wait_for_function('document.querySelector(".wc-card .discovery-watchlist-action").getAttribute("aria-busy")==="false"')
        body=json.loads(network.watch_calls[0]['body'])
        self.assertEqual((body['provider'],body['media_type'],body['external_id'],body['poster_token']),('douban','tv','1292052','douban-fixture-token'))
        for index in (1,2):
            self.assertTrue(cards.nth(index).locator('.discovery-watchlist-action').is_disabled())
        # 无资料身份可复用封面进入安全平台原页；伪造的详情不能成为资料链接。
        self.assertEqual(cards.nth(1).locator('.discovery-card-open').get_attribute('href'), data['days'][2]['items'][1]['url'])
        self.assertIsNone(cards.nth(1).locator('.discovery-card-open').get_attribute('data-media-profile-link'))
        self.assertEqual(cards.nth(2).locator('a[href]').count(), 0)
        self.assertEqual(cards.nth(3).locator('.discovery-watchlist-action').get_attribute('aria-pressed'),'true')
        self.assertFalse(any(call['method']=='GET' for call in network.watch_calls))
        self.assertIsNone(cards.nth(3).locator('.discovery-card-open').get_attribute('href'))
        self.assertEqual(page.locator('.wc-card a[href^="https://"]').count(),1)


    def wait_for_fixture(self, page, ready):
        deadline = clock.monotonic() + 6
        while not ready() and clock.monotonic() < deadline:
            page.wait_for_timeout(10)
        self.assertTrue(ready(), '离线路由应在浏览器超时内抵达')

    def remember_poster_layout(self, page):
        # 仅等待已知的初次 Lucide 占位替换完成，再采样全部元素；不使用猜测性的固定延迟。
        page.wait_for_function("document.querySelector('.wc-card') && !document.querySelector('.wc-card i[data-lucide]')")
        page.evaluate('''() => {
          window.posterElements = [...document.querySelectorAll('.wc-card, .wc-card *')];
          window.posterChanges = [];
          window.posterObserver = new MutationObserver(records => {
            for (const r of records) {
              if (r.type === 'attributes' && r.target.tagName === 'IMG') posterChanges.push(r.attributeName);
              if (r.type === 'childList' && [...r.addedNodes, ...r.removedNodes].some(n => n.nodeType === 1)) posterChanges.push('element');
            }
          });
          posterObserver.observe(document.querySelector('#calendar-days'), {
            subtree: true, childList: true, attributes: true, attributeFilter: ['src', 'hidden', 'style']
          });
        }''')
        return self.poster_layout(page)

    def poster_layout(self, page):
        return page.locator('.wc-card, .wc-card .discovery-poster, .discovery-card-footer, .discovery-watchlist-action, .wc-controls, .wc-tools, #calendar-stage').evaluate_all('''nodes => nodes.map(n => {
          const r = n.getBoundingClientRect(); return [r.x, r.y, r.width, r.height];
        })''')

    def assert_poster_layout(self, page, geometry):
        self.assertTrue(page.evaluate('''() => {
          const current = [...document.querySelectorAll('.wc-card, .wc-card *')];
          return current.length === posterElements.length && current.every((n, i) => n === posterElements[i]);
        }'''), '封面状态不得新增、移除或替换卡片及内部元素')
        self.assertEqual(self.poster_layout(page), geometry)
        self.assert_no_overflow(page)

    def test_platform_only_unmatched_posters_keep_identity_and_responsive_geometry(self):
        label = '平台原图，仅作封面，不代表已匹配资料'
        for width in (1440, 390, 320):
            with self.subTest(width=width):
                data = blank_payload()
                paths = [f'/discovery-calendar-poster/{source}/fixture-{200 + index}' for index, source in enumerate(NAMES)]
                for index, (source, path) in enumerate(zip(NAMES, paths)):
                    data['days'][2]['items'].append(synthetic_entry(index + 200, source, tmdb_id='', douban_id='',
                        watchlist=None, rating=None, mapping_status='unmatched', poster_provider=f'calendar-{source}',
                        poster_url=path, poster_urls=[path]))
                page, network = self.make_page(data, width=width, poster_holds=paths)
                cards = page.locator('.wc-card')
                geometry = self.remember_poster_layout(page)
                self.assertEqual(cards.locator('.wc-poster-note').all_text_contents(), ['封面加载中'] * 3)
                self.assertEqual(cards.locator('img').evaluate_all('(images)=>images.map(i=>i.title)'), [''] * 3)
                for path in paths:
                    page.wait_for_function('(path)=>[...document.images].some(i=>i.src.endsWith(path))', arg=path)
                    self.wait_for_fixture(page, lambda: path in network.poster_calls)
                    network.release_poster(path)
                page.wait_for_function('''() => [...document.querySelectorAll('.wc-card img')].every(i =>
                  i.complete && i.naturalWidth === 640 && !i.hidden && i.style.opacity === '1')''')
                self.assertCountEqual(network.poster_calls, paths)
                self.assertEqual(cards.locator('img').evaluate_all('(images)=>images.map(i=>i.title)'), [label] * 3)
                self.assertEqual(cards.locator('img').evaluate_all('(images)=>images.map(i=>i.alt)'), [label] * 3)
                self.assertEqual(cards.locator('.wc-tmdb-link span').all_text_contents(), ['TMDB 未匹配'] * 3)
                self.assertEqual(cards.locator('.wc-tmdb-link.is-mapped, .wc-tmdb-link[href]').count(), 0)
                self.assertTrue(cards.locator('.wc-poster-note').evaluate_all('(notes)=>notes.every(n=>n.hidden)'))
                self.assertTrue(cards.locator('.discovery-watchlist-action').evaluate_all('''buttons => buttons.every(b =>
                  b.disabled && b.dataset.watchlistKey === '' && b.getAttribute('aria-pressed') === 'false')'''))
                cards.locator('.discovery-watchlist-action').evaluate_all('(buttons)=>buttons.forEach(b=>b.click())')
                self.assertEqual(network.watch_calls, [])
                for index, item in enumerate(data['days'][2]['items']):
                    link = cards.nth(index).locator('.discovery-card-open')
                    self.assertEqual(link.get_attribute('href'), item['url'])
                    self.assertEqual(link.get_attribute('target'), '_blank')
                    self.assertEqual(link.get_attribute('rel'), 'noopener noreferrer')
                    self.assertIsNone(link.get_attribute('data-media-profile-link'))
                self.assert_poster_layout(page, geometry)
                page.evaluate('posterChanges.length = 0')
                page.locator('#calendar-refresh').click()
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                self.assert_poster_layout(page, geometry)
                self.assertEqual(page.evaluate('posterChanges'), [])
                self.assertCountEqual(network.poster_calls, paths)
                # 全页截图可能暂时调整视口，放在 DOM/尺寸断言之后，避免将截图行为当成刷新。
                self.screenshot(page, f'anime-platform-original-unmatched-{width}.png')

    def test_platform_fallback_after_both_metadata_posters_fail_retains_loaded_image(self):
        primary = '/discovery-poster/tmdb/fixture-210'
        secondary = '/discovery-poster/douban/fixture-211'
        for width, source in ((1440, 'tencent'), (390, 'iqiyi'), (320, 'youku')):
            with self.subTest(width=width, source=source):
                original = f'/discovery-calendar-poster/{source}/fixture-212'
                data = blank_payload()
                # 即使候选顺序被打乱，原图仍最后兜底；provider 标签不能改变实际图片语义。
                item = synthetic_entry(210, source, douban_id='1292052', mapping_status='matched', poster_provider='tmdb',
                    poster_url=primary, poster_urls=[original, primary, secondary, original, primary])
                data['days'][2]['items'] = [item]
                page, network = self.make_page(data, width=width, poster_failures=[primary, secondary],
                                               poster_holds=[primary, secondary, original])
                card = page.locator('.wc-card')
                image = card.locator('img')
                geometry = self.remember_poster_layout(page)
                detail = card.locator('.discovery-card-open').get_attribute('href')
                identity = card.locator('.discovery-watchlist-action').get_attribute('data-watchlist-key')
                for index, path in enumerate((primary, secondary, original)):
                    page.wait_for_function('(path)=>document.querySelector(".wc-card img").src.endsWith(path)', arg=path)
                    self.wait_for_fixture(page, lambda: path in network.poster_calls)
                    self.assertEqual(network.poster_calls, [primary, secondary, original][:index + 1])
                    self.assertEqual(card.locator('.wc-poster-note').inner_text(), '封面加载中')
                    self.assertNotIn('加载失败', card.inner_text())
                    self.assert_poster_layout(page, geometry)
                    network.release_poster(path)
                page.wait_for_function('document.querySelector(".wc-card img").naturalWidth === 640')
                self.assertEqual(image.get_attribute('src'), ORIGIN + original)
                self.assertEqual(image.get_attribute('title'), '平台原图，仅作封面，不代表已匹配资料')
                self.assertTrue(card.locator('.wc-poster-note').is_hidden())
                self.assertEqual(card.locator('.discovery-card-open').get_attribute('href'), detail)
                self.assertEqual(card.locator('.discovery-watchlist-action').get_attribute('data-watchlist-key'), identity)
                self.assertEqual(card.locator('.wc-tmdb-link span').inner_text(), 'TMDB 已映射')
                self.assert_poster_layout(page, geometry)
                page.evaluate('posterChanges.length = 0')
                # 即使失败的主图现在能返回 200，普通刷新、用户刷新、候选重排都不能让成功原图回跳。
                network.poster_failures.clear()
                network.data['days'][2]['items'][0]['poster_urls'] = [primary, secondary, original]
                for manual in (False, True, True):
                    network.hold = True
                    if manual:
                        page.locator('#calendar-refresh').click()
                    else:
                        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
                    page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "true"')
                    self.assertEqual(image.get_attribute('src'), ORIGIN + original)
                    self.assertEqual(image.evaluate('(i)=>i.style.opacity'), '1')
                    self.assertTrue(card.locator('.wc-poster-note').is_hidden())
                    self.assert_poster_layout(page, geometry)
                    self.wait_for_fixture(page, lambda: bool(network.pending))
                    network.release()
                    network.hold = False
                    page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                    self.assert_poster_layout(page, geometry)
                    self.assertEqual(network.poster_calls, [primary, secondary, original])
                    self.assertEqual(page.evaluate('posterChanges'), [])
                self.screenshot(page, f'anime-platform-original-fallback-{width}.png')
                # 实际收藏依然提交 TMDB 身份专用 token，不从当前平台图片取 token。
                button = card.locator('.discovery-watchlist-action')
                button.evaluate('(b)=>b.click()')
                page.wait_for_function('document.querySelector(".wc-card .discovery-watchlist-action").getAttribute("aria-busy") === "false"')
                body = json.loads(network.watch_calls[0]['body'])
                self.assertEqual((body['provider'], body['external_id'], body['poster_token']),
                                 ('tmdb', str(item['tmdb_id']), 'tmdb-fixture-token'))
                self.assertNotEqual(body['poster_token'], original.rsplit('/', 1)[-1])

    def test_platform_poster_strict_paths_reject_providers_urls_queries_and_traversal(self):
        invalid = [None, 12, {}, [], True, '', 'https://evil.invalid/poster.jpg', '//evil.invalid/x',
            ORIGIN + '/discovery-calendar-poster/tencent/fixture-220', 'data:image/svg+xml,<svg/>', 'javascript:alert(1)',
            '/discovery-calendar-poster/tmdb/fixture-220', '/discovery-calendar-poster/douban/fixture-220',
            '/discovery-calendar-poster/evil/fixture-220', '/discovery-calendar-poster/__proto__/fixture-220',
            '/discovery-calendar-poster/constructor/fixture-220', '/discovery-calendar-poster/Tencent/fixture-220',
            '/discovery-poster/tencent/fixture-220', '/discovery-poster/calendar-iqiyi/fixture-220']
        for prefix in ('/discovery-poster/tmdb/', '/discovery-poster/douban/',
                       '/discovery-calendar-poster/tencent/', '/discovery-calendar-poster/iqiyi/', '/discovery-calendar-poster/youku/'):
            invalid.extend(prefix + suffix for suffix in ('', '.', '..', '../fixture-220', '../../api/config',
                '%2e%2e', '%2E%2E%2Ffixture-220', 'fixture-220/..', 'fixture-220/extra', r'fixture-220\x',
                'fixture-220?redirect=evil', 'fixture-220#hash', 'fixture-220%3Fquery', 'fixture-220\n',
                'fixture-220\r', 'fixture-220 ', 'fixture-220%00', 'a' * 2049))
        data = blank_payload()
        for index, path in enumerate(invalid):
            data['days'][2]['items'].append(synthetic_entry(220 + index, tmdb_id='', douban_id='', url='',
                watchlist=None, mapping_status='unmatched', poster_url=path, poster_urls=[path],
                poster_provider='<img src=x onerror="window.pwned=1">'))
        page, network = self.make_page(data)
        self.assertEqual(page.locator('.wc-card').count(), len(invalid))
        self.assertEqual(page.locator('.wc-card img[src], .wc-card a[href]').count(), 0)
        self.assertEqual(page.locator('.wc-poster-note').all_text_contents(), ['暂未匹配海报'] * len(invalid))
        self.assertNotIn('加载失败', page.locator('#calendar-days').inner_text())
        self.assertIsNone(page.evaluate('window.pwned'))
        self.assertEqual(network.poster_calls, [])
        self.assertEqual(network.watch_calls, [])
        self.assertEqual(network.unexpected, [])
        page.locator('#calendar-refresh').click()
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(page.locator('.wc-card img[src]').count(), 0)
        self.assertEqual(network.poster_calls, [])

    def test_platform_poster_token_bounds_and_provider_labels_do_not_override_paths(self):
        data = blank_payload()
        paths = ['/discovery-calendar-poster/tencent/a', '/discovery-calendar-poster/iqiyi/' + 'A' * 2048,
                 '/discovery-calendar-poster/youku/fixture-230_sig.with-dots', '/discovery-poster/tmdb/t',
                 '/discovery-poster/douban/' + 'D' * 2048]
        for index, path in enumerate(paths):
            data['days'][2]['items'].append(synthetic_entry(230 + index, poster_url=path,
                poster_urls={'not': 'a list'} if index == 0 else [path, path],
                poster_provider=['__proto__', 'https://evil.invalid/', 'tmdb', 'calendar-youku', 'calendar-tencent'][index]))
        page, network = self.make_page(data)
        page.wait_for_function('''() => [...document.querySelectorAll('.wc-card img')].every(i => i.complete && i.naturalWidth > 0)''')
        self.assertCountEqual(network.poster_calls, paths)
        self.assertEqual(page.locator('.wc-card img').evaluate_all('(images)=>images.map(i=>i.title)'),
                         ['平台原图，仅作封面，不代表已匹配资料'] * 3 + ['', ''])
        self.assertEqual(page.locator('.wc-tmdb-link span').all_text_contents(), ['TMDB 已映射'] * 5)
        self.assertEqual(page.locator('.wc-card a[href^="https://"]').count(), 0)

    def test_platform_page_navigation_is_offline_allowlisted_and_not_a_profile(self):
        data = blank_payload()
        hosts = {'tencent': ['v.qq.com', 'm.v.qq.com'], 'iqiyi': ['www.iqiyi.com', 'm.iqiyi.com'],
                 'youku': ['www.youku.com', 'v.youku.com', 'm.youku.com', 'youku.com']}
        safe = [(source, f'https://{host}/fixture-240') for source, values in hosts.items() for host in values]
        invalid = ['javascript:alert(1)', '//v.qq.com/fixture-240', 'https://v.qq.com.evil.test/x',
                   'https://v.qq.com@evil.test/x', 'https://evil:v.qq.com@v.qq.com/x', 'http://v.qq.com/x',
                   'https://v.qq.com:444/x', 'https://www.iqiyi.com/x', 'https://v.qq.com\\@evil.test/x',
                   'https://v.qq.com/x\n', '/discovery?detail_provider=tmdb&detail_type=tv&detail_id=1']
        for index, (source, url) in enumerate(safe + [('tencent', url) for url in invalid]):
            data['days'][2]['items'].append(synthetic_entry(240 + index, source, tmdb_id='', douban_id='',
                mapping_status='unmatched', watchlist=None, url=url, poster_url='', poster_urls=[]))
        page, network = self.make_page(data)
        cards = page.locator('.wc-card')
        network.platform_pages = {url for _, url in safe}
        for index, (_, url) in enumerate(safe):
            link = cards.nth(index).locator('.discovery-card-open')
            self.assertEqual(link.get_attribute('href'), url)
            self.assertIn('原页（新窗口）', link.get_attribute('aria-label'))
            self.assertIsNone(link.get_attribute('data-media-profile-link'))
            with page.expect_popup() as popup_event:
                link.evaluate('(a)=>a.click()')
            popup = popup_event.value
            popup.wait_for_load_state('domcontentloaded')
            self.assertEqual(popup.url, url)
            self.assertEqual(popup.title(), '离线平台原页夹具')
            self.assertTrue(popup.evaluate('window.opener === null'))
            popup.close()
        for index in range(len(safe), len(safe) + len(invalid)):
            self.assertEqual(cards.nth(index).locator('a[href]').count(), 0)
        self.assertTrue(cards.locator('.discovery-watchlist-action').evaluate_all('(buttons)=>buttons.every(b=>b.disabled)'))
        self.assertEqual(network.platform_calls, [url for _, url in safe])
        self.assertEqual(network.watch_calls, [])
        self.assertFalse(any('/api/discovery/detail/' in call[1] for call in network.calls))
        self.assertEqual(page.url, ORIGIN + '/discovery/calendar')

    def test_platform_only_poster_preserves_tmdb_douban_and_empty_watch_tokens(self):
        original = '/discovery-calendar-poster/iqiyi/fixture-260'
        data = blank_payload()
        data['days'][2]['items'] = [
            synthetic_entry(260, 'iqiyi', poster_url=original, poster_urls=[original], poster_provider='calendar-iqiyi',
                watchlist={'provider': 'tmdb', 'external_id': '12560', 'media_type': 'tv', 'poster_token': '', 'in_watchlist': False}),
            synthetic_entry(261, 'iqiyi', tmdb_id='', douban_id='1292052', poster_url=original, poster_urls=[original],
                poster_provider='calendar-iqiyi'),
        ]
        page, network = self.make_page(data)
        page.wait_for_function('''() => [...document.querySelectorAll('.wc-card img')].every(i => i.naturalWidth === 640)''')
        for index, provider, identity, token in [(0, 'tmdb', '12560', ''), (1, 'douban', '1292052', 'douban-fixture-token')]:
            card = page.locator('.wc-card').nth(index)
            link = card.locator('.discovery-card-open')
            self.assertEqual(link.get_attribute('href'), f'/discovery?detail_provider={provider}&detail_type=tv&detail_id={identity}')
            self.assertIsNotNone(link.get_attribute('data-media-profile-link'))
            self.assertIsNone(link.get_attribute('target'))
            card.locator('.discovery-watchlist-action').evaluate('(b)=>b.click()')
            page.wait_for_function('''() => [...document.querySelectorAll('.wc-card .discovery-watchlist-action')].every(b =>
              b.getAttribute('aria-busy') === 'false')''')
            body = json.loads(network.watch_calls[index]['body'])
            self.assertEqual((body['provider'], body['external_id'], body['media_type'], body['poster_token']),
                             (provider, identity, 'tv', token))
            link.click()
            page.locator('#discovery-detail-dialog[open]').wait_for()
            page.wait_for_function('(provider)=>document.querySelector("#discovery-detail-body").textContent.includes("详情夹具 " + provider)', arg=provider)
            page.locator('[data-discovery-dialog-close]').click()
            page.wait_for_function('!document.querySelector("#discovery-detail-dialog").open && document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(network.platform_calls, [])
        self.assertFalse(any('fixture-260' in call['body'] for call in network.watch_calls))
        self.assertEqual(len(page.context.pages), 1)

    def test_platform_poster_failure_recovery_withdrawal_and_mapping_keep_elements(self):
        original = '/discovery-calendar-poster/youku/fixture-270'
        metadata = '/discovery-poster/douban/fixture-271'
        for width in (1440, 320):
            with self.subTest(width=width):
                data = blank_payload()
                data['days'][2]['items'] = [synthetic_entry(270, 'youku', tmdb_id='', douban_id='', watchlist=None,
                    mapping_status='unmatched', poster_url=original, poster_urls=[original])]
                page, network = self.make_page(data, width=width, poster_failures=[original])
                page.wait_for_function('document.querySelector(".wc-poster-note").textContent === "封面加载失败"')
                card = page.locator('.wc-card')
                geometry = self.remember_poster_layout(page)
                self.assertEqual(network.poster_calls, [original])
                page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                self.assertEqual(network.poster_calls, [original])
                network.poster_failures.clear()
                page.locator('#calendar-refresh').click()
                page.wait_for_function('document.querySelector(".wc-card img").naturalWidth === 640')
                self.assertEqual(network.poster_calls, [original, original])
                self.assertEqual(card.locator('img').get_attribute('title'), '平台原图，仅作封面，不代表已匹配资料')
                self.assertTrue(card.locator('.wc-poster-note').is_hidden())
                self.assert_poster_layout(page, geometry)
                # 地址撤销只是资料缺失；不能遗留真实失败文案或上一张原图的辅助说明。
                network.data['days'][2]['items'][0].update(poster_url='', poster_urls=[])
                page.locator('#calendar-refresh').click()
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                self.assertEqual(card.locator('img[src]').count(), 0)
                self.assertEqual(card.locator('img').get_attribute('title'), '')
                self.assertEqual(card.locator('img').get_attribute('alt'), '')
                self.assertEqual(card.locator('.wc-poster-note').inner_text(), '暂未匹配海报')
                self.assert_poster_layout(page, geometry)
                # 同一节目后续获得资料，仅更新既有链接与收藏身份，不替换封面/页脚节点。
                network.data['days'][2]['items'][0].update(douban_id='1292052', mapping_status='matched', poster_url=metadata,
                    poster_urls=[metadata], detail_url='/discovery?detail_provider=douban&detail_type=tv&detail_id=1292052',
                    watchlist={'provider': 'douban', 'external_id': '1292052', 'media_type': 'tv', 'poster_token': 'douban-fixture-token', 'in_watchlist': False})
                page.locator('#calendar-refresh').click()
                page.wait_for_function('document.querySelector(".wc-card img").naturalWidth === 200')
                self.assertEqual(card.locator('img').get_attribute('title'), '')
                self.assertIsNone(card.locator('.discovery-card-open').get_attribute('target'))
                self.assertIsNone(card.locator('.discovery-card-open').get_attribute('rel'))
                self.assertIsNotNone(card.locator('.discovery-card-open').get_attribute('data-media-profile-link'))
                self.assertEqual(card.locator('.discovery-watchlist-action').get_attribute('data-watchlist-key'), 'douban:tv:1292052')
                self.assertFalse(card.locator('.discovery-watchlist-action').is_disabled())
                self.assert_poster_layout(page, geometry)


    def test_watchlist_pending_keeps_keyboard_focus_and_real_single_flight(self):
        for status, success in ((200, True), (200, False), (503, False)):
            with self.subTest(status=status, success=success):
                page, network = self.make_page(width=390)
                network.watch_hold = True
                network.watch_status = status
                network.watch_success = success
                button = page.locator('.wc-grid:visible .discovery-watchlist-action').first
                button.focus()
                geometry = button.bounding_box()
                page.keyboard.press('Space')
                page.wait_for_timeout(40)
                self.assertEqual(button.get_attribute('aria-busy'), 'true')
                self.assertEqual(button.get_attribute('aria-disabled'), 'true')
                self.assertFalse(button.evaluate('(b) => b.disabled'))
                self.assertTrue(button.evaluate('(b) => document.activeElement === b'))
                self.assertEqual(button.bounding_box(), geometry)
                # aria-disabled 仅表达状态；重复键盘/程序触发都必须由 pending 守卫拒绝。
                page.keyboard.press('Space')
                page.keyboard.press('Enter')
                button.evaluate('(b) => { b.setAttribute("aria-disabled", "false"); b.click(); }')
                page.wait_for_timeout(40)
                self.assertEqual(len(network.watch_calls), 1)
                network.release_watch()
                page.wait_for_function('document.querySelector(".wc-grid:not([hidden]) .discovery-watchlist-action").getAttribute("aria-busy") === "false"')
                self.assertTrue(button.evaluate('(b) => document.activeElement === b'))
                self.assertEqual(button.get_attribute('aria-disabled'), 'false')
                self.assertEqual(button.get_attribute('aria-pressed'), str(status == 200 and success).lower())
                self.assertEqual(button.bounding_box(), geometry)
                network.watch_hold = False
                page.keyboard.press('Space')
                page.wait_for_timeout(40)
                self.assertEqual(len(network.watch_calls), 2)
                self.assertTrue(button.evaluate('(b) => document.activeElement === b'))

    def test_refresh_and_retry_pending_keep_focus_with_business_single_flight(self):
        for retry, status in ((False, 200), (False, 503), (True, 503)):
            with self.subTest(retry=retry, status=status):
                page, network = self.make_page(width=390, hold=retry)
                if retry:
                    network.status = 503
                    page.wait_for_timeout(40)
                    network.release()
                    page.wait_for_function('document.querySelector("#calendar-status").dataset.tone === "error"')
                network.hold = True
                network.status = status
                button = page.locator('.wc-grid:visible .jump-btn' if retry else '#calendar-refresh')
                button.focus()
                geometry = button.bounding_box()
                before = len(network.api_calls())
                page.keyboard.press('Enter')
                page.wait_for_timeout(40)
                self.assertEqual(button.get_attribute('aria-busy'), 'true')
                self.assertEqual(button.get_attribute('aria-disabled'), 'true')
                self.assertFalse(button.evaluate('(b) => b.disabled'))
                self.assertTrue(button.evaluate('(b) => document.activeElement === b'))
                page.keyboard.press('Enter')
                page.keyboard.press('Space')
                # 同时跨刷新/重试入口调用，不能通过改 ARIA 属性绕过 inFlight。
                page.locator('#calendar-refresh, .wc-grid:visible .jump-btn').evaluate_all('''buttons => buttons.forEach(b => {
                    b.setAttribute('aria-disabled', 'false'); b.setAttribute('aria-busy', 'false'); b.click();
                })''')
                page.wait_for_timeout(40)
                self.assertEqual(len(network.api_calls()), before + 1)
                self.assertEqual(button.bounding_box(), geometry)
                network.release()
                page.wait_for_function('document.querySelector("#calendar-stage").getAttribute("aria-busy") === "false"')
                self.assertTrue(button.evaluate('(b) => document.activeElement === b'))
                self.assertEqual(button.get_attribute('aria-busy'), 'false')
                self.assertEqual(button.bounding_box(), geometry)
                network.hold = False
                page.keyboard.press('Enter')
                page.wait_for_timeout(40)
                self.assertEqual(len(network.api_calls()), before + 2)

    def test_background_refresh_uses_snapshot_busy_not_aria_disabled_attribute(self):
        data = synthetic_payload()
        data['refreshing'] = True
        data['retry_after'] = 5
        page, network = self.make_page(data, width=390)
        page.clock.install()
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('document.querySelectorAll(".wc-card").length === 42')
        button = page.locator('#calendar-refresh')
        self.assertFalse(button.evaluate('(b) => b.disabled'))
        self.assertEqual(button.get_attribute('aria-busy'), 'true')
        before = len(network.api_calls())
        button.focus()
        button.evaluate('(b) => b.setAttribute("aria-disabled", "false")')
        page.keyboard.press('Space')
        page.keyboard.press('Enter')
        page.wait_for_timeout(40)
        self.assertEqual(len(network.api_calls()), before)
        self.assertTrue(button.evaluate('(b) => document.activeElement === b'))
        network.data['refreshing'] = False
        page.clock.run_for(5001)
        page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
        self.assertEqual(len(network.api_calls()), before + 1)
        self.assertEqual(button.get_attribute('aria-disabled'), 'false')
        self.assertTrue(button.evaluate('(b) => document.activeElement === b'))

    def test_async_completion_never_reclaims_user_moved_focus(self):
        for watch, status in ((False, 200), (False, 503), (True, 200), (True, 503)):
            with self.subTest(watch=watch, status=status):
                page, network = self.make_page(width=390)
                button = page.locator('.wc-grid:visible .discovery-watchlist-action').first if watch else page.locator('#calendar-refresh')
                if watch:
                    network.watch_hold = True
                    network.watch_status = status
                else:
                    network.hold = True
                    network.status = status
                button.focus()
                page.keyboard.press('Enter')
                page.wait_for_timeout(40)
                platform = page.locator('#calendar-platform')
                platform.focus()
                platform.select_option('iqiyi')
                if watch:
                    network.release_watch()
                else:
                    # 回包同时重排，不能恢复请求开始时的按钮焦点。
                    for day in network.data['days']:
                        day['items'].reverse()
                    network.release()
                page.wait_for_function('''() => [...document.querySelectorAll('#calendar-refresh, .discovery-watchlist-action')]
                    .every(b => b.getAttribute('aria-busy') === 'false')''')
                self.assertTrue(platform.evaluate('(e) => document.activeElement === e'))
                self.assertEqual(platform.input_value(), 'iqiyi')
                self.assertEqual(page.locator('.wc-card:visible').count(), 2)

    def test_reconciliation_reorder_preserves_same_control_focus_and_scroll(self):
        for width in (320, 1440):
            for watch in (False, True):
                with self.subTest(width=width, watch=watch):
                    page, network = self.make_page(width=width)
                    network.hold = True
                    page.locator('#calendar-refresh').evaluate('(b) => b.click()')
                    page.wait_for_timeout(40)
                    selector = '.discovery-watchlist-action' if watch else '.discovery-card-open'
                    button = page.locator(f'.wc-grid:visible {selector}').last
                    # 真正进入键盘模态；仅脚本 focus 不保证浏览器绘制 :focus-visible。
                    page.keyboard.press('Tab')
                    button.focus()
                    self.assertTrue(button.evaluate('(e) => e.matches(":focus-visible")'))
                    page.evaluate('''() => {
                        window.focusBeforeReconcile = document.activeElement;
                        window.restoreFocusOptions = [];
                        const original = focusBeforeReconcile.focus;
                        focusBeforeReconcile.focus = function(options) {
                            restoreFocusOptions.push(options); original.call(this, options);
                        };
                    }''')
                    scroll = page.evaluate('scrollY')
                    for day in network.data['days']:
                        day['items'].reverse()
                    network.release()
                    page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                    self.assertTrue(page.evaluate('document.activeElement === focusBeforeReconcile && focusBeforeReconcile.isConnected'))
                    self.assertEqual(page.evaluate('restoreFocusOptions'), [{'preventScroll': True}])
                    self.assertEqual(page.locator('.wc-card:visible').evaluate_all('(cards) => cards.map(c => c.dataset.stableId)'),
                                     [item['stable_id'] for item in network.data['days'][2]['items']])
                    self.assertEqual(page.evaluate('scrollY'), scroll)
                    # 重排后 .last Locator 已指向另一张卡；所有焦点断言必须绑定原节点。
                    self.assertTrue(page.evaluate('focusBeforeReconcile.matches(":focus-visible")'))
                    page.keyboard.press('Enter')
                    if watch:
                        page.wait_for_timeout(40)
                        self.assertEqual(len(network.watch_calls), 1)
                    else:
                        page.wait_for_function('document.querySelector("#discovery-detail-dialog").open')

    def test_reconciliation_never_refocuses_removed_hidden_or_disabled_link(self):
        for change in ('remove', 'filter', 'day', 'disable'):
            with self.subTest(change=change):
                page, network = self.make_page(width=390)
                network.hold = True
                page.locator('#calendar-refresh').evaluate('(b) => b.click()')
                page.wait_for_timeout(40)
                page.locator('.wc-grid:visible .discovery-card-open').last.focus()
                page.evaluate('''() => {
                    window.oldFocus = document.activeElement; window.restoreCalls = 0;
                    const original = oldFocus.focus;
                    oldFocus.focus = function(options) { restoreCalls++; original.call(this, options); };
                }''')
                if change == 'filter':
                    page.locator('#calendar-platform').select_option('iqiyi')
                elif change == 'day':
                    page.locator('.wc-weekday[data-date="2026-09-11"]').evaluate('(b) => b.click()')
                else:
                    for day in network.data['days']:
                        if change == 'remove':
                            day['items'] = [item for item in day['items'] if item['stable_id'] != 'tencent:fixture-6']
                        else:
                            day['items'][-1].update(tmdb_id='', douban_id='', detail_url='', url='', watchlist=None)
                for day in network.data['days']:
                    day['items'].reverse()
                network.release()
                page.wait_for_function('document.querySelector("#calendar-refresh").getAttribute("aria-busy") === "false"')
                self.assertEqual(page.evaluate('restoreCalls'), 0)
                self.assertFalse(page.evaluate('document.activeElement === oldFocus'))
                self.assertEqual(page.locator('.wc-grid:visible').get_attribute('data-date'), '2026-09-11' if change == 'day' else TODAY)
                self.assertEqual(page.locator('#calendar-platform').input_value(), 'iqiyi' if change == 'filter' else 'all')



if __name__=='__main__':
    unittest.main()
