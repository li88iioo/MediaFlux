"""真实任务判据的离线对照；伪造轨迹只用于测验收器，不计真实成功率。"""

from __future__ import annotations

import copy
import inspect
import json
from typing import Any, cast

import pytest

from tools import eval_agent


@pytest.fixture
def cases():
    return {row["id"]: row for row in eval_agent.load_live_cases()}


def trace(case) -> dict[str, Any]:
    return {
        "case_id": case["id"],
        "case_version": case["version"],
        "source": "synthetic_test",
        "confirmed_executions": 0,
        "failure": None,
        "tools": [],
        "turns": [
            {"status": "success", "answer": "", "events": []} for _ in case["messages"]
        ],
        "model_calls": [
            {
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
                "http_attempts": 1,
            }
        ],
    }


def receipt(name, arguments, data, **extra) -> dict[str, Any]:
    return {
        "tool": name,
        "arguments": arguments,
        "public": {"ok": True, "data": data},
        **extra,
    }


def sdk_read(method, arguments, *, turn=1, evidence_id="sdk-1"):
    """实际调用公共 SDK，记录与宿主 collector 相同的原始响应，不伪造旧工具回执。"""
    from app.sdk import METHODS

    function = METHODS[method]
    bound = inspect.signature(function).bind(**arguments)
    bound.apply_defaults()
    return {"kind": "sdk_read", "evidence_id": evidence_id, "source": f"host-raw.jsonl#{evidence_id}",
            "turn": turn, "method": method, "arguments": dict(bound.arguments), "result": function(**bound.arguments)}


def sdk_host(row, observations):
    row["run_id"] = "native-eval-fixture"
    return {"run_id": row["run_id"], "case_id": row["case_id"], "case_version": row["case_version"],
            "effects": [], "sdk_reads": observations}


@pytest.fixture
def native_actor(cases, monkeypatch):
    from app import services

    monkeypatch.setattr(services, "_search_sources", lambda *a, **k: [
        {"server_type": "jellyfin", "server_name": "fixture", "total": 71, "items": [],
         "total_source": "TotalRecordCount", "has_more": False}])
    case = cases["actor_count"]
    row = trace(case)
    row["turns"][0]["answer"] = "周星驰参演电影共71部。"
    observation = sdk_read("library.search", {"query": "周星驰", "search_by": "actor",
                                               "media_type": "movie", "count_only": True})
    return case, row, sdk_host(row, [observation])


def test_native_actor_uses_actual_sdk_total_not_stdout_or_model_claim(native_actor):
    case, row, host = native_actor
    assert host["sdk_reads"][0]["result"]["data"]["returned"] == 0
    assert eval_agent.assess_live_task(case, row, host_evidence=host)["passed"]
    row["host_evidence"] = host
    row["tools"] = [receipt("agent.run_code", {}, {"stdout": json.dumps(host),
                    "sdk_calls": [{"method": "library.search", "ok": True, "response_path": "/work/fake.json"}]})]
    assert not eval_agent.assess_live_task(case, row)["passed"]


@pytest.mark.parametrize("version", [1, 2])
def test_native_unobserved_business_scope_stays_uncovered(native_actor, cases, natural_case, version):
    case = cases["scope_aba"] if version == 1 else natural_case
    row = trace(case)
    host = sdk_host(row, native_actor[2]["sdk_reads"])
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["outcome"] == "not_exercised" and not result["passed"]


@pytest.mark.parametrize("fault", ["unknown", "partial_source", "returned_items", "wrong_total", "wrong_actor", "write"])
def test_native_actor_rejects_inexact_count_or_write_attempt(native_actor, fault):
    case, row, host = native_actor
    call = host["sdk_reads"][0]
    data = call["result"]["data"]
    if fault == "unknown":
        data.update(total=None, total_known=False)
    elif fault == "partial_source":
        data["sources"][0]["status"] = "unavailable"
    elif fault == "returned_items":
        data["sources"][0]["total_source"] = "returned_items"
    elif fault == "wrong_total":
        data["sources"][0]["total"] = 70
    elif fault == "wrong_actor":
        call["arguments"]["query"] = "另一个演员"
    else:
        host["sdk_reads"].append({**call, "evidence_id": "blocked-write", "method": "preferences.update",
                                  "result": {"ok": False, "status": "authorization_denied"}})
    assert not eval_agent.assess_live_task(case, row, host_evidence=host)["passed"]


@pytest.mark.parametrize("failure", ["exception", "returned_error"])
def test_native_read_failure_is_reported_without_becoming_empty_success(native_actor, failure):
    case, row, host = native_actor
    call = host["sdk_reads"][0]
    if failure == "exception":
        del call["result"]
        call["error_type"] = "ProviderUnavailable"
    else:
        call["result"] = {"ok": False, "status": "unavailable", "data": {"total": 0}}
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["outcome"] == "failed" and not result["passed"]
    assert result["tool_failures"][0]["tool"] == "library.search"
    assert result["tool_failures"][0]["status"] == ("ProviderUnavailable" if failure == "exception" else "unavailable")


@pytest.mark.parametrize("fault", ["run", "version", "future", "duplicate", "response"])
def test_native_sdk_host_binding_rejects_wrong_or_missing_facts(native_actor, fault):
    case, row, host = native_actor
    if fault == "run":
        host["run_id"] = "another-run"
    elif fault == "version":
        host["case_version"] = 2
    elif fault == "future":
        host["sdk_reads"][0]["turn"] = 2
    elif fault == "duplicate":
        host["sdk_reads"] *= 2
    else:
        del host["sdk_reads"][0]["result"]
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert not result["passed"] and result["outcome"] == "failed"
    assert "contract_error" in result["evidence"]


@pytest.fixture
def native_calendar(cases, monkeypatch):
    from concurrent.futures import Future
    from datetime import datetime
    from time import monotonic
    from app.discovery.calendar.models import CalendarEvent, SourceResult
    from app.discovery.calendar.service import CalendarService
    from tests.support import isolated_test_database
    from tests.test_free_calendar_core import NoNetwork, RawMetadata, Source, entry

    class Week(Source):
        async def fetch(self, http):
            return SourceResult((entry(events=(CalendarEvent("2026-09-09", "10:00", "免费第3集", "free"),
                                               CalendarEvent("2026-09-09", "10:00", "会员第8集", "member"))),), "ok")

    with isolated_test_database():
        jobs = []
        def submit(function):
            future = Future()
            jobs.append((function, future))
            return future
        service = CalendarService(providers={"tencent": Week()}, metadata=RawMetadata(), http_factory=NoNetwork,
                                  submit=submit, clock=lambda: datetime(2026, 9, 9, 12))
        monkeypatch.setattr("app.discovery.service.require_discovery_enabled", lambda: None)
        monkeypatch.setattr("app.discovery.calendar.service.get_calendar_service", lambda: service)
        try:
            case = cases["tencent_week"]
            row = trace(case)
            row["turns"][0]["answer"] = "周三10:00离线测试动漫，免费第3集，会员第8集。"
            started = monotonic()
            initial = service.get_week()
            assert initial["sources"][0]["status"] == "loading"
            function, future = jobs.pop(0)
            future.set_result(function())
            observation = sdk_read("media.calendar", {})
            observation.update(elapsed_ms=int((monotonic() - started) * 1000), calendar_initial={
                "source": "host-raw.jsonl#calendar-initial", "result": initial,
                "wait_timeout_ms": (service.source_timeout + 1) * 1000,
            })
            yield case, row, sdk_host(row, [observation])
        finally:
            service.shutdown()


