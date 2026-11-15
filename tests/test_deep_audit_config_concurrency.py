"""深审：普通保存和Agent CAS策略保存必须遵循同一锁顺序。"""
from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from starlette.requests import Request

from app import config
from app.modules import backup


def simultaneous_configuration_writers(directory, output):
    """用独立进程隔离旧实现的真实死锁，失败不能污染后续pytest全局锁。"""
    # 排除首次导入模块的锁，明确检验应用自己的RLock/文件锁顺序。
    from app.modules.process_lock import CrossProcessLock

    warmup = CrossProcessLock("config-snapshot", directory=Path(directory))
    warmup.acquire()
    warmup.release()
    target = Path(directory) / "user.env"
    simple_ready, policy_locked = threading.Event(), threading.Event()
    outcomes = {}
    original_update = config.update_runtime_env_file
    original_guard = backup.config_snapshot_guard

    def delayed_simple_update(*args, **kwargs):
        if threading.current_thread().name == "simple-config":
            simple_ready.set()
            if not policy_locked.wait(5):
                raise RuntimeError("策略写入未进入文件锁")
        return original_update(*args, **kwargs)

    @contextmanager
    def observed_guard(paths):
        with original_guard(paths):
            if threading.current_thread().name == "policy-config":
                policy_locked.set()
            yield

    with mock.patch.object(config, "ENV_FILE", target), \
            mock.patch.object(config, "PATHS", replace(config.PATHS, data_dir=Path(directory), config_dir=Path(directory))), \
            mock.patch.object(config, "_cache", None), \
            mock.patch.object(config, "update_runtime_env_file", side_effect=delayed_simple_update), \
            mock.patch.object(backup, "config_snapshot_guard", side_effect=observed_guard), \
            mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SIMPLE_KEY", None)
        os.environ.pop("POLICY_KEY", None)
        config.write_env_file(target, {"HISTORICAL_KEY": "preserved"}, replace=False)
        from app import database as db
        from app.agent.feature_gate import initialize_runtime_fence, read_runtime_fence
        db.configure_database(Path(directory) / "test.db", test_mode=True)
        db.init_db()
        initialize_runtime_fence()
        expected, _ = config.read_env_snapshot(target)

        def simple_writer():
            try:
                config.set_and_save({"SIMPLE_KEY": "simple"})
                outcomes["simple"] = "saved"
            except Exception as exc:
                outcomes["simple"] = type(exc).__name__

        def policy_writer():
            try:
                original_update(target, {"POLICY_KEY": "policy"}, expected=expected)
                outcomes["policy"] = "saved"
            except Exception as exc:
                outcomes["policy"] = type(exc).__name__

        simple = threading.Thread(target=simple_writer, name="simple-config", daemon=True)
        policy = threading.Thread(target=policy_writer, name="policy-config", daemon=True)
        simple.start()
        if not simple_ready.wait(5):
            raise RuntimeError("普通写入未取得快照")
        policy.start()
        simple.join(2)
        policy.join(2)
        alive = [thread.name for thread in (simple, policy) if thread.is_alive()]
        result = {
            "alive": alive, "outcomes": outcomes,
            "file": target.read_text(),
            "policy_runtime": os.environ.get("POLICY_KEY"),
            "simple_runtime": os.environ.get("SIMPLE_KEY"),
            "cache": dict(config._cache or {}),
        }
        if not alive:
            with db.get_conn() as conn:
                result["fence_phase"] = read_runtime_fence(conn).phase
            initialize_runtime_fence()
            original_update(target, {"SIMPLE_KEY": "retried"}, expected=target.read_bytes())
            result["retry_value"] = config.get("SIMPLE_KEY")
        output.put(result)


