"""规则展示窗口不能限制同类列表、活动身份查找或并发确认去重。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from app import database as db
from app.modules.media_automation_rules import digests
from app.agent.models import ToolContext
from app.repositories import activity as follows
from app.repositories import media_automation_rules as rules
from app.repositories.agent_jobs import agent_job_owner_digest
from tests.support import IsolatedDatabaseTestCase


class AutomationRuleLookupAuditTests(IsolatedDatabaseTestCase):

    def setUp(self):
        with db.get_conn() as conn:
            conn.execute('DELETE FROM media_automation_rules')
            conn.execute('DELETE FROM download_request_keys')
            conn.execute('DELETE FROM download_log')
            conn.execute('DELETE FROM download_requests')
        self.context = ToolContext(owner='lookup-audit-owner', session_id='lookup-session')
        self.owner = agent_job_owner_digest(self.context.owner)
        identifier, _ = db.create_download_request('lookup-fixture', 'magnet', title='Audit')
        self.target = {'kind': 'download', 'id': identifier}
        self.arguments = {'kind': 'download', 'identifier': identifier, 'hours': 24}
        route = patch.object(follows, 'notification_route_settings', return_value={'notification_owner': self.context.owner, 'notification_chat_id': '123'})
        route.start()
        self.addCleanup(route.stop)

    def _save(self, kind, *, settings=None, historical=False, owner=None):
        row = rules.save_rule(owner or self.owner, kind=kind, settings=settings or {}, enabled=True, next_run_at='2026-01-02T08:00:00+00:00')
        assert row is not None
        if historical:
            with db.get_conn() as conn:
                conn.execute("UPDATE media_automation_rules SET created_at='2000-01-01 00:00:00' WHERE id=?", (row['id'],))
        return row

    def test_digest_kind_filter_precedes_other_kind_display_limit(self):
        for _ in range(105):
            self._save('activity_follow', historical=True)
        digest = self._save('daily_summary', settings={'hour': 8, 'minute': 0})
        items = digests(owner=self.context.owner)["data"]['items']
        self.assertEqual([row['rule_id'] for row in items], [digest['id']])
        self.assertEqual(len(rules.list_rules(self.owner)), 106)

    def test_follow_kind_filter_and_corruption_notice_survive_other_kind_history(self):
        for _ in range(105):
            self._save('daily_summary', historical=True)
        follow = self._save('activity_follow', settings={'target': self.target})
        with db.get_conn() as conn:
            conn.execute("UPDATE media_automation_rules SET settings_json='null' WHERE id=?", (follow['id'],))
        items = follows.follows(owner=self.context.owner)['data']['items']
        self.assertEqual([row['rule_id'] for row in items], [follow['id']])
        self.assertTrue(items[0].get('settings_error'))

    def test_existing_target_beyond_same_kind_window_is_updated_and_replay_rejected(self):
        for index in range(105):
            self._save('activity_follow', settings={'target': {'kind': 'download', 'id': index + 100000}}, historical=True)
        self._save('activity_follow', settings={'target': self.target}, historical=True, owner='another-owner')
        existing = self._save('activity_follow', settings={'target': self.target})
        token = follows.prepare_follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route)['snapshot']
        result = follows.follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route, expected_snapshot=token)
        self.assertEqual(result['data']['rule_id'], existing['id'])
        get_rule_result = rules.get_rule(self.owner, existing['id'])
        assert get_rule_result is not None
        self.assertEqual(get_rule_result['revision'], 2)
        with self.assertRaises(ValueError):
            follows.follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route, expected_snapshot=token)
        with db.get_conn() as conn:
            total = conn.execute('SELECT COUNT(*) FROM media_automation_rules WHERE owner_digest=?', (self.owner,)).fetchone()[0]
        self.assertEqual(total, 106)
        items = follows.follows(owner=self.context.owner)['data']['items']
        self.assertEqual(len(items), total)
        self.assertIn(existing['id'], {item['rule_id'] for item in items})

    def test_two_confirmations_of_absent_target_commit_only_one_rule(self):
        token = follows.prepare_follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route)['snapshot']
        barrier = Barrier(2)
        original = rules.save_rule

        def after_prepare(*args, **kwargs):
            barrier.wait(timeout=10)
            return original(*args, **kwargs)

        def confirm():
            try:
                return follows.follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route, expected_snapshot=token)
            except ValueError as exc:
                return exc
        with patch.object(rules, 'save_rule', side_effect=after_prepare):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: confirm(), range(2)))
        self.assertEqual(sum(not isinstance(row, ValueError) for row in results), 1)
        self.assertEqual(len(rules.list_rules(self.owner)), 1)