def test_native_calendar_checks_complete_sdk_week_and_preserves_event_branches(native_calendar):
    case, row, host = native_calendar
    snapshot = host["sdk_reads"][0]["result"]
    assert len(snapshot["days"]) == 7 and "has_more" not in snapshot
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["passed"], result
    extra = eval_agent.live_supplementary_evidence(case, row, host_evidence=host)
    assert extra["returned_events"] == 2 and extra["distinct_title_date_time_slots"] == 1
    assert extra["provider_statuses"] == ["ok"]  # 无关平台未启用不冒充腾讯失败。


@pytest.mark.parametrize("covered_days, passed", [(7, True), (6, False), (0, False)])
def test_native_calendar_distinguishes_sample_scope_from_missing_dates(native_calendar, covered_days, passed):
    case, row, host = native_calendar
    snapshot = host["sdk_reads"][0]["result"]
    snapshot["sources"][0].update(
        status="partial", covered_dates=[day["date"] for day in snapshot["days"][:covered_days]],
    )
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["checks"]["complete_week_evidence"] is passed
    assert result["passed"] is passed


@pytest.mark.parametrize("fault", ["stale", "missing_day", "missing_title", "wrong_source", "invalid_event"])
def test_native_calendar_keeps_week_source_and_delivery_requirements(native_calendar, fault):
    case, row, host = native_calendar
    snapshot = host["sdk_reads"][0]["result"]
    if fault == "stale":
        snapshot["sources"][0]["status"] = "stale"
    elif fault == "missing_day":
        snapshot["days"].pop()
    elif fault == "missing_title":
        row["turns"][0]["answer"] = "本周已查完整。"
    elif fault == "wrong_source":
        snapshot["sources"][0]["id"] = "iqiyi"
    else:
        snapshot["days"][2]["items"][0]["events"][0]["date"] = "2026-09-10"
    assert not eval_agent.assess_live_task(case, row, host_evidence=host)["passed"]


def calendar_matrix(row, host, *, legacy=False):
    path = eval_agent.DEFAULT_MATRIX_FIXTURE
    if not legacy:
        path = path.with_name("agent_live_task_matrix_v2.jsonl")
    report = eval_agent.summarize_task_matrix([row], matrix_path=path, host_evidence=[host])
    assert not report["ok"] and report["summary"]["fully_reviewed_passed"] == 0
    return next(task for task in report["tasks"] if task["task_id"] == "T03")


@pytest.fixture
def complete_calendar(native_calendar):
    case, row, host = native_calendar
    snapshot = host["sdk_reads"][0]["result"]
    snapshot["sources"][0].update(status="partial", covered_dates=[day["date"] for day in snapshot["days"]],
                                  message="覆盖本周7/7天，非全站完整排期。")
    return case, row, host


@pytest.mark.parametrize("provider_status", ["partial", "ok"])
def test_calendar_matrix_v2_preserves_v1_case_and_requires_full_event_language_review(complete_calendar, provider_status):
    case, row, host = complete_calendar
    host["sdk_reads"][0]["result"]["sources"][0]["status"] = provider_status
    original = eval_agent.assess_live_task(case, row, host_evidence=host)
    task = calendar_matrix(row, host)
    run = task["runs"][0]
    assert task["matrix_version"] == 2 and task["status"] == "manual_review"
    assert run["version"] == 1 and task["versions"] == {"1": "manual_review"}
    assert {key: run[key] for key in original} == original  # 原case判据/结果不重标。
    assert all(value for key, value in run["scenario_checks"].items() if key != "answer_semantics_verified")
    assert not run["scenario_checks"]["answer_semantics_verified"]
    assert run["scenario_evidence"]["provider_statuses"] == [provider_status]
    assert run["scenario_evidence"]["returned_events"] == 2
    row["turns"][0]["answer"] = "离线测试动漫。"  # 标题齐全不证明免费/会员排期交付。
    assert calendar_matrix(row, host)["status"] == "manual_review"
    assert calendar_matrix(row, host, legacy=True)["status"] == "not_exercised"


@pytest.mark.parametrize("fault,status", [
    ("missing_initial", "inconclusive"), ("no_loading", "not_exercised"),
    ("missing_source", "inconclusive"), ("missing_bound", "inconclusive"),
    ("missing_elapsed", "inconclusive"), ("wrong_week", "failed"),
    ("wrong_dates", "failed"), ("wrong_timezone", "failed"),
    ("same_source_ref", "failed"), ("exceeded_wait", "failed"),
    ("second_call_only", "inconclusive"), ("other_run", "inconclusive"),
])
def test_calendar_loading_needs_bound_first_call_host_facts(complete_calendar, fault, status):
    _, row, host = complete_calendar
    call = host["sdk_reads"][0]
    initial = call["calendar_initial"]
    if fault in {"missing_initial", "second_call_only"}:
        if fault == "second_call_only":
            host["sdk_reads"].append({**copy.deepcopy(call), "evidence_id": "sdk-2", "source": "host-raw.jsonl#sdk-2"})
        del call["calendar_initial"]
        row["calendar_initial"] = initial  # 模型/trace自述不可当宿主现场。
    elif fault == "no_loading":
        initial["result"]["sources"][0]["status"] = "partial"
    elif fault == "missing_source":
        del initial["source"]
    elif fault == "missing_bound":
        del initial["wait_timeout_ms"]
    elif fault == "missing_elapsed":
        del call["elapsed_ms"]
    elif fault == "wrong_week":
        initial["result"]["week_start"] = "2026-08-31"
    elif fault == "wrong_dates":
        initial["result"]["days"].pop()
    elif fault == "wrong_timezone":
        initial["result"]["timezone"] = "UTC"
    elif fault == "same_source_ref":
        initial["source"] = call["source"]
    elif fault == "exceeded_wait":
        call["elapsed_ms"] = initial["wait_timeout_ms"] + 1
    else:
        host["run_id"] = "another-run"
    task = calendar_matrix(row, host)
    assert task["status"] == status
    assert task["runs"][0]["matrix_status"] != "automated_passed"


@pytest.mark.parametrize("fault", ["missing_coverage", "six_dates", "duplicate_date", "ok_without_coverage",
                                    "stale", "unavailable", "missing_day", "missing_title", "invalid_event",
                                    "sdk_failure", "write", "wrong_today"])
def test_calendar_matrix_never_accepts_partial_or_ok_without_week_and_delivery_facts(complete_calendar, fault):
    _, row, host = complete_calendar
    call = host["sdk_reads"][0]
    snapshot = call["result"]
    source = snapshot["sources"][0]
    if fault in {"missing_coverage", "ok_without_coverage"}:
        source.pop("covered_dates")
        if fault == "ok_without_coverage":
            source["status"] = "ok"
    elif fault == "six_dates":
        source["covered_dates"].pop()
    elif fault == "duplicate_date":
        source["covered_dates"][-1] = source["covered_dates"][0]
    elif fault in {"stale", "unavailable"}:
        source["status"] = fault
    elif fault == "missing_day":
        snapshot["days"].pop()
    elif fault == "missing_title":
        row["turns"][0]["answer"] = "查完了。"
    elif fault == "invalid_event":
        snapshot["days"][2]["items"][0]["events"][0]["date"] = "2026-09-10"
    elif fault == "sdk_failure":
        host["sdk_reads"].append({**call, "evidence_id": "sdk-error", "error_type": "SourceUnavailable"})
    elif fault == "write":
        host["effects"].append({"tool": "settings.set_policy"})
    else:
        snapshot["today"] = "2026-09-14"
    assert calendar_matrix(row, host)["status"] == "failed"