class ConfigLockOrderBusinessTests(unittest.TestCase):
    def test_settings_validate_related_fields_against_the_committed_predecessor(self):
        from app.agent import feature_gate
        from app.agent.feature_gate import initialize_runtime_fence
        from app.routes import api
        from tests.support import isolated_test_database

        first_saving, second_observed, first_saved = (threading.Event() for _ in range(3))
        original_get = config.get
        original_save = config.set_and_save
        original_guard = config.runtime_config_read_guard
        original_transition = feature_gate.agent_runtime_transition
        outcomes: dict[str, Any] = {}
        initial = {"DISCOVERY_CACHE_TTL_SECONDS": "60", "DISCOVERY_STALE_TTL_SECONDS": "600"}

        @contextmanager
        def observed_guard():
            if threading.current_thread().name == "second-settings":
                second_observed.set()
            with original_guard():
                yield

        @contextmanager
        def observed_transition():
            if threading.current_thread().name == "second-settings":
                second_observed.set()
            with original_transition():
                yield

        def observed_get(key, default=""):
            value = original_get(key, default)
            if (threading.current_thread().name == "second-settings"
                    and key == "DISCOVERY_CACHE_TTL_SECONDS" and not first_saved.is_set()):
                # 旧实现会在第一个请求尚未发布时读取旧值；发布锁必须覆盖此验证。
                second_observed.set()
                if not first_saved.wait(5):
                    raise RuntimeError("第一个设置请求没有结束")
            return value

        def delayed_save(updates):
            if threading.current_thread().name == "first-settings":
                first_saving.set()
                if not second_observed.wait(5):
                    raise RuntimeError("第二个设置请求没有进入")
            original_save(updates)
            if threading.current_thread().name == "first-settings":
                first_saved.set()

        def save(name, payload):
            request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(background_services_enabled=False))})
            try:
                outcomes[name] = api.save_config(request, payload)
            except Exception as exc:
                outcomes[name] = exc

        with tempfile.TemporaryDirectory() as directory, isolated_test_database(), \
                mock.patch.dict(os.environ, initial), \
                mock.patch.object(config, "ENV_FILE", Path(directory) / "user.env"), \
                mock.patch.object(config, "_cache", None):
            config.write_env_file(config.ENV_FILE, initial, replace=False)
            initialize_runtime_fence()
            with mock.patch.object(api, "require_api_login"), \
                    mock.patch.object(config, "get", side_effect=observed_get), \
                    mock.patch.object(config, "set_and_save", side_effect=delayed_save), \
                    mock.patch.object(config, "runtime_config_read_guard", side_effect=observed_guard), \
                    mock.patch.object(feature_gate, "agent_runtime_transition", side_effect=observed_transition), \
                    mock.patch("app.discovery.service.shutdown_discovery_service"), \
                    mock.patch("app.discovery.search.shutdown_discovery_search_service"), \
                    mock.patch("app.modules.recognition_hints.clear_recognition_hint_cache"), \
                    mock.patch("app.services.clear_dashboard_cache"):
                first = threading.Thread(target=save, name="first-settings", args=("first", {"DISCOVERY_CACHE_TTL_SECONDS": "500"}), daemon=True)
                second = threading.Thread(target=save, name="second-settings", args=("second", {"DISCOVERY_STALE_TTL_SECONDS": "300"}), daemon=True)
                first.start()
                self.assertTrue(first_saving.wait(5))
                second.start()
                first.join(10)
                second.join(10)
                self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(outcomes["first"], {"success": True})
            self.assertEqual(getattr(outcomes["second"], "status_code", None), 400, outcomes)
            _, values = config.read_env_snapshot(config.ENV_FILE)
            self.assertEqual(values, {**initial, "DISCOVERY_CACHE_TTL_SECONDS": "500"})

    def test_concurrent_plain_and_policy_writes_finish_with_safe_cas_conflict(self):
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        with tempfile.TemporaryDirectory() as directory:
            process = context.Process(target=simultaneous_configuration_writers, args=(directory, output))
            process.start()
            try:
                result = output.get(timeout=12)
                process.join(5)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(result["alive"], [], f"配置写入未完成：{result['outcomes']}")
                self.assertEqual(result["outcomes"], {"simple": "ConcurrentConfigUpdateError", "policy": "saved"})
                self.assertEqual(result["policy_runtime"], "policy")
                self.assertIsNone(result["simple_runtime"])
                self.assertEqual(result["cache"], {"HISTORICAL_KEY": "preserved", "POLICY_KEY": "policy"})
                self.assertIn("HISTORICAL_KEY", result["file"])
                self.assertIn("POLICY_KEY", result["file"])
                self.assertNotIn("SIMPLE_KEY", result["file"])
                self.assertEqual(result["fence_phase"], "stable")
                self.assertEqual(result["retry_value"], "retried")
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                output.close()
                output.join_thread()
