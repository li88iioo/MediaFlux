"""手动刮削搜索词：打开弹窗时的自动清洗样本，以及双语整串零结果时的单语言补搜。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from app.modules.recognition.cleaner import _comparison_key
from app.modules.scraper import TMDBScraper
from tests.support import IsolatedDatabaseTestCase


ROOT = Path(__file__).resolve().parents[1]
SLIME = '关于我转生变成史莱姆这档事 Tensei Shitara Slime Datta Ken'


class ManualScrapeQueryCleaningTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.scraper = TMDBScraper()
        self.addCleanup(self.scraper.close)

    def test_instance_releases_clean_to_their_titles_without_tmdb(self):
        cases = [json.loads(line) for line in (
            ROOT / 'tests/fixtures/release_recognition_cases.jsonl'
        ).read_text('utf-8').splitlines()]
        with patch.object(TMDBScraper, 'search', side_effect=AssertionError('清洗不能查询 TMDB')):
            for case in cases:
                if 'source-instance-20261010' not in case.get('tags', []):
                    continue
                with self.subTest(filename=case['filename']):
                    self.assertEqual(
                        _comparison_key(self.scraper.clean_title(case['filename'])),
                        _comparison_key(case['expected']['title']),
                    )

    def test_release_noise_is_removed_and_official_title_parts_are_kept(self):
        # 样本来自原“一键精简”回归：括号副标题、真实标题中的语言词和数字不得被误删。
        cases = (
            ('1917.2019.1080p.BluRay.mkv', '1917'),
            ('Toy Story 3', 'Toy Story 3'),
            ('Proper Hybrid Unrated IMAX', 'Proper Hybrid Unrated IMAX'),
            ('Movie.SecretChapter.2024.1080p.BluRay.mkv', 'Movie SecretChapter'),
            ('[Japanese] Story.2003.1080p.WEB-DL.H264.AAC.mkv', 'Japanese Story'),
            ('[Arifureta Shokugyou de Sekai Saikyou S2][01-12][BIG5][1080P][MP4]',
             'Arifureta Shokugyou de Sekai Saikyou'),
            ('[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生（仅限港澳台)',
             '被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生'),
            ('[ANi] 被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生（仅限港澳台） - 01 '
             '[1080P][Bilibili][WEB-DL][AAC AVC][CHT CHS][MP4].mp4',
             '被解雇的暗黑士兵（30多岁）开始了慢生活的第二人生'),
            ('[Sakurato] Kage no Jitsuryokusha ni Naritakute! [01-20 FIN][AVC-8bit 1080P AAC][CHS]',
             'Kage no Jitsuryokusha ni Naritakute!'),
            ('[LoliHouse] The Ghost in the Shell - 03 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv',
             'The Ghost in the Shell'),
            ('[orion origin] Undead Unluck [01-24] [BDRip] [1080p] [H265 10bit_FLAC] [CHS＆JPN]',
             'Undead Unluck'),
            ('[豌豆字幕组&LoliHouse] 关于我转生变成史莱姆这档事 第四季 _ Tensei Shitara Slime Datta Ken '
             '4th Season [01-24(73-96) 合集][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]', SLIME),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.scraper.clean_title(raw), expected)

    def test_audio_channels_are_cleaned_whole_and_never_become_episode_numbers(self):
        for audio in ('DTS-HD.MA.5.1', 'DTS.HD.MA.6.1', 'DDP5.1', 'TrueHD.7.1'):
            for position in ('', '.S02E01'):
                with self.subTest(audio=audio, position=position):
                    raw = f'Movie{position}.2024.1080p.BluRay.{audio}.mkv'
                    self.assertEqual(self.scraper.clean_title(raw), 'Movie')
                    parsed = self.scraper.parse_media(raw)
                    self.assertEqual(parsed.media_type, 'tv' if position else 'movie')
                    self.assertEqual(parsed.source_episode, 1 if position else None)


class ManualSearchSingleLanguageFallbackTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.scraper = TMDBScraper()
        self.addCleanup(self.scraper.close)
        self.calls: list[tuple[str, str]] = []

    def _search(self, hits):
        def search(title, year, media_type):
            self.calls.append((title, year))
            return [dict(item) for item in hits.get(title, [])]
        return patch.object(self.scraper, 'search', side_effect=search)

    def test_bilingual_query_without_results_retries_each_language_in_order(self):
        hit = {'id': 82684, 'name': '关于我转生变成史莱姆这档事', 'first_air_date': '2018-10-02'}
        with self._search({'关于我转生变成史莱姆这档事': [hit]}):
            candidates = self.scraper.search_candidates(SLIME, '2018', 'tv')
        self.assertEqual(self.calls, [(SLIME, '2018'), (SLIME, ''), ('关于我转生变成史莱姆这档事', '2018')])
        self.assertEqual([item.tmdb_id for item in candidates], ['82684'])

    def test_full_query_hit_or_single_language_title_never_adds_searches(self):
        hit = {'id': 862, 'title': 'Toy Story', 'release_date': '1995-10-30'}
        for query, hits, expected in (
            (SLIME, {SLIME: [hit]}, [(SLIME, '')]),
            ('Toy Story 3', {}, [('Toy Story 3', '')]),
        ):
            with self.subTest(query=query):
                self.calls.clear()
                with self._search(hits):
                    self.scraper.search_candidates(query, '', 'movie')
                self.assertEqual(self.calls, expected)

    def test_noise_only_query_does_not_search(self):
        with self._search({}):
            self.assertEqual(self.scraper.search_candidates('[1080p][AAC]', '', 'movie'), [])
        self.assertEqual(self.calls, [])