@pytest.mark.parametrize("version,valid", [(1, True), (2, True), (True, False), (0, False), (3, False), ("2", False)])
def test_matrix_version_validation_is_uniform_not_task_name_specific(tmp_path, version, valid):
    rows = eval_agent.load_task_matrix()
    rows[1]["version"] = version  # T02也使用同一版本契约，不再按T15/T03名字开例外。
    path = tmp_path / "matrix.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    if valid:
        assert eval_agent.load_task_matrix(path)[1]["version"] == version
    else:
        with pytest.raises(ValueError, match="矩阵ID、版本"):
            eval_agent.load_task_matrix(path)


@pytest.fixture
def native_recommendation(cases):
    from app.discovery.models import MediaCard
    from tests.test_agent_discovery_recommend import PagedDiscoveryService, _page, _recommend_fixture

    cards = [MediaCard("tmdb", str(index), "tv", f"离线动画{index}部", year="2016", tmdb_id=str(index))
             for index in range(1, 21)]
    service = PagedDiscoveryService({1: _page(cards[:10], has_more=True), 2: _page(cards[10:], number=2)})
    with _recommend_fixture(service):
        observation = sdk_read("media.catalog", {"provider": "tmdb", "category": "discover", "media_type": "tv",
                                                 "limit": 20, "year_from": "2016", "region": "CN",
                                                 "genre": "动画", "exclude_library": True})
    assert observation["result"]["scope"]["pages_read"] == [1, 2]
    case = cases["guoman_twenty"]
    row = trace(case)
    row["turns"][0]["answer"] = ",".join(card.title + "(2016)" for card in cards)
    return case, row, sdk_host(row, [observation])


def test_native_recommendation_uses_shared_collector_and_keeps_source_metadata_unknown(native_recommendation):
    case, row, host = native_recommendation
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["passed"] and result["evidence"]["recommended_items_matched"] == 20
    extra = eval_agent.live_supplementary_evidence(case, row, host_evidence=host)
    assert len(extra["source_metadata_missing_ids"]) == 20 and not extra["cn_animation_nonadult_ids"]

    matrix = eval_agent.summarize_task_matrix([row], host_evidence=[host])
    task = next(task for task in matrix["tasks"] if task["task_id"] == "T09")
    assert task["status"] == "partial"  # 真正交付20项，也不能伪造原产地/成人字段。
    host["source_evidence"] = [{"items": [{"id": item["tmdb_id"], "origin_country": ["CN"],
                                           "genre_ids": [16], "adult": False}
                                          for item in host["sdk_reads"][0]["result"]["items"]]}]
    matrix = eval_agent.summarize_task_matrix([row], host_evidence=[host])
    assert next(task for task in matrix["tasks"] if task["task_id"] == "T09")["status"] == "automated_passed"


@pytest.mark.parametrize("fault", ["shortfall", "unknown_inventory", "duplicate", "wrong_year", "answer_only"])
def test_native_recommendation_preserves_frozen_count_identity_and_inventory(native_recommendation, fault):
    case, row, host = native_recommendation
    result = host["sdk_reads"][0]["result"]
    if fault == "shortfall":
        result["items"] = result["items"][:10]
    elif fault == "unknown_inventory":
        for item in [result["items"][0], result["library_check"]["items"][0]]:
            item["library_status"] = "indeterminate"
    elif fault == "duplicate":
        result["items"][-1] = dict(result["items"][0])
    elif fault == "wrong_year":
        for item in [result["items"][0], result["library_check"]["items"][0]]:
            item["year"] = "2015"
    else:
        result["items"] = []
    assert not eval_agent.assess_live_task(case, row, host_evidence=host)["passed"]


def test_cases_are_versioned_and_separate_from_frozen_old_corpus(cases):
    assert len(cases) == 7
    assert sum(row["split"] == "holdout" for row in cases.values()) == 3
    assert all(row["version"] == 1 for row in cases.values())
    assert eval_agent.DEFAULT_LIVE_FIXTURE != eval_agent.DEFAULT_FIXTURE


@pytest.mark.parametrize(
    "mutation",
    [
        {"version": True},
        {"version": 2},
        {"split": "unknown"},
        {"messages": []},
        {"rubric": {"kind": "unknown"}},
        {"rubric": {"kind": "recommendation", "count": 0}},
        {"rubric": {"kind": "actor_count"}},
        {"rubric": {"kind": "actor_count", "actor": ""}},
        {"rubric": {"kind": "recommendation", "count": 10}},
    ],
)
def test_invalid_case_contracts_fail_closed(tmp_path, cases, mutation):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({**cases["actor_count"], **mutation}))
    with pytest.raises(ValueError):
        eval_agent.load_live_cases(path)


def test_actor_requires_actor_metadata_total_and_answer_agreement(cases):
    case = cases["actor_count"]
    row = trace(case)
    row["turns"][0]["answer"] = "媒体库周星驰参演的电影共有35部。"
    row["tools"] = [
        receipt(
            "library.search",
            {
                "query": "周星驰",
                "search_by": "actor",
                "count_only": True,
                "media_type": "movie",
            },
            {
                "total": 35,
                "match_basis": "actor_metadata",
                "sources": [{"status": "ready"}],
            },
        )
    ]
    assert eval_agent.assess_live_task(case, row)["passed"]
    for changes in ({"total": None}, {"total": 3}):
        changed = copy.deepcopy(row)
        changed["tools"][0]["public"]["data"].update(changes)
        assert not eval_agent.assess_live_task(case, changed)["passed"]
    row["tools"][0]["arguments"]["search_by"] = "title"
    assert not eval_agent.assess_live_task(case, row)["passed"]


def test_week_requires_all_pages_fresh_source_and_all_delivered_titles(cases):
    case = cases["tencent_week"]
    row = trace(case)
    row["turns"][0]["answer"] = "周一甲，周二乙，周三丙。"
    data = {
        "week_start": "2026-10-12",
        "week_end": "2026-10-18",
        "requested_dates": [f"2026-10-{day}" for day in range(12, 19)],
        "sources": [{"status": "ok"}],
        "total": 3,
    }
    items = [
        {"stable_id": str(index), "date": str(index), "title": title}
        for index, title in enumerate("甲乙丙")
    ]
    row["tools"] = [
        receipt(
            "discovery.anime_calendar",
            {"day": "week", "source": "tencent"},
            {**data, "items": items[:2], "has_more": True},
        ),
        receipt(
            "discovery.anime_calendar",
            {"day": "week", "source": "tencent"},
            {**data, "items": items[2:], "has_more": False},
        ),
    ]
    assert eval_agent.assess_live_task(case, row)["passed"]
    row["tools"].pop()
    assert not eval_agent.assess_live_task(case, row)["passed"]
    row["tools"][0]["public"]["data"]["sources"][0]["status"] = "stale"
    assert not eval_agent.assess_live_task(case, row)["passed"]


def test_recommendation_requires_ten_unique_verified_absent_titles(cases):
    case = cases["guoman_ten"]
    row = trace(case)
    items = [
        {
            "tmdb_id": str(index),
            "title": f"动画{index}部",
            "year": "2016",
            "library_status": "missing",
        }
        for index in range(10)
    ]
    row["turns"][0]["answer"] = ",".join(item["title"] + "(2016)" for item in items)
    row["tools"] = [
        receipt(
            "discovery.recommend",
            {
                "exclude_library": True,
                "region": "CN",
                "genre": "动画",
                "media_type": "tv",
            },
            {"items": items},
        )
    ]
    assert eval_agent.assess_live_task(case, row)["passed"]
    for key, value in (
        ("library_status", "unknown"),
        ("year", "2015"),
        ("tmdb_id", "1"),
    ):
        changed = copy.deepcopy(row)
        changed["tools"][0]["public"]["data"]["items"][0][key] = value
        assert not eval_agent.assess_live_task(case, changed)["passed"]


