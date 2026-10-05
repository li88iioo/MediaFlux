"""受控季集研究设置：源码隔离 API/内存配置与静态 UI 回归，不加载 DB 或模型。"""
from __future__ import annotations

import ast
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
from contextlib import nullcontext
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ENABLED = "AGENT_EPISODE_RESEARCH_ENABLED"
LIMIT = "AGENT_EPISODE_RESEARCH_DAILY_LIMIT"
PARENT = "AGENT_RECOGNITION_REVIEW_ENABLED"
VALIDATOR = "_validate_agent_episode_research_updates"


class MemoryConfig:
    """只在内存发布；禁止依赖生产 ENV_FILE、密钥与数据库。"""

    AtomicPublishError = OSError

    def __init__(self):
        self.values = {}
        self.overrides = {}
        self.set_and_save = Mock(side_effect=self.values.update)

    def get(self, key, default=""):
        return self.overrides.get(key, self.values.get(key, default))

    def get_bool(self, key, default=False):
        return str(self.get(key, default)).strip().lower() in {"1", "true", "yes", "on"}

    def all_items(self):
        return dict(self.values)

    def has_external_override(self, key):
        return key in self.overrides


def _module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


def _load_settings_api(config):
    """执行真实路由/注册代码，剥离顶层导入和无关校验，防止启动业务服务。"""
    namespace = {
        "config": config, "threading": threading, "time": time, "re": re,
        "logger": logging.getLogger(__name__), "Body": lambda **_: None,
        "require_api_login": Mock(), "redact_config": dict,
        "api_error": lambda message, status=400: SimpleNamespace(
            status_code=status, body=json.dumps({"error": message}).encode(),
        ),
    }
    defaults_path = ROOT / "app/defaults.py"
    exec(compile(defaults_path.read_text("utf-8"), str(defaults_path), "exec"), namespace)
    path = ROOT / "app/routes/api.py"
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    functions = {"_is_config_mask", "_normalize_discovery_boolean", VALIDATOR, "get_config", "save_config"}
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "app.indexers.config":
            nodes.append(node)
        elif isinstance(node, ast.Assign) and all(
            isinstance(target, ast.Name) and target.id.startswith("_") for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef):
            if node.name in functions:
                node.decorator_list = []
                nodes.append(node)
            elif node.name.startswith("_validate_"):
                namespace[node.name] = Mock(return_value={})
    isolated = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(isolated, str(path), "exec"), namespace)
    return namespace


