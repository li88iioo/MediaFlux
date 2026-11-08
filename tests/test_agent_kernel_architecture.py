from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "app" / "agent" / "kernel"
OLD_CONTROL_FILES = {
    "orchestrator.py",
    "llm_router.py",
    "service.py",
    "presentation_stream.py",
    "tools.py",
    "registry.py",
    "result_projection.py",
    "conversation_compaction.py",
    "conversation_history.py",
    "pending_action_actions.py",
}
OLD_MODULES = {f"app.agent.{name.removesuffix('.py')}" for name in OLD_CONTROL_FILES} | {
    "app.agent.domain_catalog", "app.agent.kernel.discovery",
}


class AgentKernelArchitectureTests(unittest.TestCase):
    def test_old_control_plane_is_deleted_not_hidden_as_legacy(self) -> None:
        agent_root = ROOT / "app" / "agent"
        existing = {path.name for path in agent_root.glob("*.py")}
        self.assertTrue(OLD_CONTROL_FILES.isdisjoint(existing))
        self.assertFalse(
            any(
                path.name.casefold().startswith("legacy")
                for path in agent_root.rglob("*")
            )
        )
        self.assertFalse((ROOT / "app" / "routes" / "agent_kernel_api.py").exists())
        self.assertFalse((ROOT / "app" / "bot" / "agent_kernel_adapter.py").exists())
        self.assertFalse((ROOT / "app" / "static" / "js" / "agent_kernel.js").exists())

    def test_production_agent_entrypoints_never_import_old_control_plane(self) -> None:
        roots = [
            KERNEL,
            ROOT / "app" / "agent" / "domains",
            ROOT / "app" / "routes" / "agent_api.py",
            ROOT / "app" / "bot" / "agent_adapter.py",
        ]
        violations: list[str] = []
        paths: list[Path] = []
        for root in roots:
            paths.extend(root.rglob("*.py") if root.is_dir() else [root])
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {item.name for item in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {node.module or ""}
                else:
                    continue
                overlap = names & OLD_MODULES
                if overlap:
                    violations.append(f"{path}:{node.lineno}:{sorted(overlap)}")
        self.assertEqual(violations, [])

    def test_kernel_has_one_control_plane_without_business_regex_router(
        self,
    ) -> None:
        forbidden_names = {
            "is_workspace_health_message",
            "is_download_queue_diagnosis_message",
            "rss_subscription_summary_request",
            "is_unsafe_qb_bulk_delete_request",
            "catalog_from_existing_registry",
            "CapabilityRetriever",
            "CapabilityDiscovery",
        }
        sources = [path.read_text(encoding="utf-8") for path in KERNEL.rglob("*.py")]
        identifiers = {
            node.id if isinstance(node, ast.Name) else node.attr
            for text in sources for node in ast.walk(ast.parse(text))
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        self.assertTrue(forbidden_names.isdisjoint(identifiers))

    def test_execution_kernel_does_not_import_business_ports(self) -> None:
        violations: list[str] = []
        for name in ("pipeline.py", "session.py", "turn.py", "model_rounds.py", "tool_batches.py", "turn_history.py"):
            path = KERNEL / name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                    modules.extend(f"{node.module or ''}.{item.name}" for item in node.names)
                else:
                    continue
                if any({"ports", "domains"}.intersection(module.split(".")) for module in modules):
                    violations.append(f"{path}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_session_does_not_resolve_channel_or_concrete_model_policy(self) -> None:
        path = KERNEL / "session.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = {"telegram_model_preferences", "OpenAICompatibleModelAdapter"}
        violations: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(item.name for item in node.names)]
            elif isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            else:
                continue
            if any(forbidden.intersection(name.split(".")) for name in names):
                violations.append(node.lineno)
        self.assertEqual(violations, [])

    def test_web_adapter_only_uses_kernel_event_endpoints(self) -> None:
        source = (ROOT / "app" / "static" / "js" / "agent.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("async function readEventStream", source)
        self.assertIn("response.body.getReader", source)
        self.assertIn("/api/agent/query", source)
        self.assertIn("/api/agent/actions/confirm", source)
        self.assertNotIn("/api/agent/tools/", source)
        self.assertNotIn("/prepare", source)
        self.assertNotIn("presentation", source.casefold())


if __name__ == "__main__":
    unittest.main()