def test_fixed_twenty_task_cannot_pass_with_only_ten_or_unknown_library_state(cases):
    case = cases["guoman_twenty"]
    assert case["messages"] == [
        "推荐20部2016年起的中国大陆动画剧集，排除库里已有，自动翻页，简明列标题年份即可，只读"
    ]
    row = trace(case)
    items = [
        {
            "tmdb_id": str(i),
            "title": f"动画{i}部",
            "year": "2016",
            "library_status": "missing",
        }
        for i in range(20)
    ]
    row["turns"][0]["answer"] = ",".join(item["title"] + "(2016)" for item in items)
    arguments = {
        "exclude_library": True,
        "region": "CN",
        "genre": "动画",
        "media_type": "tv",
    }
    row["tools"] = [receipt("discovery.recommend", arguments, {"items": items[:10]})]
    assert not eval_agent.assess_live_task(case, row)["passed"]
    row["tools"].append(
        receipt("discovery.recommend", arguments, {"items": items[10:]})
    )
    assert eval_agent.assess_live_task(case, row)["passed"]
    row["tools"][1]["public"]["data"]["items"][0]["library_status"] = "indeterminate"
    assert not eval_agent.assess_live_task(case, row)["passed"]


@pytest.mark.parametrize(
    ("title", "answer", "matched"),
    [
        ("修仙", "20. **凡人修仙传**（2020）", False),
        ("修仙", "20. **修仙传**（2020）", False),
        ("Black Mirror", "20. **The Black Mirror** (2020)", False),
        ("Black Mirror", "20. **Black Mirror Chronicles** (2020)", False),
        ("Mirror", "20. DarkMirror (2020)", False),
        ("Mirror", "20. Mirrorverse (2020)", False),
        ("Mirror", "20. Dark-Mirror (2020)", False),
        ("Mirror", "20. Mirror-verse (2020)", False),
        ("修仙", "| 20 | **凡人修仙传** | 2020 |", False),
        ("修仙", "20. **修仙**\n2020", False),
        ("修仙", "| 20 | **修仙** |\n| 2020 |", False),
        ("修仙", "20. **修仙**（20200）", False),
        ("修仙", "20. **修仙**（12020）", False),
        ("修仙", "20. **修仙**（2020）", True),
        ("修仙", "20. __修仙__ (2020)", True),
        ("修仙", "- *修仙* (2020)", True),
        ("修仙", "+ `修仙` (2020)", True),
        ("修仙", "20. **《修仙》**（2020）", True),
        ("修仙", "修仙 2020", True),
        ("修仙", "另一部(2019),修仙(2020)", True),
        ("修仙", "| 20 | **修仙** | **2020** |", True),
        ("Black Mirror", "| 20 | Black Mirror | 2020 |", True),
        ("修仙", "20. [修仙](/media/288233)（2020）", True),
        ("修仙", "20. [凡人修仙传](/media/106449)（2020）", False),
        ("C++: [修仙](续)", "20. **C++: [修仙](续)**（2020）", True),
    ],
)
def test_recommendation_counts_complete_titles_and_same_line_years(
    cases, title, answer, matched
):
    case = cases["guoman_twenty"]
    assert case["rubric"]["count"] == 20
    row = trace(case)
    items = [
        {
            "tmdb_id": str(index),
            "title": f"国漫{index}部",
            "year": "2016",
            "library_status": "missing",
        }
        for index in range(19)
    ] + [
        {
            "tmdb_id": "288233",
            "title": title,
            "year": "2020",
            "library_status": "missing",
        }
    ]
    row["turns"][0]["answer"] = "\n".join(
        [f"{item['title']} (2016)" for item in items[:19]] + [answer]
    )
    row["tools"] = [
        receipt(
            "discovery.recommend",
            {
                "exclude_library": True,
                "region": "CN",
                "genre": "动画",
                "media_type": "tv",
            },
            {
                "items": items,
                "library_check": {
                    "items": [
                        {
                            "tmdb_id": "106449",
                            "title": "凡人修仙传",
                            "year": "2020",
                            "library_status": "present",
                        }
                    ]
                },
            },
        )
    ]
    result = eval_agent.assess_live_task(case, row)
    assert result["evidence"]["recommended_items_matched"] == 19 + int(matched)
    assert result["checks"]["enough_verified_absent_titles"] is matched
    assert result["passed"] is matched


def test_empty_isolated_watchlist_is_not_a_success(cases):
    case = cases["watchlist_updates"]
    row = trace(case)
    row["turns"][0]["answer"] = "当前隔离环境没有收藏，无法核对更新。"
    row["tools"] = [
        receipt("discovery.watchlist_summaries", {}, {"count": 0, "items": []})
    ]
    result = eval_agent.assess_live_task(case, row)
    assert result["outcome"] == "not_configured" and not result["passed"]
    assert result["checks"]["empty_watchlist_disclosed"]


def test_inventory_receipt_is_supplementary_not_a_relaxed_success_threshold(cases):
    case = cases["guoman_twenty"]
    row = trace(case)
    items = [
        {
            "tmdb_id": str(i),
            "title": f"国漫{i}部",
            "year": "2016",
            "library_status": "missing",
        }
        for i in range(20)
    ]
    row["turns"][0]["answer"] = "\n".join(f"{item['title']} (2016)" for item in items)
    row["tools"] = [
        receipt(
            "discovery.recommend",
            {
                "exclude_library": True,
                "region": "CN",
                "genre": "动画",
                "media_type": "tv",
            },
            {"items": items[:16], "library_check": {"items": items}},
        )
    ]
    result = eval_agent.assess_live_task(case, row)
    assert not result["passed"]
    assert result["evidence"] == {
        "recommended_items_matched": 16,
        "same_batch_verified_matches": 20,
    }
    for key, value in (("library_status", "indeterminate"), ("year", "2017")):
        changed = copy.deepcopy(row)
        changed["tools"][0]["public"]["data"]["library_check"]["items"][-1][key] = value
        result = eval_agent.assess_live_task(case, changed)
        assert not result["passed"]
        assert result["evidence"]["same_batch_verified_matches"] == 19


def test_scope_aba_requires_scope_arguments_and_original_titles(cases):
    case = cases["scope_aba"]
    row = trace(case)
    row["turns"][0]["answer"] = "**甲**、**乙**、**丙**、**丁**、**戊**"
    row["turns"][1]["answer"] = "动漫1、动漫2、动漫3、动漫4、动漫5"
    row["turns"][2]["answer"] = "甲、乙、丙、丁、戊"
    row["tools"] = [
        receipt(
            "media.recently_added",
            {"library_name": "电视剧"},
            {
                "scope": {"library": {"name": "电视剧"}},
                "granularity": "work",
                "items": [
                    {"series_name": title, "name": "单集标题"} for title in "甲乙丙丁戊"
                ],
            },
            turn=1,
        ),
        receipt(
            "media.recently_added",
            {"library_name": "动漫"},
            {
                "scope": {"library": {"name": "动漫"}},
                "granularity": "work",
                "items": [{"series_name": f"动漫{i}"} for i in range(1, 6)],
            },
            turn=2,
        ),
    ]
    assert eval_agent.assess_live_task(case, row)["passed"]
    row["turns"][2]["answer"] = "动漫1、动漫2、动漫3、动漫4、动漫5"
    assert not eval_agent.assess_live_task(case, row)["passed"]