class EpisodeResearchSettingsTests(unittest.TestCase):
    def setUp(self):
        self.config = MemoryConfig()
        self.api = _load_settings_api(self.config)
        self.invalidate = Mock()
        self.wake = Mock()
        modules = {
            "app.services": _module("app.services", clear_dashboard_cache=Mock()),
            "app.agent.feature_gate": _module(
                "app.agent.feature_gate", agent_runtime_transition=nullcontext,
                invalidate_agent_runtime_generation=self.invalidate,
            ),
            "app.modules.organize_confirmations": _module(
                "app.modules.organize_confirmations", wake_recognition_review_dispatcher=self.wake,
            ),
            "app.modules.offline": _module(
                "app.modules.offline", DEFAULT_VIDEO_EXTS_CSV="mkv,mp4",
                normalize_video_extensions=lambda _: ("mkv", "mp4"),
            ),
            # 任何意外加载这些运行时依赖都会立即失败，不会连接真实资源。
            "app.database": None, "sqlite3": None, "requests": None,
            "app.agent.kernel.bootstrap": None,
        }
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.request = SimpleNamespace(
            session={"logged_in": True},
            app=SimpleNamespace(state=SimpleNamespace(background_services_enabled=False, media_proxy_manager=None)),
        )

    def save(self, payload):
        return self.api["save_config"](self.request, payload)

    def get(self):
        return self.api["get_config"](self.request)

    def test_shared_defaults_and_missing_config_are_independently_off(self):
        self.assertIs(self.api["DEFAULT_BOOL_CONFIG_VALUES"].get(ENABLED), False)
        self.assertEqual(self.api.get("DEFAULT_AGENT_EPISODE_RESEARCH_DAILY_LIMIT"), 10)
        self.config.values.update({PARENT: "1", "AGENT_ENABLED": "1", "AGENT_LLM_ENABLED": "1"})
        self.assertEqual(self.get().get(ENABLED), "0")
        self.assertEqual(self.get().get(LIMIT), "10")
        self.assertNotIn(ENABLED, self.config.values)
        self.assertNotIn(LIMIT, self.config.values)

    def test_both_keys_are_registered_and_have_api_defaults(self):
        for key, default in ((ENABLED, "0"), (LIMIT, "10")):
            with self.subTest(key=key):
                self.assertIn(key, self.api["_CONFIG_UI_SAVEABLE_KEYS"])
                self.assertEqual(self.api["_AGENT_SETTINGS_DEFAULTS"].get(key), default)

    def test_boolean_normalization_roundtrips_without_enabling_dependencies(self):
        for raw, expected in ((True, "1"), (False, "0"), (" on ", "1"), ("off", "0"), ("YES", "1"), ("n", "0")):
            with self.subTest(value=raw):
                self.assertEqual(self.save({ENABLED: raw}), {"success": True})
                self.assertEqual(self.get()[ENABLED], expected)
                self.assertEqual(self.config.values, {ENABLED: expected})
        self.assertEqual(self.invalidate.call_count, 6)
        self.assertEqual(self.wake.call_count, 6)

    def test_valid_daily_limits_roundtrip_as_canonical_integers(self):
        for raw, expected in ((1, "1"), (100, "100"), (10, "10"), (" 07 ", "7")):
            with self.subTest(value=raw):
                self.assertEqual(self.save({LIMIT: raw}), {"success": True})
                self.assertEqual(self.get()[LIMIT], expected)
                self.assertEqual(self.config.values, {LIMIT: expected})

    def test_invalid_boolean_never_publishes(self):
        for raw in ("", "enabled", "2", 2, None, [], {}):
            with self.subTest(value=raw):
                result = self.save({ENABLED: raw, LIMIT: 10})
                self.assertEqual(result.status_code, 400)
                self.assertIn(ENABLED, json.loads(result.body)["error"])
                self.assertEqual(self.config.values, {})
        self.config.set_and_save.assert_not_called()
        self.invalidate.assert_not_called()
        self.wake.assert_not_called()

    def test_invalid_daily_limit_rejects_entire_update(self):
        for raw in (0, -1, 101, "", " ", None, False, True, 1.5, 10.0, "1.0", "NaN", "inf", "1e1", [], {}):
            with self.subTest(value=raw):
                result = self.save({ENABLED: "1", LIMIT: raw})
                self.assertEqual(result.status_code, 400)
                self.assertIn(LIMIT, json.loads(result.body)["error"])
                self.assertEqual(self.config.values, {})
        self.config.set_and_save.assert_not_called()
        self.invalidate.assert_not_called()
        self.wake.assert_not_called()

    def test_parent_toggle_and_existing_quota_do_not_grant_child_permission(self):
        self.config.values.update({"TAVILY_DAILY_CREDIT_LIMIT": "23", "WEB_SEARCH_ENABLED": "0"})
        self.assertEqual(self.save({PARENT: "1"}), {"success": True})
        self.assertEqual(self.get().get(ENABLED), "0")
        self.assertNotIn(ENABLED, self.config.values)
        self.assertEqual(self.save({ENABLED: "1", LIMIT: "10"}), {"success": True})
        self.assertEqual(self.config.values["TAVILY_DAILY_CREDIT_LIMIT"], "23")
        self.assertEqual(self.config.values["WEB_SEARCH_ENABLED"], "0")
        self.assertNotIn("AGENT_ENABLED", self.config.values)
        self.assertNotIn("AGENT_LLM_ENABLED", self.config.values)

    def test_unchanged_normalized_values_do_not_publish_or_wake(self):
        self.config.values.update({ENABLED: "0", LIMIT: "10"})
        self.assertEqual(self.save({ENABLED: "false", LIMIT: " 10 "}), {"success": True})
        self.config.set_and_save.assert_not_called()
        self.invalidate.assert_not_called()
        self.wake.assert_not_called()

    def test_environment_overrides_are_visible_and_locked_for_both_keys(self):
        for key, deployed, attempted in ((ENABLED, "1", "0"), (LIMIT, "25", "10")):
            with self.subTest(key=key):
                self.config.overrides = {key: deployed}
                self.assertEqual(self.get().get(key), deployed)
                self.assertIn(key, self.get()["__managed_fields"])
                response = self.save({key: attempted})
                self.assertEqual(response.status_code, 409)
                self.assertIn(key, json.loads(response.body)["error"])
        self.config.set_and_save.assert_not_called()


class InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "data-key" in attrs:
            self.inputs[attrs["data-key"]] = attrs


class EpisodeResearchSettingsUiTests(unittest.TestCase):
    def test_controls_reuse_settings_rows_and_keep_stable_accessible_placeholders(self):
        html = (ROOT / "app/templates/settings.html").read_text("utf-8")
        metadata = html.split('id="settings-panel-metadata"', 1)[1].split('id="settings-panel-discovery"', 1)[0]
        parser = InputParser()
        parser.feed(metadata)
        self.assertIn(ENABLED, parser.inputs)
        self.assertIn(LIMIT, parser.inputs)
        toggle, limit = parser.inputs[ENABLED], parser.inputs[LIMIT]
        self.assertEqual(toggle["type"], "checkbox")
        self.assertNotIn("checked", toggle)
        self.assertIn("disabled", toggle)
        self.assertEqual(toggle["aria-describedby"], "episodeResearchDescription episodeResearchDependency")
        for key, value in (("type", "number"), ("min", "1"), ("max", "100"), ("step", "1"), ("value", "10")):
            self.assertEqual(limit[key], value)
        self.assertIn("disabled", limit)
        self.assertIn("aria-label", toggle)
        self.assertIn("aria-describedby", limit)
        self.assertLess(metadata.index(f'data-key="{PARENT}"'), metadata.index(f'data-key="{ENABLED}"'))
        self.assertLess(metadata.index(f'data-key="{ENABLED}"'), metadata.index(f'data-key="{LIMIT}"'))
        for key in (ENABLED, LIMIT):
            self.assertEqual(html.count(f'data-key="{key}"'), 1)
        option = metadata.split('class="metadata-option metadata-episode-research"', 1)[1].split('</div>', 1)[0]
        self.assertIn('class="toggle"', option)
        self.assertIn('.metadata-episode-research .toggle { width: 44px; height: 44px; }', html)
        self.assertIn('.metadata-episode-research input:focus-visible + .toggle-slider', html)
        self.assertNotIn(" hidden", option)
        self.assertNotIn('style=', option)
        for text in ("默认关闭", "光鸭待确认剧集", "冻结案例", "无冲突新归档", "不自动覆盖或删除目标", "本地媒体继续旧复核", "首版不执行本地文件映射", "TMDB 剧集组", "唯一", "可验证", "歧义", "待确认", "Tavily", "可选", "现有额度", "Agent 主动复核", "模型地址", "模型名"):
            self.assertIn(text, option)

    def test_javascript_defaults_and_readiness_hooks(self):
        script = (ROOT / "app/static/js/settings.js").read_text("utf-8")
        self.assertIn(f"{ENABLED}:'0'", script)
        self.assertIn(f"{LIMIT}:'10'", script)
        ready = script.split("function setConfigReady(){", 1)[1].split("function setConfigLoadError", 1)[0]
        self.assertLess(ready.index("syncEpisodeResearchAvailability()"), ready.index("revealConfigFields()"))
        normalize = script.split("loadAppConfig().then(config=>{", 1)[1].split("fillConfigFields(form,config)", 1)[0]
        self.assertIn("episodeResearchToggle", normalize)
        self.assertIn("episodeResearchDependencies", normalize)

    @unittest.skipUnless(shutil.which("node"), "静态依赖状态验证需要 Node.js")
    def test_dependency_events_preserve_values_and_dom_in_node(self):
        script = (ROOT / "app/static/js/settings.js").read_text("utf-8")
        start = script.index("    const episodeResearchToggle=")
        end = script.index("    function revealConfigFields", start)
        snippet = script[start:end]
        # 状态同步不能重建、隐藏或改写提示，确保移动端换行和设置行占位不变。
        for forbidden in ("innerHTML", "hidden", "style.", "textContent", "replaceChildren", ".checked=", ".value="):
            self.assertNotIn(forbidden, snippet)
        harness = r'''
const assert=require('node:assert/strict');
const enabled='AGENT_EPISODE_RESEARCH_ENABLED', limit='AGENT_EPISODE_RESEARCH_DAILY_LIMIT';
const keys=[enabled,limit,'AGENT_ENABLED','AGENT_LLM_ENABLED','AGENT_RECOGNITION_REVIEW_ENABLED','AGENT_LLM_API_URL','AGENT_LLM_MODEL'];
const fields=Object.fromEntries(keys.map(key=>[key,{
  type:key.endsWith('_ENABLED')?'checkbox':'text',checked:false,value:'',disabled:true,dataset:{},events:{},
  addEventListener(type,handler){(this.events[type]??=[]).push(handler);},
  fire(type){for(const handler of this.events[type]||[])handler();}
}]));
const form={querySelector(selector){return fields[selector.match(/data-key="([^"]+)"/)[1]]||null;}};
let configReady=false;
__SNIPPET__
const toggle=fields[enabled], quota=fields[limit];
toggle.checked=true;quota.value='17';
for(const key of keys.slice(2)){fields[key].checked=true;fields[key].value='configured';}
syncEpisodeResearchAvailability();assert.equal(toggle.disabled,true);assert.equal(quota.disabled,true);
configReady=true;syncEpisodeResearchAvailability();assert.equal(toggle.disabled,false);assert.equal(quota.disabled,false);
for(const key of keys.slice(2)){
 const field=fields[key];
 for(const event of ['input','change']){
  field.checked=false;field.value='  ';field.fire(event);
  assert.equal(toggle.disabled,true,key);assert.equal(quota.disabled,true,key);
  assert.equal(toggle.checked,true);assert.equal(quota.value,'17');
  field.checked=true;field.value='configured';field.fire(event);
  assert.equal(toggle.disabled,false,key);assert.equal(quota.disabled,false,key);
 }
}
toggle.checked=false;toggle.fire('change');assert.equal(quota.disabled,true);assert.equal(toggle.disabled,false);
toggle.checked=true;toggle.fire('change');assert.equal(quota.disabled,false);
for(const field of [toggle,quota]){
 field.dataset.managedByEnvironment='true';syncEpisodeResearchAvailability();assert.equal(field.disabled,true);
 field.dataset.managedByEnvironment='false';syncEpisodeResearchAvailability();assert.equal(field.disabled,false);
}
// 重复同步不应修改授权/限额；缺少 DOM 依赖或重新等待配置时必须禁用。
for(let i=0;i<10;i++)syncEpisodeResearchAvailability();
assert.equal(toggle.checked,true);assert.equal(quota.value,'17');
episodeResearchDependencies[4]=null;syncEpisodeResearchAvailability();
assert.equal(toggle.disabled,true);assert.equal(quota.disabled,true);
episodeResearchDependencies[4]=fields['AGENT_LLM_MODEL'];
configReady=false;syncEpisodeResearchAvailability();
assert.equal(toggle.disabled,true);assert.equal(quota.disabled,true);
console.log('dependency state matrix passed (no browser/network/DB)');
'''
        completed = subprocess.run(
            [shutil.which("node"), "-e", harness.replace("__SNIPPET__", snippet)],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_formal_configuration_guide_explains_authorization_and_shared_search_budget(self):
        guide = (ROOT / "docs/配置参考.md").read_text("utf-8")
        self.assertIn(f"| `{ENABLED}` | `0` |", guide)
        self.assertIn(f"| `{LIMIT}` | `10` |", guide)
        section = guide.split(f"| `{ENABLED}`", 1)[1].split("光鸭 NSFW 子授权位于", 1)[0]
        for text in ("1–100", "默认关闭", "光鸭待确认剧集", "冻结案例", "无冲突新归档", "不自动覆盖或删除目标", "local_media", "继续旧复核", "首版未实现本地文件映射执行", "AGENT_RECOGNITION_REVIEW_ENABLED", "AGENT_ENABLED", "AGENT_LLM_ENABLED", "模型地址", "模型名", "TMDB 剧集组", "唯一", "稳定 episode ID", "Tavily", "可选", "TAVILY_DAILY_CREDIT_LIMIT", "待确认", "网页"):
            self.assertIn(text, section)


if __name__ == "__main__":
    unittest.main()
