"""跟踪走同一持久规则/通知 outbox，确认前不写，取消/重启后不误发。"""
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from typing import Any
from app import config
from app import database as db
from app.repositories.activity import prepare_follow, follow, prepare_unfollow, unfollow, follows
from app.agent.models import ToolContext
from app.agent.owner_routes import web_kernel_owner
from app.modules.activity_follow_notifications import deliver_activity_follow
from app.modules.media_automation_rules import drain_automation_rules, register_rule_handler
from app.repositories import media_automation_rules as rules
from app.repositories.agent_jobs import agent_job_owner_digest
from tests.support import IsolatedDatabaseTestCase

class ActivityFollowTests(IsolatedDatabaseTestCase):

    def setUp(self):
        enabled = patch('app.modules.media_automation_rules.is_agent_enabled', return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)
        with db.get_conn() as conn:
            conn.execute('DELETE FROM media_automation_rules')
            conn.execute('DELETE FROM download_request_keys')
            conn.execute('DELETE FROM download_log')
            conn.execute('DELETE FROM download_requests')
        self.context = ToolContext(owner=web_kernel_owner('follow-test'), session_id='one')
        identifier, _ = db.create_download_request('follow-request', 'magnet', title='测试剧集')
        db.update_download_request(identifier, status='downloading', gy_status='downloading')
        self.identifier = identifier
        self.arguments: dict[str, Any] = {'kind': 'download', 'identifier': identifier, 'hours': 24}
        get = config.get
        patched = patch('app.modules.media_automation_rules.config.get', side_effect=lambda key, *args, **kw: '123' if key == 'TG_CHAT_ID' else get(key, *args, **kw))
        patched.start()
        self.addCleanup(patched.stop)
        register_rule_handler('activity_follow', deliver_activity_follow)

    def save(self):
        prepared = prepare_follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route)
        preview, token = (prepared['preview'], prepared['snapshot'])
        self.assertEqual(preview['status'], 'confirmation_required')
        return follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route, expected_snapshot=token)

    def test_topic_follow_notification_keeps_route_through_domain_context_and_outbox(self):
        from app.modules.telegram_notification_center import drain_telegram_notifications
        from app.notifier import TelegramSendResult
        owner = 'tg:v1:123\x1f456'
        context = ToolContext(owner=owner, session_id='topic-77', request_id='follow-topic', delivery_route={'chat_id': '123', 'message_thread_id': 77})
        values = {'AGENT_ENABLED': '1', 'TG_AGENT_ENABLED': '1', 'TG_AGENT_ALLOWED_USER_IDS': '456', 'TG_NOTIFICATION_ENABLED': '1', 'TG_NOTIFICATION_LEVEL': 'standard'}
        current_get = config.get
        with patch.object(config, 'get', side_effect=lambda key, default='': values.get(key, current_get(key, default))), patch('app.modules.telegram_notification_center.send_event_result', side_effect=[TelegramSendResult(ok=False, status_code=429, retry_after_seconds=1), TelegramSendResult(ok=True, message_id=1234)]) as send:
            token = prepare_follow(**self.arguments, owner=context.owner, delivery_route=context.delivery_route)['snapshot']
            saved = follow(**self.arguments, owner=context.owner, delivery_route=context.delivery_route, expected_snapshot=token)
            db.update_download_request(self.identifier, status='completed', gy_status='completed')
            self.assertEqual(drain_automation_rules(now=datetime.now().astimezone() + timedelta(seconds=1)), 1)
            self.assertFalse(drain_telegram_notifications())
            with db.get_conn() as conn:
                conn.execute("UPDATE telegram_notification_outbox SET next_attempt_at='2000-01-01 00:00:00'")
            self.assertTrue(drain_telegram_notifications())
            self.assertEqual(send.call_count, 2)
            for call in send.call_args_list:
                self.assertEqual(call.kwargs.get('message_thread_id'), 77)
                self.assertEqual(call.kwargs['chat_id'], '123')
            row = rules.get_rule(agent_job_owner_digest(owner), saved['data']['rule_id'])
            assert row is not None
            self.assertFalse(row['enabled'])

    def test_follow_confirmation_rejects_changed_physical_topic(self):
        from dataclasses import replace
        context = ToolContext(owner='tg:v1:123\x1f456', session_id='shared', delivery_route={'chat_id': '123', 'message_thread_id': 77})
        with patch('app.modules.media_automation_rules.telegram_owner_route_is_currently_authorized', return_value=True):
            token = prepare_follow(**self.arguments, owner=context.owner, delivery_route=context.delivery_route)['snapshot']
            changed = replace(context, delivery_route={'chat_id': '123', 'message_thread_id': 78})
            with self.assertRaisesRegex(ValueError, '通知目标已改变'):
                follow(**self.arguments, owner=changed.owner, delivery_route=changed.delivery_route, expected_snapshot=token)
            self.assertEqual(follows(owner=context.owner)['data']['items'], [])

    def test_preparation_is_readonly_and_confirm_does_not_complete_download(self):
        token = prepare_follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route)['snapshot']
        self.assertEqual(follows(owner=self.context.owner)['data']['items'], [])
        result = follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route, expected_snapshot=token)
        self.assertTrue(result['data']['enabled'])
        self.assertIn('不表示原任务已完成', result['summary'])
        with db.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT status FROM download_requests WHERE id=?', (self.identifier,)).fetchone()[0], 'downloading')
        with self.assertRaises(ValueError):
            follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route, expected_snapshot=token)

    def test_owner_duration_and_route_changes_reject(self):
        token = prepare_follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route)['snapshot']
        contexts = [ToolContext(owner=web_kernel_owner('other'), session_id='one')]
        for context in contexts:
            with self.assertRaises(ValueError):
                follow(**self.arguments, owner=context.owner, delivery_route=context.delivery_route, expected_snapshot=token)
        changed_hours: dict[str, Any] = {**self.arguments, 'hours': 2}
        with self.assertRaises(ValueError):
            follow(**changed_hours, owner=self.context.owner, delivery_route=self.context.delivery_route, expected_snapshot=token)
        with patch('app.modules.media_automation_rules.config.get', return_value='456'), self.assertRaises(ValueError):
            follow(**self.arguments, owner=self.context.owner, delivery_route=self.context.delivery_route, expected_snapshot=token)

    def test_stop_confirmation_survives_scheduler_state_changes(self):
        for phase in ('claimed', 'checked', 'terminal'):
            with self.subTest(phase=phase):
                saved = self.save()
                args = {'rule_id': saved['data']['rule_id']}
                token = prepare_unfollow(**args, owner=self.context.owner)['snapshot']
                owner = agent_job_owner_digest(self.context.owner)
                before = rules.get_rule(owner, args['rule_id'])
                clock = datetime.now().astimezone() + timedelta(seconds=1)
                claim = rules.claim_due_rules(clock)[0]
                if phase != 'claimed':
                    self.assertTrue(rules.finish_rule(claim['id'], claim['lease_token'], (clock + timedelta(minutes=5)).isoformat(), disable=phase == 'terminal'))
                current = rules.get_rule(owner, args['rule_id'])
                assert before is not None
                assert current is not None
                self.assertEqual(current['settings'], before['settings'])
                self.assertEqual(current['revision'], before['revision'])
                self.assertTrue(unfollow(**args, owner=self.context.owner, expected_snapshot=token)['ok'])
                after = rules.get_rule(owner, args['rule_id'])
                assert after is not None
                self.assertFalse(after['enabled'])
                self.assertEqual(after['lease_token'], '')
                self.assertEqual(after['lease_until'], '')
                self.assertFalse(rules.owns_lease(claim['id'], claim['lease_token']))
                self.assertEqual(after['revision'], before['revision'] + 1)
                with self.assertRaises(ValueError):
                    unfollow(**args, owner=self.context.owner, expected_snapshot=token)

    def test_stop_confirmation_still_rejects_changed_rule_configuration(self):
        saved = self.save()
        args = {'rule_id': saved['data']['rule_id']}
        token = prepare_unfollow(**args, owner=self.context.owner)['snapshot']
        row = rules.get_rule(agent_job_owner_digest(self.context.owner), args['rule_id'])
        assert row is not None
        settings = {**row['settings'], 'title': 'Changed by another confirmation'}
        updated = rules.save_rule(row['owner_digest'], kind=row['kind'], settings=settings, enabled=True, next_run_at=row['next_run_at'], rule_id=row['id'], expected_revision=row['revision'])
        assert updated is not None
        with self.assertRaises(ValueError):
            unfollow(**args, owner=self.context.owner, expected_snapshot=token)
        assert row is not None
        get_rule_result = rules.get_rule(row['owner_digest'], row['id'])
        assert get_rule_result is not None
        self.assertTrue(get_rule_result['enabled'])

    def test_read_running_sends_nothing_then_terminal_notifies_once(self):
        saved = self.save()
        now = datetime.now().astimezone() + timedelta(seconds=1)
        with patch('app.modules.media_automation_rules.publish_notification_event', return_value=Mock(status='queued')) as send:
            self.assertEqual(drain_automation_rules(now=now), 1)
            send.assert_not_called()
            db.update_download_request(self.identifier, status='completed', gy_status='completed')
            self.assertEqual(drain_automation_rules(now=now + timedelta(minutes=6)), 1)
            send.assert_called_once()
            event = send.call_args.args[1]
            self.assertIn('不等于已入库', event.lines[0])
            self.assertIn('未建立媒体库可见性复核', str(event.fields))
            self.assertEqual(drain_automation_rules(now=now + timedelta(minutes=12)), 0)
            self.assertFalse(follows(owner=self.context.owner)['data']['items'][0]['enabled'])
            self.assertEqual(send.call_args.kwargs['chat_id'], '123')
        self.assertTrue(saved['ok'])

    def test_resubmission_ends_follow_once_without_claiming_download_or_library_completion(self):
        self.save()
        successor, _ = db.create_download_request('follow-successor', 'magnet', title='测试剧集')
        db.update_download_request(successor, targets='guangya', status='downloading', gy_status='downloading')
        run = db.add_task_run('organize', 'follow-resubmission')
        db.finish_task_run(run, 'failed', error='旧整理运行失败')
        db.update_download_request(self.identifier, targets='guangya', status='failed', gy_status='failed',
                                   organize_status='failed', organize_run_id=run)
        db.mark_download_request_resubmitted(self.identifier, successor_request_id=successor, targets='guangya')
        now = datetime.now().astimezone() + timedelta(seconds=1)
        with patch('app.modules.media_automation_rules.publish_notification_event', return_value=Mock(status='queued')) as send:
            self.assertEqual(drain_automation_rules(now=now), 1)
            send.assert_called_once()
            event = send.call_args.args[1]
            self.assertEqual(event.title, '活动跟踪结束')
            self.assertIn('移交', str(event.fields))
            self.assertIn('重提回执', str(event.fields))
            self.assertIn('不代表实际下载或入库完成', str(event.fields))
            self.assertIn('不等于已入库', event.lines[0])
            self.assertIn('未建立媒体库可见性复核', str(event.fields))
            self.assertEqual(drain_automation_rules(now=now + timedelta(minutes=6)), 0)
        self.assertFalse(follows(owner=self.context.owner)['data']['items'][0]['enabled'])
        successor_row = db.get_download_request(successor)
        assert successor_row is not None
        self.assertEqual(successor_row['status'], 'downloading')

    def test_resubmission_keeps_follow_until_the_other_backend_finishes(self):
        self.save()
        successor, _ = db.create_download_request('follow-successor', 'magnet', title='测试剧集')
        db.update_download_request(self.identifier, targets='both', status='manual_review',
                                   qb_status='downloading', gy_status='failed')
        db.mark_download_request_resubmitted(self.identifier, successor_request_id=successor, targets='guangya')
        now = datetime.now().astimezone() + timedelta(seconds=1)
        with patch('app.modules.media_automation_rules.publish_notification_event', return_value=Mock(status='queued')) as send:
            self.assertEqual(drain_automation_rules(now=now), 1)
            send.assert_not_called()
            self.assertTrue(follows(owner=self.context.owner)['data']['items'][0]['enabled'])
            db.update_download_request(self.identifier, qb_status='completed')
            self.assertEqual(drain_automation_rules(now=now + timedelta(minutes=6)), 1)
            send.assert_called_once()
            self.assertIn('移交', str(send.call_args.args[1].fields))
            self.assertEqual(drain_automation_rules(now=now + timedelta(minutes=12)), 0)
        self.assertFalse(follows(owner=self.context.owner)['data']['items'][0]['enabled'])

    def test_stop_owner_scope_does_not_cancel_request(self):
        saved = self.save()
        args = {'rule_id': saved['data']['rule_id']}
        with self.assertRaises(ValueError):
            prepare_unfollow(**args, owner=ToolContext(owner=web_kernel_owner('other')).owner)
        token = prepare_unfollow(**args, owner=self.context.owner)['snapshot']
        self.assertTrue(unfollow(**args, owner=self.context.owner, expected_snapshot=token)['ok'])
        with self.assertRaises(ValueError):
            unfollow(**args, owner=self.context.owner, expected_snapshot=token)
        self.assertEqual(drain_automation_rules(now=datetime.now().astimezone() + timedelta(minutes=6)), 0)

    def test_expiry_reports_unknown_not_completion_and_stops(self):
        self.save()
        with patch('app.modules.media_automation_rules.publish_notification_event', return_value=Mock(status='queued')) as send:
            drain_automation_rules(now=datetime.now().astimezone() + timedelta(hours=25))
            self.assertIn('尚未确认结束', str(send.call_args.args[1].fields))
        self.assertFalse(follows(owner=self.context.owner)['data']['items'][0]['enabled'])

    def test_failure_notification_once_and_no_retained_provider_secrets(self):
        self.save()
        db.update_download_request(self.identifier, status='failed', gy_status='failed', error='token=secret /private/file')
        with patch('app.modules.media_automation_rules.publish_notification_event', return_value=Mock(status='queued')) as send:
            drain_automation_rules(now=datetime.now().astimezone() + timedelta(seconds=1))
            self.assertNotIn('secret', str(send.call_args.args))
            self.assertIn('关注', send.call_args.args[1].title)
        self.assertFalse(follows(owner=self.context.owner)['data']['items'][0]['enabled'])

    def test_invalid_duration_and_injected_route_rejected(self):
        invalid: list[dict[str, Any]] = [{**self.arguments, 'hours': True}, {**self.arguments, 'chat_id': '321'}]
        for bad in invalid:
            with self.assertRaises((ValueError, TypeError)):
                prepare_follow(**bad, owner=self.context.owner, delivery_route=self.context.delivery_route)
        self.assertEqual(follows(owner=self.context.owner)['data']['items'], [])

    def test_agent_disabled_does_not_claim_or_send_web_owned_rules(self):
        self.save()
        with patch('app.modules.media_automation_rules.is_agent_enabled', return_value=False), patch('app.modules.media_automation_rules.publish_notification_event') as send:
            self.assertEqual(drain_automation_rules(now=datetime.now().astimezone() + timedelta(minutes=6)), 0)
            send.assert_not_called()
        self.assertTrue(follows(owner=self.context.owner)['data']['items'][0]['enabled'])

    def test_disable_during_read_prevents_late_outbox_admission(self):
        self.save()
        from app.modules.media_automation_rules import RuleDelivery
        from app.notifier import NotificationEvent
        with patch('app.modules.media_automation_rules.is_agent_enabled', side_effect=[True, False]), patch('app.modules.activity_follow_notifications.deliver_activity_follow', return_value=RuleDelivery(NotificationEvent('任务结束'), 'one', datetime.now().astimezone().isoformat(), terminal=True)), patch('app.modules.media_automation_rules.publish_notification_event') as send:
            self.assertEqual(drain_automation_rules(now=datetime.now().astimezone() + timedelta(seconds=1)), 0)
            send.assert_not_called()
        self.assertTrue(follows(owner=self.context.owner)['data']['items'][0]['enabled'])