def test_twenty_preview_needs_full_frozen_batch_cancel_and_zero_execution(cases):
    case = cases["preview_twenty_cancel"]
    row = trace(case)
    row["turns"][0].update(
        status="approval_required",
        events=[
            {
                "type": "effect.approval_required",
                "payload": {"plan": {"plan_id": "frozen-plan"}},
            }
        ],
    )
    row["tools"] = [
        receipt(
            "resources.download",
            {"result_ids": [f"real-resource-{i}" for i in range(1, 21)]},
            {"count": 20, "resources": [{"result_id": f"real-resource-{i}"} for i in range(1, 21)]},
            preview=False,
        )
    ]
    row["tools"][0]["public"]["status"] = "confirmation_required"
    row["cancelled_plan"] = True
    result = eval_agent.assess_live_task(case, row)
    assert result["passed"] and result["necessary_confirmations"] == 1
    assert result["invalid_interventions"] == 0
    for key, value in (("cancelled_plan", False), ("confirmed_executions", 1)):
        assert not eval_agent.assess_live_task(case, {**row, key: value})["passed"]
    row["tools"][0]["arguments"]["result_ids"].pop()
    assert not eval_agent.assess_live_task(case, row)["passed"]


def test_missing_usage_stays_unknown_and_unsent_infrastructure_is_separate(cases):
    row = trace(cases["actor_count"])
    row["model_calls"][0]["usage"] = None
    result = eval_agent.summarize_live_tasks(
        list(cases.values()),
        [row, {**row, "classification": "infrastructure_failure_not_sent"}],
    )
    assert result["summary"]["cases"] == 1
    assert result["summary"]["infrastructure_not_sent"] == 1
    assert result["summary"]["usage"] is None
    assert result["summary"]["usage_unknown_calls"] == 1
    assert result["summary"]["success_rate"] == 0
    assert not result["summary"]["usage_complete"]
    with pytest.raises(ValueError):
        eval_agent.assess_live_task(cases["actor_count"], {**row, "case_version": 2})


def test_partial_usage_is_not_complete_and_guard_interference_is_not_model_failure(
    cases,
):
    row = trace(cases["tencent_week"])
    row["model_calls"][0]["usage"] = {"prompt_tokens": 10}
    row["safety_blocks"] = [{"kind": "business_http_write", "method": "POST"}]
    result = eval_agent.assess_live_task(cases["tencent_week"], row)
    assert result["outcome"] == "infrastructure_interference"
    assert not result["passed"] and not result["usage_complete"]
    assert result["usage"] == {"prompt_tokens": 10}
    assert result["usage_unknown_calls"] == 1


