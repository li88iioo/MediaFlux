"""人工确认成功后只收尾已确认原父链，不扫描或删除无关目录。"""
from __future__ import annotations

from typing import Any, cast
from app.clients.guangya import GuangYaClient
from app.modules.scraper import TMDBScraper

from unittest.mock import patch

from app.repositories import download_requests as download_requests_repo
from app.modules import organize_confirmations as confirmations
from app.modules.organize import (
    OrganizeContext, OrganizePlan, OrganizePlanningResult, Organizer, OrganizeRules,
)
from app.modules.organize_scan import OrganizeScanResult
from tests.support import IsolatedDatabaseTestCase
from tests.test_download_staging_cleanup_lifecycle import _CloudTree


class ConfirmationSourceCleanupTests(IsolatedDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.cloud = _CloudTree()
        self.cloud.add_dir('leaf', 'stage')
        self.rules = OrganizeRules(target_dir_id='target', clean_empty=True)
        self.organizer = Organizer(client=cast(GuangYaClient, self.cloud), scraper=cast(TMDBScraper, object()))
        self.addCleanup(self.organizer.close)
        self.payload = {'source_dir_id': 'source', 'source_parent_id': 'leaf'}
        self.stats: dict[str, Any] = {'moved': 1}
        self.protected = self.enterContext(patch.object(
            download_requests_repo, 'list_protected_guangya_staging_ids', return_value=[],
        ))
        self.enterContext(patch(
            'app.modules.organize_tasks.config.get',
            side_effect=lambda key, default='': (
                '[{"id":"source","name":"Source"}]' if key == 'GY_ORGANIZE_SOURCE_DIRS' else default
            ),
        ))

    def clean(self):
        confirmations._cleanup_confirmed_source(self.payload, self.organizer, self.rules, self.stats)

    def test_empty_parent_chain_cleaned_inside_out_but_permanent_root_preserved(self):
        self.clean()
        self.assertEqual(self.cloud.deleted, ['leaf', 'stage'])
        self.assertEqual(self.stats['empty_dirs_cleaned'], 2)
        self.assertEqual(self.stats['source_dir_cleanup_protected'], 1)

    def test_never_walks_above_original_temporary_source(self):
        self.payload['source_dir_id'] = 'stage'
        self.clean()
        self.assertEqual(self.cloud.deleted, ['leaf', 'stage'])
        self.assertNotIn('source', self.cloud.reads)

    def test_parent_equal_source_is_cleaned(self):
        self.payload['source_dir_id'] = 'leaf'
        self.clean()
        self.assertEqual(self.cloud.deleted, ['leaf'])

    def test_nonempty_parent_keeps_all_ancestors_and_unselected_files(self):
        self.cloud.add_file('unselected.srt', 'leaf')
        self.clean()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.stats['source_dir_cleanup_not_empty'], 1)
        self.assertIn('伴随', ' '.join(self.stats['empty_dir_cleanup_reasons']))

    def test_unrelated_empty_sibling_is_not_scanned_or_cleaned(self):
        self.cloud.add_dir('unrelated', 'stage')
        self.clean()
        self.assertEqual(self.cloud.deleted, ['leaf'])
        self.assertNotIn('unrelated', self.cloud.reads)

    def test_disabled_cleanup_makes_no_cloud_calls(self):
        self.rules.clean_empty = False
        self.clean()
        self.assertEqual(self.cloud.reads, [])
        self.assertEqual(self.cloud.deleted, [])
        self.protected.assert_not_called()

    def test_recycled_confirmation_cleans_exact_chain_with_cleanup_disabled(self):
        self.rules.clean_empty = False
        self.stats = {'moved': 0, 'skipped': 1, 'duplicate_recycled': 1,
                      'duplicate_cleanup_parent_ids': ['leaf']}
        self.cloud.add_dir('unrelated', 'source')
        self.clean()
        self.assertEqual(self.cloud.deleted, ['leaf', 'stage'])
        self.assertIn('source', self.cloud.nodes)
        self.assertNotIn('unrelated', self.cloud.reads)
        self.assertEqual(self.stats['empty_dirs_cleaned'], 2)
        event = confirmations._confirmation_result_event(self.payload, {}, self.stats)
        result_label = str(dict(event.fields)['执行结果'])
        self.assertIn('同版本已回收 1', result_label)
        self.assertIn('跳过 0', result_label)
        self.assertIn('整理完成', str(event.title))

    def test_recycled_confirmation_does_not_delete_temporary_source_root(self):
        self.rules.clean_empty = False
        self.payload['source_dir_id'] = 'leaf'
        self.stats = {'duplicate_recycled': 1, 'duplicate_cleanup_parent_ids': ['leaf']}
        self.clean()
        self.assertEqual(self.cloud.deleted, [])

    def test_recycled_confirmation_needs_its_exact_parent_and_no_failure(self):
        self.rules.clean_empty = False
        for extra in ({'duplicate_cleanup_parent_ids': ['other']}, {'duplicate_cleanup_failed': 1}):
            with self.subTest(extra=extra):
                self.stats = {'duplicate_recycled': 1, 'duplicate_cleanup_parent_ids': ['leaf'], **extra}
                self.clean()
                self.assertEqual(self.cloud.deleted, [])
                self.assertEqual(self.cloud.reads, [])

    def execute_recycled_stage(self, **extra):
        plan = OrganizePlan('video', 'video.mkv', 'leaf', original_parent_id='leaf', action='recycled')
        stats = Organizer._initial_stats()
        stats.update({'skipped': 1, 'duplicate_recycled': 1,
                      'duplicate_cleanup_parent_ids': ['leaf'], **extra})
        scan = OrganizeScanResult(
            scanned_videos=[], scanned_dirs=[(id_, 1, 'v1', 100) for id_ in self.cloud.nodes],
            companion_files={}, video_files_by_path={}, protected_sources=set(), source_root_name='Source',
        )
        with patch('app.modules.organize.execute_organize_plans'):
            self.organizer._run_execution_stage(
                scan, OrganizePlanningResult(plans=[plan], subtitle_plans_by_video={}),
                OrganizeContext(source_dir_id='source', dry_run=False, post_actions=False), self.rules, stats,
            )
        return stats

    def test_organizer_recycles_only_exact_ancestors_with_cleanup_disabled(self):
        self.rules.clean_empty = False
        self.cloud.add_dir('unrelated', 'source')
        stats = self.execute_recycled_stage()
        self.assertEqual(self.cloud.deleted, ['leaf', 'stage'])
        self.assertEqual(stats['empty_dirs_cleaned'], 2)
        self.assertNotIn('unrelated', self.cloud.reads)
        self.assertNotIn('source', self.cloud.reads)

    def test_organizer_does_not_treat_recycled_plan_as_remaining_media(self):
        stats = self.execute_recycled_stage()
        self.assertEqual(self.cloud.deleted, ['leaf', 'stage'])
        self.assertEqual(stats['empty_dirs_cleaned'], 2)
        self.assertNotIn('source', self.cloud.deleted)

    def test_organizer_recycled_cleanup_preserves_nonempty_and_out_of_scope_tree(self):
        self.rules.clean_empty = False
        self.cloud.add_file('unselected.srt', 'leaf')
        self.execute_recycled_stage()
        self.assertEqual(self.cloud.deleted, [])
        self.cloud.nodes['stage'].parent_id = '0'
        stats = self.execute_recycled_stage()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(stats['empty_dir_cleanup_failed'], 1)

    def test_organizer_recycle_failure_blocks_cleanup_and_read_error_is_redacted(self):
        self.rules.clean_empty = False
        stats = self.execute_recycled_stage(duplicate_cleanup_failed=1)
        self.assertEqual(self.cloud.reads, [])
        self.assertEqual(stats['empty_dir_cleanup_skipped'], 1)
        with patch.object(self.cloud, 'file_info', side_effect=RuntimeError('api_key=private-token')):
            stats = self.execute_recycled_stage()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(stats['duplicate_recycled'], 1)
        self.assertEqual(stats['empty_dir_cleanup_failed'], 1)
        self.assertNotIn('private-token', str(stats))

    def test_no_move_or_incomplete_execution_does_not_cleanup(self):
        for extra in ({'moved': 0}, {'failed': 1}, {'need_confirm': 1}, {'stopped': 1},
                      {'scan_errors': ['failed']}, {'replacement_cleanup_failed': 1}, {'audit_failures': 1}):
            with self.subTest(extra=extra):
                self.stats = {'moved': 1, **extra}
                self.clean()
                self.assertEqual(self.cloud.deleted, [])
        self.protected.assert_not_called()

    def test_active_download_root_and_descendants_are_not_cleaned(self):
        self.protected.return_value = ['stage']
        self.clean()
        self.assertEqual(self.cloud.deleted, [])
        self.assertIn('下载隔离', ' '.join(self.stats['empty_dir_cleanup_reasons']))

    def test_out_of_scope_or_broken_parent_chain_is_retained_before_any_deletion(self):
        for parent in ('0', 'missing', 'leaf'):
            with self.subTest(parent=parent):
                self.cloud.nodes['stage'].parent_id = parent
                self.clean()
                self.assertEqual(self.cloud.deleted, [])
                self.assertGreater(self.stats['source_dir_cleanup_failed'], 0)

    def test_delete_failure_does_not_fail_media_success_or_continue_upwards(self):
        self.cloud.on_delete = lambda _: (_ for _ in ()).throw(RuntimeError('provider refused'))
        self.clean()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.stats['moved'], 1)
        self.assertEqual(self.stats['source_dir_cleanup_failed'], 1)
        self.assertEqual(self.cloud.reads, ['leaf'])

    def test_recheck_rejects_directory_that_became_nonempty(self):
        self.cloud.on_delete = lambda directory: self.cloud.add_file('new.mp4', directory)
        self.clean()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.stats['source_dir_cleanup_failed'], 1)

    def test_archive_root_is_never_deleted(self):
        self.rules.target_dir_id = "leaf"
        self.clean()
        self.assertEqual(self.cloud.deleted, [])
        self.assertEqual(self.stats["source_dir_cleanup_protected"], 1)

    def test_read_failure_retains_directory_and_reports_partial_completion(self):
        with patch.object(self.cloud, "list_dir", side_effect=RuntimeError("read rejected")):
            self.clean()
        self.assertEqual(self.cloud.deleted, [])
        event = confirmations._confirmation_result_event(self.payload, {}, self.stats)
        self.assertIn("部分完成", str(event.title))
        self.assertIn("read rejected", str(event.fields))

    def test_cleanup_result_is_visible_in_confirmation_message(self):
        self.clean()
        event = confirmations._confirmation_result_event(self.payload, {}, self.stats)
        self.assertIn(('空目录清理', '已清理 2 个'), event.fields)
        self.assertIn('安全策略保留', str(event.fields))
