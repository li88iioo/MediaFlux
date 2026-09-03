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
OLD_MODULES = {f"app.agent.{name.removesuffix('.py')}" for name in OLD_CONTROL_FILES}


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
            ROOT / "app" / "agent" / "domain_catalog",
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
                    names = {str(node.module or "")}
                else:
                    continue
                overlap = names & OLD_MODULES
                if overlap:
                    violations.append(f"{path}:{node.lineno}:{sorted(overlap)}")
        self.assertEqual(violations, [])

    def test_kernel_has_one_compact_control_plane_without_business_regex_router(
        self,
    ) -> None:
        forbidden_names = {
            "is_workspace_health_message",
            "is_download_queue_diagnosis_message",
            "rss_subscription_summary_request",
            "is_unsafe_qb_bulk_delete_request",
            "catalog_from_existing_registry",
        }
        sources = [path.read_text(encoding="utf-8") for path in KERNEL.rglob("*.py")]
        source = "\n".join(sources)
        self.assertTrue(forbidden_names.isdisjoint(source.split()))
        line_count = sum(text.count("\n") + 1 for text in sources)
        self.assertGreaterEqual(line_count, 4_000)
        self.assertLessEqual(line_count, 7_000)

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