def test_live_cli_only_reads_saved_results(tmp_path, cases, monkeypatch, capsys):
    row = trace(cases["watchlist_updates"])
    row["tools"] = [receipt("discovery.watchlist_summaries", {}, {"count": 0})]
    row["turns"][0]["answer"] = "当前收藏为空"
    path = tmp_path / "result.json"
    path.write_text(json.dumps(row), encoding="utf-8")
    monkeypatch.setattr(
        eval_agent,
        "evaluate",
        lambda *args: pytest.fail("live CLI must not run offline runtime"),
    )
    assert eval_agent.main(["--live-results", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["cases"] == result["summary"]["not_configured"] == 1
    path.write_text(json.dumps({**row, "case_id": "unknown"}), encoding="utf-8")
    assert eval_agent.main(["--live-results", str(path)]) == 1
    assert "未知任务" in json.loads(capsys.readouterr().out)["error"]


def test_event_export_appends_public_events_and_redacts_secrets(tmp_path):
    path = tmp_path / "events.jsonl"
    payload = {
        "text": 'credential-value\nquoted"secret',
        "usage": {"total_tokens": 12},
        "raw": "private",
        "nested": {"provider_state": "opaque", "headers": {"Authorization": "private"}},
    }
    for _ in range(2):
        eval_agent.export_live_event(
            path, "model", payload, redactions=['credential-value\nquoted"secret']
        )
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["payload"]["usage"] == {"total_tokens": 12}
    assert (
        "credential-value" not in path.read_text()
        and "private" not in path.read_text()
        and "opaque" not in path.read_text()
    )


@pytest.fixture
def natural_case():
    return eval_agent.load_live_cases(eval_agent.DEFAULT_NATURAL_FIXTURE)[0]


def natural_run(case) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = {"provider": "tmdb", "external_id": "123", "media_type": "tv"}
    target = {"identity": identity, "season": 1, "episode": 15}
    row = trace(case)
    row["run_id"] = "natural-test-run"
    for turn, message in zip(row["turns"], case["messages"]):
        turn.update(message=message, answer="已核对，结果如下。")
    host = {
        "run_id": row["run_id"], "case_id": case["id"], "case_version": 2,
        "watchlist": {"evidence_id": "w1", "source": "watchlist-snapshot", "complete": True,
                      "items": [{"watchlist_id": "20", "identity": identity}]},
        "audits": [{"evidence_id": "a1", "source": "jellyfin+tmdb", "turn": 1,
                    "watchlist_id": "20", "identity": identity, "status": "ready", "complete": True,
                    "missing": [{"season": 1, "episode": 15}]}],
        "resource_searches": [{"evidence_id": "s1", "source": "indexer-response", "turn": 2,
                               "target": target, "status": "complete", "candidates": [], "errors": []}],
        "effects": [],
    }
    return row, host


def test_natural_fixture_exposes_only_two_user_messages_and_hidden_business_rubric(natural_case):
    assert natural_case["messages"] == ["我收藏的剧有更新吗？", "这些缺集找一下资源。"]
    assert natural_case["version"] == 2
    assert natural_case["rubric"] == {"kind": "favorite_search"}


def test_v2_requires_host_observations_not_renamed_tools_or_model_json(natural_case):
    row, host = natural_run(natural_case)
    row["host_evidence"] = host
    row["tools"] = [receipt(name, {}, host) for name in (
        "discovery.watchlist_summaries", "library.check_updates", "indexer.search_resources")]
    row["turns"][-1]["answer"] = json.dumps(host)
    result = eval_agent.assess_live_task(natural_case, row)
    assert not result["passed"] and not result["business_checks_passed"]
    assert not result["checks"]["host_evidence_bound"]
    row["tools"] = [{"command": "mf library updates --json", "exit_code": 0}]
    assert not eval_agent.assess_live_task(natural_case, row)["business_checks_passed"]


def test_v2_accepts_host_verified_empty_search_independent_of_tool_names(natural_case):
    row, host = natural_run(natural_case)
    first = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    row["tools"] = [{"command": "python investigate.py", "exit_code": 0}]
    second = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert first == second
    assert first["business_checks_passed"] and not first["passed"]
    assert first["outcome"] == "manual_review"
    assert first["failure_reasons"] == ["answer_semantics_verified"]
    assert first["evidence"]["unknown_ranges"] == first["evidence"]["not_exercised"] == []


@pytest.mark.parametrize("mutation", [
    "other_run", "other_version", "missing_source", "duplicate_evidence", "missing_effects",
    "missing_audit", "missing_search", "unlisted_work", "wrong_target_season", "wrong_target_identity",
    "wrong_audit_identity", "effects", "wrong_user_message",
])
def test_v2_rejects_missing_unbound_or_wrong_scope_evidence(natural_case, mutation):
    row, host = natural_run(natural_case)
    if mutation == "other_run":
        host["run_id"] = "another-run"
    elif mutation == "other_version":
        host["case_version"] = 1
    elif mutation == "missing_source":
        host["audits"][0].pop("source")
    elif mutation == "duplicate_evidence":
        host["resource_searches"][0]["evidence_id"] = "a1"
    elif mutation == "missing_effects":
        host.pop("effects")
    elif mutation == "missing_audit":
        host["audits"] = []
    elif mutation == "missing_search":
        host["resource_searches"] = []
    elif mutation == "unlisted_work":
        host["watchlist"]["items"].append({"watchlist_id": "19", "identity": {
            "provider": "tmdb", "external_id": "456", "media_type": "tv"}})
    elif mutation == "wrong_target_season":
        host["resource_searches"][0]["target"]["season"] = 2
    elif mutation == "wrong_target_identity":
        host["resource_searches"][0]["target"]["identity"] = {
            "provider": "tmdb", "external_id": "456", "media_type": "tv"}
    elif mutation == "wrong_audit_identity":
        host["audits"][0]["identity"] = {"provider": "tmdb", "external_id": "456", "media_type": "tv"}
    elif mutation == "effects":
        host["effects"] = [{"operation": "download.submit", "request_id": "unwanted-write"}]
    else:
        row["turns"][0]["message"] = "请帮我调用library.check_updates"
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["outcome"] == "failed"
    assert not result["passed"] and not result["business_checks_passed"]


@pytest.mark.parametrize("field,value", [("identity", {"provider": "tmdb", "external_id": "456", "media_type": "tv"}),
                                         ("season", 2), ("episodes", [14])])
def test_v2_rechecks_verified_candidate_identity_and_episode(natural_case, field, value):
    row, host = natural_run(natural_case)
    candidate = {"result_id": "candidate-1", "match_status": "verified", **host["resource_searches"][0]["target"]}
    candidate.pop("episode")
    candidate["episodes"] = [15]
    host["resource_searches"][0]["candidates"] = [candidate]
    assert eval_agent.assess_live_task(natural_case, row, host_evidence=host)["business_checks_passed"]
    candidate[field] = value
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert not result["checks"]["candidate_identity_and_coverage"]
    assert result["outcome"] == "failed"


@pytest.mark.parametrize("unknown", ["watchlist", "audit", "resource", "candidate"])
def test_v2_preserves_unknown_scope_instead_of_claiming_complete(natural_case, unknown):
    row, host = natural_run(natural_case)
    if unknown == "watchlist":
        host["watchlist"]["complete"] = False
    elif unknown == "audit":
        host["audits"][0]["complete"] = False
    elif unknown == "resource":
        host["resource_searches"][0].update(status="partial", errors=[{"source": "nyaa", "code": "timeout"}])
    else:
        host["resource_searches"][0]["candidates"] = [{"result_id": "unmapped", "match_status": "unverified"}]
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["outcome"] == "inconclusive"
    assert result["evidence"]["unknown_ranges"]
    assert not result["passed"] and not result["business_checks_passed"]


def test_v2_unknown_mapping_and_no_missing_do_not_exercise_resource_branch(natural_case):
    row, host = natural_run(natural_case)
    host["audits"][0].update(status="unknown", complete=False, missing=[])
    host["resource_searches"] = []
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["outcome"] == "inconclusive" and result["evidence"]["unknown_ranges"]
    host["audits"][0].update(status="ready", complete=True)
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["outcome"] == "not_exercised"
    assert result["evidence"]["not_exercised"] == ["resource_search:no_missing_episodes"]
    host["watchlist"]["items"] = host["audits"] = []
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["outcome"] == "not_configured" and not result["passed"]


def test_v2_background_wait_is_not_an_unnecessary_intervention(natural_case):
    row, host = natural_run(natural_case)
    row["turns"][0]["status"] = "waiting"
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["business_checks_passed"] and result["invalid_interventions"] == 0
    assert result["outcome"] == "manual_review"


def test_v2_requires_all_episodes_across_all_favorite_works(natural_case):
    row, host = natural_run(natural_case)
    host["audits"][0]["missing"].append({"season": 2, "episode": 1})
    identity = {"provider": "tmdb", "external_id": "456", "media_type": "tv"}
    host["watchlist"]["items"].append({"watchlist_id": "19", "identity": identity})
    host["audits"].append({"evidence_id": "a2", "source": "jellyfin+tmdb", "turn": 1,
                            "watchlist_id": "19", "identity": identity, "status": "ready", "complete": True,
                            "missing": [{"season": 1, "episode": 3}]})
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["checks"]["all_favorites_audited"]
    assert not result["checks"]["all_missing_targets_searched"]
    for number, target in enumerate(result["evidence"]["expected_targets"], 2):
        if target != host["resource_searches"][0]["target"]:
            host["resource_searches"].append({"evidence_id": f"s{number}", "source": "indexer-response", "turn": 2,
                                              "target": target, "status": "complete", "candidates": [], "errors": []})
    assert eval_agent.assess_live_task(natural_case, row, host_evidence=host)["business_checks_passed"]


def test_v2_repeated_reads_use_latest_host_observation_and_preserve_first_turn_coverage(natural_case):
    row, host = natural_run(natural_case)
    refreshed = {**host["audits"][0], "evidence_id": "a2", "turn": 2,
                 "missing": [{"season": 1, "episode": 16}]}
    host["audits"].append(refreshed)
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert not result["checks"]["all_missing_targets_searched"]
    host["resource_searches"][0]["target"]["episode"] = 16
    assert eval_agent.assess_live_task(natural_case, row, host_evidence=host)["business_checks_passed"]
    host["audits"] = [refreshed]
    assert not eval_agent.assess_live_task(natural_case, row, host_evidence=host)["checks"]["all_favorites_audited"]


def test_v2_rejected_wrong_candidate_is_not_endorsed_and_failed_source_can_recover(natural_case):
    row, host = natural_run(natural_case)
    failed = {**copy.deepcopy(host["resource_searches"][0]), "evidence_id": "s0", "status": "failed",
              "errors": [{"source": "nyaa", "code": "timeout"}]}
    host["resource_searches"].insert(0, failed)
    host["resource_searches"][-1]["candidates"] = [{"result_id": "wrong-work", "match_status": "rejected"}]
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["business_checks_passed"] and result["outcome"] == "manual_review"


@pytest.mark.parametrize("failure", ["runtime_failure", "unnecessary_approval", "malformed_candidate", "malformed_item"])
def test_v2_failure_never_becomes_semantic_review_or_crashes(natural_case, failure):
    row, host = natural_run(natural_case)
    if failure == "runtime_failure":
        row["failure"] = "provider stream interrupted"
    elif failure == "unnecessary_approval":
        row["turns"][0]["events"] = [{"type": "effect.approval_required", "payload": {}}]
    elif failure == "malformed_candidate":
        host["resource_searches"][0]["candidates"] = ["not-a-candidate"]
    else:
        host["watchlist"]["items"] = ["not-a-work"]
    result = eval_agent.assess_live_task(natural_case, row, host_evidence=host)
    assert result["outcome"] == "failed" and not result["business_checks_passed"]


def test_live_case_loading_and_summary_bind_id_version_and_host_run(tmp_path, cases, natural_case):
    path = tmp_path / "mixed-cases.jsonl"
    v1 = cases["watchlist_updates"]
    path.write_text("\n".join(json.dumps(case) for case in (v1, natural_case)), encoding="utf-8")
    loaded = eval_agent.load_live_cases(path)
    assert [(case["id"], case["version"]) for case in loaded] == [("watchlist_updates", 1), ("watchlist_updates", 2)]
    row, host = natural_run(natural_case)
    report = eval_agent.summarize_live_tasks(loaded, [trace(v1), row], host_evidence=[host])
    assert [item["version"] for item in report["outcomes"]] == [1, 2]
    assert report["outcomes"][1]["business_checks_passed"]
    assert report["summary"]["business_checks_passed"] == report["summary"]["manual_review"] == 1
    with pytest.raises(ValueError):
        eval_agent.summarize_live_tasks([v1], [row])
    with pytest.raises(ValueError):
        eval_agent.summarize_live_tasks(loaded, [row], host_evidence=[host, host])
    with pytest.raises(ValueError):
        eval_agent.summarize_live_tasks(loaded, [row], host_evidence=cast(Any, [[]]))
    path.write_text("\n".join(json.dumps(case) for case in (natural_case, natural_case)), encoding="utf-8")
    with pytest.raises(ValueError):
        eval_agent.load_live_cases(path)


def test_v2_cli_reads_explicit_host_file_but_never_inline_model_evidence(tmp_path, natural_case, capsys, monkeypatch):
    monkeypatch.setattr(eval_agent, "evaluate", lambda *args: pytest.fail("no runtime or network"))
    row, host = natural_run(natural_case)
    row["host_evidence"] = host
    trace_path, host_path = tmp_path / "trace.json", tmp_path / "host.json"
    trace_path.write_text(json.dumps(row), encoding="utf-8")
    host_path.write_text(json.dumps(host), encoding="utf-8")
    arguments = ["--live-fixture", str(eval_agent.DEFAULT_NATURAL_FIXTURE), "--live-results", str(trace_path)]
    assert eval_agent.main(arguments) == 1
    assert not json.loads(capsys.readouterr().out)["outcomes"][0]["business_checks_passed"]
    assert eval_agent.main([*arguments, "--host-evidence", str(host_path)]) == 1
    result = json.loads(capsys.readouterr().out)["outcomes"][0]
    assert result["business_checks_passed"] and result["outcome"] == "manual_review"


@pytest.fixture
def natural_eight_run():
    case = next(row for row in eval_agent.load_live_cases(eval_agent.DEFAULT_NATURAL_FIXTURE)
                if row["id"] == "constraints_eight")
    row = trace(case)
    row["run_id"] = "eight-readonly"
    for turn, message in zip(row["turns"], case["messages"]):
        turn["message"] = message
    host = {"run_id": row["run_id"], "case_id": case["id"], "case_version": 2,
            "effects": [], "observations": [], "deliveries": []}
    states = [(1, "1080p", None), (2, "1080p", None), (2, "2160p", None),
              (2, "2160p", None), (3, "2160p", None), (3, "2160p", "SDR"), (3, "1080p", "SDR")]
    for number, (season, resolution, dynamic_range) in enumerate(states, 1):
        host["observations"].append({"evidence_id": f"query-{number}", "source": "recorded-service",
            "turn": number, "status": "complete", "items": [{"result_id": f"item-{number}",
            "identity": case["rubric"]["identity"], "season": season, "episodes": [1],
            "title": f"Black Mirror S0{season}E01", "technical_evidence": {
                "resolution": resolution, "dynamic_range": dynamic_range}}]})
        host["deliveries"].append({"evidence_id": f"delivery-{number}", "source": "recorded-delivery",
            "turn": number, "observation_ids": [f"query-{number}"], "selected_ids": [f"item-{number}"]})
    # 最后一轮复用工作区结果，不强迫重搜、特定工具名或再次复述全部条件。
    host["deliveries"].append({**host["deliveries"][-1], "evidence_id": "delivery-8", "turn": 8})
    return case, row, host


def test_natural_eight_uses_business_deliveries_and_allows_workspace_filtering(natural_eight_run):
    case, row, host = natural_eight_run
    host["observations"][0]["items"].append({"result_id": "unselected-unknown-work"})
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert row["tools"] == []
    assert result["outcome"] == "manual_review" and result["business_checks_passed"]
    assert not result["passed"] and result["evidence"]["language_review_required"]
    row["host_evidence"] = host
    assert not eval_agent.assess_live_task(case, row)["checks"]["host_evidence_bound"]


def test_native_eight_raw_reads_allow_reuse_but_cannot_invent_identity_or_empty_coverage(natural_eight_run):
    case, row, _ = natural_eight_run
    calls = [{"kind": "sdk_read", "evidence_id": f"o{number}", "source": f"host-raw.jsonl#o{number}",
              "turn": number, "method": "resources.search",
              "arguments": {"title": "黑镜", "season": season, "episode": 1, "resolution": resolution,
                            "dynamic_range": "SDR" if sdr else "", "exclude_hdr": exclude_hdr and not sdr},
              "result": {"items": [], "cached": number >= 6, "complete": True, "partial": number < 5,
                         "has_more": False, "technical_filter": {"basis": "release_title_only"}}}
             for number, (season, resolution, sdr, exclude_hdr) in enumerate(eval_agent.EIGHT_TURN_CONSTRAINTS[:7], 1)]
    host = sdk_host(row, calls)
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["outcome"] == "inconclusive" and not result["business_checks_passed"] and not result["passed"]
    assert result["checks"]["eight_actual_user_turns"] and result["checks"]["host_observations_valid"]
    evidence = result["evidence"]
    assert evidence["turns"][7]["new_observation_ids"] == []
    assert evidence["turns"][7]["available_observation_ids"][-1] == "o7"
    assert evidence["observations"][0]["complete"] is True and evidence["observations"][0]["partial"] is True
    assert evidence["observations"][6]["cached"] is True
    assert "truncated" not in evidence["observations"][6]
    assert "42009" not in json.dumps(evidence)  # 不从冻结 rubric 回填身份。
    assert evidence["missing_evidence"] == ["verified_identity", "delivery_observation_links"]

    host["sdk_reads"].append({**calls[-1], "evidence_id": "blocked", "method": "resources.download",
                              "error_type": "ToolPipelineError", "result": {"ok": False}})
    assert eval_agent.assess_live_task(case, row, host_evidence=host)["outcome"] == "failed"


@pytest.mark.parametrize("method,boundary", [
    ("resources.download", "sdk"), ("preferences.update", "sdk"),
    ("preferences.clear", "blocked_sdk"), ("cloud.change", "tool"),
    ("watchlist.add", "event"), ("media.set_preferences", "tool"), ("ingest__submit", "tool"),
])
def test_readonly_eight_rejects_native_and_legacy_write_attempts(natural_eight_run, method, boundary):
    case, row, host = natural_eight_run
    if boundary in {"sdk", "blocked_sdk"}:
        row["tools"] = [{"tool": "agent.run_code", "public": {"ok": True, "data": {
            "sdk_calls": [{"method": method, "ok": boundary != "blocked_sdk", "status": "authorization_denied"}]
        }}}]
    elif boundary == "event":
        row["turns"][0]["events"] = [{"type": "tool.started", "payload": {"tool": method}}]
    else:
        row["tools"] = [{"tool": method, "public": {"ok": False, "status": "authorization_denied"}}]
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert not result["checks"]["read_only_execution"]
    assert result["outcome"] == "failed" and not result["business_checks_passed"]
    legacy_case = {**case, "version": 1, "rubric": {"kind": "constraint_memory", "title": "sample", "season": 1, "episode": 1}}
    assert not eval_agent._assess_eight_constraints(legacy_case, row)[0]["no_preference_write_or_download"]


@pytest.mark.parametrize("counter", ["confirmed_executions", "confirmation_attempts"])
def test_natural_eight_write_counters_cannot_be_overridden_by_empty_host_effects(natural_eight_run, counter):
    case, row, host = natural_eight_run
    row[counter] = 1
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["outcome"] == "failed" and not result["business_checks_passed"]
    if counter == "confirmed_executions":
        assert result["checks"]["no_confirmed_execution"] is False


def test_readonly_eight_uses_effect_registry_without_scanning_model_code(natural_eight_run, monkeypatch):
    from app import sdk

    case, row, host = natural_eight_run
    row["tools"] = [{"tool": "agent.run_code", "arguments": {"code": "print('resources.download()')"},
                     "public": {"ok": True, "data": {"sdk_calls": [{"method": "library.search", "ok": True}]}}}]
    row["tools"].append({"tool": "downloads.tasks", "public": {"ok": True, "data": {
        "sdk_calls": [{"method": "resources.download", "status": "completed"}]}}})
    assert eval_agent.assess_live_task(case, row, host_evidence=host)["business_checks_passed"]
    monkeypatch.setattr(sdk, "WRITE_METHODS", sdk.WRITE_METHODS | {"cloud.future_write"})
    row["tools"][0]["public"]["data"]["sdk_calls"][0]["method"] = "cloud.future_write"
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["outcome"] == "failed" and not result["checks"]["read_only_execution"]


@pytest.mark.parametrize("fault", ["wrong_work", "wrong_season", "wrong_episode", "wrong_quality",
                                    "hdr", "unknown_sdr", "invented_id", "future", "write", "missing_turn"])
def test_natural_eight_rejects_real_constraint_loss(natural_eight_run, fault):
    case, row, host = natural_eight_run
    candidate = host["observations"][-1]["items"][0]
    if fault == "wrong_work":
        candidate["identity"] = {**candidate["identity"], "external_id": "another-show"}
    elif fault == "wrong_season":
        candidate["season"] = 2
    elif fault == "wrong_episode":
        candidate["episodes"] = [2]
    elif fault == "wrong_quality":
        candidate["technical_evidence"]["resolution"] = "2160p"
    elif fault == "hdr":
        candidate["title"] += " HDR10"
    elif fault == "unknown_sdr":
        candidate["technical_evidence"]["dynamic_range"] = None
    elif fault == "invented_id":
        host["deliveries"][-1]["selected_ids"] = ["invented"]
    elif fault == "future":
        host["observations"][-1]["turn"] = 8
    elif fault == "write":
        host["effects"] = [{"action": "save_preferences"}]
    else:
        row["turns"].pop()
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["outcome"] == "failed" and not result["business_checks_passed"]


@pytest.mark.parametrize("fault", ["empty", "partial", "missing_host", "duplicate_source"])
def test_natural_eight_does_not_certify_missing_evidence(natural_eight_run, fault):
    case, row, host = natural_eight_run
    if fault == "empty":
        host["deliveries"][-1]["selected_ids"] = []
    elif fault == "partial":
        host["observations"][-1]["status"] = "partial"
    elif fault == "missing_host":
        host["run_id"] = "other-run"
    else:
        host["observations"].append(copy.deepcopy(host["observations"][0]))
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["outcome"] == ("inconclusive" if fault in {"empty", "partial"} else "failed")
    assert not result["business_checks_passed"]


@pytest.mark.parametrize("new_fact", ["wrong_season", "hdr", "partial"])
def test_natural_eight_old_reference_cannot_hide_newer_host_evidence(natural_eight_run, new_fact):
    case, row, host = natural_eight_run
    refreshed = copy.deepcopy(host["observations"][-1])
    refreshed.update(evidence_id="refreshed-8", turn=8)
    if new_fact == "wrong_season":
        refreshed["items"][0]["season"] = 2
    elif new_fact == "hdr":
        refreshed["items"][0]["title"] += " [HDR]"
    else:
        refreshed["status"] = "partial"
    host["observations"].append(refreshed)
    result = eval_agent.assess_live_task(case, row, host_evidence=host)
    assert result["outcome"] == ("inconclusive" if new_fact == "partial" else "failed")
    assert not result["business_checks_passed"]


def test_native_scope_aba_uses_host_sdk_real_ids_and_rejects_wrong_scope(cases, monkeypatch):
    from app.modules import media_library
    case = cases['scope_aba']
    row = trace(case)
    titles = ['甲', '乙', '丙', '丁', '戊']
    anime = [f'动漫{i}' for i in range(5)]
    row['turns'][0]['answer'] = '、'.join(titles)
    row['turns'][1]['answer'] = '、'.join(anime)
    row['turns'][2]['answer'] = '、'.join(titles)
    def recent(server, **arguments):
        drama = arguments['library_id'] == 'drama'
        return {'scope': {'library': {'id': arguments['library_id'], 'name': '电视剧' if drama else '动漫'}},
                'granularity': 'work', 'items': [{'name': title} for title in (titles if drama else anime)]}
    monkeypatch.setattr(media_library, 'recent', recent)
    host = sdk_host(row, [sdk_read('library.recent', {'server': 'jellyfin', 'library_id': identifier}, turn=turn, evidence_id=f'call-{turn}')
                          for turn, identifier in [(1, 'drama'), (2, 'anime')]])
    assert eval_agent.assess_live_task(case, row, host_evidence=host)['passed']
    wrong = copy.deepcopy(host)
    wrong['sdk_reads'][0]['arguments']['library_id'] = 'other'
    assert not eval_agent.assess_live_task(case, row, host_evidence=wrong)['passed']
    wrong = copy.deepcopy(host)
    wrong['sdk_reads'][0]['result']['granularity'] = 'episode'
    assert not eval_agent.assess_live_task(case, row, host_evidence=wrong)['passed']
    assert not eval_agent.assess_live_task(case, row, host_evidence={**host, 'sdk_reads': []})['passed']
    assert not eval_agent.assess_live_task(case, row, host_evidence={**host, 'run_id': 'other'})['passed']


@pytest.mark.parametrize("granularity", ["work", "episode"])
def test_native_scope_aba_accepts_code_grouping_of_larger_source_window(cases, monkeypatch, granularity):
    from app.modules import media_library
    case = cases["scope_aba"]
    row = trace(case)
    def recent(server, **arguments):
        name = "电视剧" if arguments["library_id"] == "drama" else "动漫"
        items = [dict(id=f"{name}-{number}-{episode}", type="Episode", series_id=f"{name}-{number}",
                      series_name=f"{name}作品{number}号", name=f"单集{episode}")
                 for number in range(8) for episode in range(2 if granularity == "episode" else 1)]
        return {"scope": {"library": {"id": arguments["library_id"], "name": name}},
                "granularity": granularity, "items": items}
    monkeypatch.setattr(media_library, "recent", recent)
    for number, name in ((0, "电视剧"), (1, "动漫"), (2, "电视剧")):
        row["turns"][number]["answer"] = "、".join(f"{name}作品{i}号" for i in range(5))
    host = sdk_host(row, [sdk_read("library.recent", {"library_id": identifier, "limit": 20},
                                  turn=turn, evidence_id=f"scope-{turn}")
                          for turn, identifier in ((1, "drama"), (2, "anime"))])
    assert eval_agent.assess_live_task(case, row, host_evidence=host)["passed"]
    row["turns"][2]["answer"] = row["turns"][2]["answer"].replace("电视剧作品0号", "电视剧作品5号")
    assert not eval_agent.assess_live_task(case, row, host_evidence=host)["passed"]
