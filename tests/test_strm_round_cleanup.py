from __future__ import annotations

from unittest.mock import patch

from app.modules.scheduler import STRMScheduler


class TestStrmRoundCleanup:
    def test_full_round_runs_shared_empty_directory_sweep_once(self):
        scheduler = STRMScheduler()
        sources = [
            {"id": "a", "name": "A", "rel_prefix": "A"},
            {"id": "b", "name": "B", "rel_prefix": "B"},
        ]
        scheduler._source_runtime = [
            {"id": item["id"], "name": item["name"], "status": "pending", "completed": 0, "total": 0}
            for item in sources
        ]
        source_stats = scheduler._empty_stats()
        retirement = {
            "sources": 0, "blocked": 0, "cleaned": 0,
            "empty_dirs_cleaned": 0, "removed_paths": [],
            "removed_dir_paths": [], "errors": [], "stopped": False,
            "empty_dir_roots": ["/tmp/strm/光鸭云盘", "/tmp/strm/光鸭云盘"],
        }
        with patch(
            "app.modules.scheduler.configured_strm_source_plans",
            return_value=(sources, ""),
        ), patch(
            "app.modules.scheduler.clean_retired_strm_sources",
            return_value=retirement,
        ), patch(
            "app.modules.scheduler.sync_strm",
            side_effect=[dict(source_stats), dict(source_stats)],
        ) as sync, patch(
            "app.modules.scheduler.clean_empty_strm_dirs",
            return_value={
                "empty_dirs_cleaned": 2,
                "removed_dir_paths": ["/tmp/strm/A/empty", "/tmp/strm/B/empty"],
                "stopped": False,
            },
        ) as empty_cleanup, patch(
            "app.modules.scheduler.db.cancel_retired_strm_metadata_jobs", return_value=0,
        ):
            aggregate, source_results, stopped = scheduler._run_full_sources(
                sources,
                base_url="http://media.invalid",
                strm_root="/tmp/strm",
                exts={"mkv"},
                metadata_exts=set(),
                threshold=0,
            )

        assert not stopped
        assert len(source_results) == 2
        assert source_results[0]["local_dir"] == "/tmp/strm/光鸭云盘/A"
        assert source_results[1]["local_dir"] == "/tmp/strm/光鸭云盘/B"
        assert aggregate["empty_dirs_cleaned"] == 2
        assert empty_cleanup.call_count == 1
        assert empty_cleanup.call_args.kwargs["owned_root"].name == "光鸭云盘"
        assert sync.call_count == 2
        assert all(call.kwargs["clean_empty_dirs"] is False for call in sync.call_args_list)
        assert all(call.kwargs["clean_invalid"] is False for call in sync.call_args_list)
        assert all(isinstance(call.kwargs["deferred_cleanup_actions"], list) for call in sync.call_args_list)
        assert all("defer_metadata" not in call.kwargs for call in sync.call_args_list)


    def test_full_round_defers_destructive_cleanup_until_every_source_scan_is_safe(self):
        scheduler = STRMScheduler()
        sources = [
            {"id": "a", "name": "A", "rel_prefix": "A"},
            {"id": "b", "name": "B", "rel_prefix": "B"},
        ]
        scheduler._source_runtime = [
            {"id": item["id"], "name": item["name"], "status": "pending", "completed": 0, "total": 0}
            for item in sources
        ]
        events = []

        def sync_source(**kwargs):
            source_id = kwargs["source_dir_id"]
            events.append(f"scan:{source_id}")
            kwargs["deferred_cleanup_actions"].append(
                lambda source_id=source_id: events.append(f"cleanup:{source_id}")
            )
            return scheduler._empty_stats()

        def retire(*args, **kwargs):
            events.append("retirement")
            return {
                "sources": 0, "blocked": 0, "cleaned": 0,
                "empty_dirs_cleaned": 0, "removed_paths": [],
                "removed_dir_paths": [], "errors": [], "stopped": False,
                "empty_dir_roots": [],
            }

        with patch(
            "app.modules.scheduler.configured_strm_source_plans",
            return_value=(sources, ""),
        ), patch(
            "app.modules.scheduler.sync_strm", side_effect=sync_source,
        ), patch(
            "app.modules.scheduler.clean_retired_strm_sources", side_effect=retire,
        ), patch(
            "app.modules.scheduler.clean_empty_strm_dirs",
            return_value={"empty_dirs_cleaned": 0, "removed_dir_paths": [], "stopped": False},
        ), patch(
            "app.modules.scheduler.db.cancel_retired_strm_metadata_jobs", return_value=0,
        ):
            scheduler._run_full_sources(
                sources, base_url="http://media.invalid", strm_root="/tmp/strm",
                exts={"mkv"}, metadata_exts=set(), threshold=0,
            )

        assert events == [
            "scan:a", "scan:b", "cleanup:a", "cleanup:b", "retirement",
        ]

    def test_full_round_scan_failure_blocks_all_deletes_retirement_and_empty_sweep(self):
        scheduler = STRMScheduler()
        sources = [
            {"id": "a", "name": "A", "rel_prefix": "A"},
            {"id": "b", "name": "B", "rel_prefix": "B"},
        ]
        scheduler._source_runtime = [
            {"id": item["id"], "name": item["name"], "status": "pending", "completed": 0, "total": 0}
            for item in sources
        ]
        cleanup_events = []

        def sync_source(**kwargs):
            source_id = kwargs["source_dir_id"]
            kwargs["deferred_cleanup_actions"].append(
                lambda source_id=source_id: cleanup_events.append(source_id)
            )
            stats = scheduler._empty_stats()
            if source_id == "b":
                stats["scan_incomplete"] = True
                stats["scan_errors"] = 1
            return stats

        with patch(
            "app.modules.scheduler.configured_strm_source_plans",
            return_value=(sources, ""),
        ), patch(
            "app.modules.scheduler.sync_strm", side_effect=sync_source,
        ), patch(
            "app.modules.scheduler.clean_retired_strm_sources",
        ) as retire, patch(
            "app.modules.scheduler.clean_empty_strm_dirs",
        ) as empty_cleanup, patch(
            "app.modules.scheduler.db.cancel_retired_strm_metadata_jobs",
        ) as cancel_retired:
            aggregate, source_results, stopped = scheduler._run_full_sources(
                sources, base_url="http://media.invalid", strm_root="/tmp/strm",
                exts={"mkv"}, metadata_exts=set(), threshold=0,
            )

        assert not stopped
        assert cleanup_events == []
        retire.assert_not_called()
        empty_cleanup.assert_not_called()
        cancel_retired.assert_not_called()
        assert aggregate["clean_skipped"] is True
        assert all(result["stats"]["clean_skipped"] is True for result in source_results)
        assert any("整轮扫描未完整" in str(item) for item in aggregate["error_samples"])

    def test_scoped_round_never_retires_unselected_sources_or_sweeps_global_root(self):
        scheduler = STRMScheduler()
        sources = [{"id": "a", "name": "整理", "rel_prefix": "整理"}]
        scheduler._source_runtime = [
            {"id": "a", "name": "整理", "status": "pending", "completed": 0, "total": 0}
        ]
        source_stats = scheduler._empty_stats()
        with patch(
            "app.modules.scheduler.configured_strm_source_plans",
            return_value=(sources, ""),
        ), patch(
            "app.modules.scheduler.sync_strm", return_value=source_stats,
        ), patch(
            "app.modules.scheduler.clean_retired_strm_sources",
        ) as retire, patch(
            "app.modules.scheduler.db.cancel_retired_strm_metadata_jobs",
        ) as cancel_retired, patch(
            "app.modules.scheduler.clean_empty_strm_dirs",
            return_value={
                "empty_dirs_cleaned": 0, "removed_dir_paths": [], "stopped": False,
            },
        ) as empty_cleanup:
            aggregate, source_results, stopped = scheduler._run_full_sources(
                sources,
                base_url="http://media.invalid",
                strm_root="/tmp/strm",
                exts={"mkv"},
                metadata_exts=set(),
                threshold=0,
                active_ids_complete=False,
            )

        assert not stopped
        assert len(source_results) == 1
        assert aggregate["retired_sources"] == 0
        retire.assert_not_called()
        cancel_retired.assert_not_called()
        empty_cleanup.assert_called_once()
        assert str(empty_cleanup.call_args.kwargs["owned_root"]).endswith(
            "/光鸭云盘/整理"
        )

    def test_source_local_dir_uses_same_single_component_sanitizing_as_strm_targets(self):
        from app.modules.scheduler import _source_local_dir
        from app.modules.strm import safe_path_component

        source = {"id": "a", "name": "影视/动画", "rel_prefix": "影视/动画"}
        expected = f"/tmp/strm/光鸭云盘/{safe_path_component('影视/动画')}"

        assert _source_local_dir("/tmp/strm", source) == expected
