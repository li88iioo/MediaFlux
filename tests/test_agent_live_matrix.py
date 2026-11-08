"""矩阵与领域判据的离线反例；这些轨迹绝不算真实验收结果。"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import pytest

from tests.test_agent_live_eval import receipt, sdk_host, sdk_read, trace
from tools import eval_agent


@pytest.fixture
def matrix_cases():
    return {row["id"]: row for row in eval_agent.load_live_cases(eval_agent.DEFAULT_MATRIX_CASES)}


def actor_receipt(actor="成龙", total=71):
    return receipt("library.search", {"query": actor, "search_by": "actor", "media_type": "movie", "count_only": True},
                   {"match_basis": "actor_metadata", "total": total,
                    "sources": [{"server_type": "jellyfin", "status": "ready", "total": total, "total_source": "TotalRecordCount"}]})


def test_manifest_has_26_tasks_and_six_contextual_runner_cases_without_rule_dsl(matrix_cases):
    matrix = eval_agent.load_task_matrix()
    assert len(matrix) == 26
    assert {row["id"] for row in matrix} == {f"T{index:02}" for index in range(1, 27)}
    assert {key for row in matrix for key in row["runner_cases"]} == matrix_cases.keys()
    assert len(matrix_cases["constraints_eight"]["messages"]) == 8
    assert all(row["acceptance"] and row["external_conditions"] and row["steps"] for row in matrix)
    assert all("checks" not in row and "observations" not in row for row in matrix)
    frozen = eval_agent.DEFAULT_LIVE_FIXTURE.read_bytes()
    assert hashlib.sha256(frozen).hexdigest() == "568476cc5dde773059a0dbe8f831d83c10202fb6c62fc9f937a275b33c92432b"


@pytest.mark.parametrize("total,exercised", [(0, False), (36, False), (50, False), (51, True), (71, True)])
def test_actor_over_fifty_requires_actual_actor_server_total(matrix_cases, total, exercised):
    case = matrix_cases["actor_count_over_fifty"]
    row = trace(case)
    row["tools"] = [actor_receipt(total=total)]
    row["turns"][0]["answer"] = f"成龙参演电影总数为{total}，按演员元数据和Jellyfin统计。"
    result = eval_agent.assess_live_task(case, row)
    assert result["passed"] is exercised
    if not exercised:
        assert result["outcome"] == "not_exercised"
    row["tools"][0]["public"]["data"]["match_basis"] = "title_search"
    assert not eval_agent.assess_live_task(case, row)["passed"]


def test_actor_over_fifty_keeps_server_counts_and_delivery_on_same_receipt(matrix_cases):
    case = matrix_cases["actor_count_over_fifty"]
    row = trace(case)
    row["tools"] = [actor_receipt(total=71), actor_receipt(total=36)]
    row["turns"][0]["answer"] = "成龙参演电影总数71部，另一记录36部。"
    row["tools"][0]["public"]["data"]["sources"][0]["total"] = 1
    assert not eval_agent.assess_live_task(case, row)["checks"]["server_totals_verified"]
    row["tools"] = [actor_receipt(total=71)]
    row["turns"][0]["answer"] = "成龙参演电影总数50部。"
    result = eval_agent.assess_live_task(case, row)
    assert not result["passed"] and result["outcome"] == "failed"
    assert result["checks"]["observed_total_over_fifty"]


def test_matrix_cli_consumes_bound_native_host_reads_without_changing_frozen_cases(matrix_cases, monkeypatch, tmp_path, capsys):
    from app import services

    case = matrix_cases["actor_count_over_fifty"]
    monkeypatch.setattr(services, "_search_sources", lambda *a, **k: [
        {"server_type": "jellyfin", "server_name": "fixture", "items": [], "total": 71,
         "total_source": "TotalRecordCount", "has_more": False}])
    row = trace(case)
    row["turns"][0]["answer"] = "成龙参演电影总数71部，Jellyfin演员元数据统计。"
    host = sdk_host(row, [sdk_read("library.search", {"query": "成龙", "search_by": "actor",
                                                      "media_type": "movie", "count_only": True})])
    trace_path, host_path = tmp_path / "trace.json", tmp_path / "host.json"
    trace_path.write_text(json.dumps(row), encoding="utf-8")
    host_path.write_text(json.dumps(host), encoding="utf-8")
    args = ["--matrix-results", str(trace_path)]
    assert eval_agent.main(args) == 1
    before = json.loads(capsys.readouterr().out)
    assert before["tasks"][0]["status"] == "not_exercised"
    assert eval_agent.main([*args, "--host-evidence", str(host_path)]) == 1
    after = json.loads(capsys.readouterr().out)
    assert after["tasks"][0]["status"] == "automated_passed"
    assert after["summary"]["fully_reviewed_passed"] == 0 and not after["ok"]
    assert all(task["status"] == "not_exercised" for task in after["tasks"][1:])
    assert hashlib.sha256(eval_agent.DEFAULT_LIVE_FIXTURE.read_bytes()).hexdigest() == "568476cc5dde773059a0dbe8f831d83c10202fb6c62fc9f937a275b33c92432b"


def test_dune_needs_actual_movie_entity_year_not_answer_keyword(matrix_cases):
    case = matrix_cases["dune_year"]
    row = trace(case)
    row["turns"][0]["answer"] = "媒体库已收录2021年《沙丘》。"
    row["tools"] = [receipt("library.search", {"query": "沙丘", "media_type": "movie"}, {
        "sources": [{"server_type": "jellyfin", "status": "ready", "items": [
            {"title": "沙丘", "year": "1984", "media_type": "movie"}]}]})]
    assert not eval_agent.assess_live_task(case, row)["passed"]
    row["tools"][0]["public"]["data"]["sources"][0]["items"].append(
        {"title": "沙丘", "year": "2021", "media_type": "movie"})
    result = eval_agent.assess_live_task(case, row)
    assert result["passed"] and result["evidence"]["other_entity_years"] == ["1984"]
    row["tools"][0]["public"]["data"]["sources"][0]["items"][-1]["media_type"] = "series"
    assert not eval_agent.assess_live_task(case, row)["passed"]


def test_empty_library_does_not_exercise_dune_identity_branch(matrix_cases):
    case = matrix_cases["dune_year"]
    row = trace(case)
    row["tools"] = [receipt("library.search", {"query": "沙丘"}, {"sources": [{"status": "ready", "items": []}]})]
    row["turns"][0]["answer"] = "查询未找到2021沙丘。"
    result = eval_agent.assess_live_task(case, row)
    assert not result["passed"] and result["outcome"] == "not_exercised"


@pytest.mark.parametrize("case_id", ["read_missing_query", "read_invalid_enum"])
def test_read_fault_not_triggered_is_not_exercised_even_when_actor_answer_correct(matrix_cases, case_id):
    case = matrix_cases[case_id]
    row = trace(case)
    row["tools"] = [actor_receipt("周星驰")]
    row["turns"][0]["answer"] = "周星驰参演电影总计71部。"
    result = eval_agent.assess_live_task(case, row)
    assert not result["passed"] and result["outcome"] == "not_exercised"


def recovered_trace(case):
    row = trace(case)
    row["matrix"] = {"scenario": case["id"], "applied": [{"tool": "library.search", "variant": case["rubric"]["fault"],
                      "after": {} if case["rubric"]["fault"] == "missing_query" else {"media_type": "matrix_invalid_enum"},
                      "error_code": "invalid_arguments"}]}
    row["tools"] = [{"tool": "library.search", "arguments": {}, "error": "invalid_arguments"}, actor_receipt("周星驰")]
    row["turns"][0].update(answer="周星驰参演电影总计71部。", events=[{"type": "tool.failed", "payload": {"code": "invalid_arguments"}}])
    return row


@pytest.mark.parametrize("case_id", ["read_missing_query", "read_invalid_enum"])
def test_read_recovery_requires_error_feedback_once_bounded_and_correct_answer(matrix_cases, case_id):
    case = matrix_cases[case_id]
    row = recovered_trace(case)
    result = eval_agent.assess_live_task(case, row)
    assert result["passed"] and result["execution_mode"] == "hybrid"
    for mutation in ("no_error_event", "duplicate_injection", "four_attempts", "wrong_total"):
        bad = copy.deepcopy(row)
        if mutation == "no_error_event":
            bad["turns"][0]["events"] = []
        elif mutation == "duplicate_injection":
            assert bad["matrix"] is not None
            bad["matrix"]["applied"] *= 2
        elif mutation == "four_attempts":
            bad["tools"] += copy.deepcopy(bad["tools"])
        else:
            bad["turns"][0]["answer"] = "电影总计50部。"
        assert not eval_agent.assess_live_task(case, bad)["passed"], mutation


def eight_constraints_trace(matrix_cases):
    """离线观察与交付样本；判据仅认事实/真实ID关系，工具名称不提供证明。"""
    case = matrix_cases['constraints_eight']
    row = trace(case)
    for number, (turn, message, constraints) in enumerate(zip(row['turns'], case['messages'], eval_agent.EIGHT_TURN_CONSTRAINTS), 1):
        turn['message'] = message
        if number == 8:
            turn['answer'] = row['turns'][6]['answer']
            continue
        season, resolution, sdr, exclude_hdr = constraints
        title = f'Black.Mirror.S{season:02}E01.{resolution}.SDR'
        identity = f'observed-resource-{number:016d}'
        query = {'title': '黑镜', 'season': season, 'episode': 1, 'resolution': resolution,
                 'dynamic_range': 'SDR' if sdr else '', 'exclude_hdr': exclude_hdr}
        item = {'result_id': identity, 'title': title, 'technical_evidence': {
            'resolution': resolution, 'dynamic_range': 'SDR', 'dynamic_ranges': ['SDR'],
            'basis': 'release_title_only', 'stream_verified': False}}
        row['tools'].append({'turn': number, 'tool': 'agent.run_code', 'arguments': query,
            'public': {'ok': True}, 'model_content': json.dumps({'ok': True, 'data': {'items': [item], 'effective_query': query}})})
        row['tools'].append({'turn': number, 'tool': 'agent.run_code', 'arguments': {'result_ids': [identity]},
            'public': {'ok': True}, 'model_content': json.dumps({'ok': True, 'data': {'result_ids': [identity], 'effective_query': query}})})
        turn['answer'] = title
    return row


def change_receipt(tool, modify):
    value = json.loads(tool['model_content'])
    modify(value['data'])
    tool['model_content'] = json.dumps(value)


def test_eight_turns_reuses_observed_facts_and_requires_manual_language_review(matrix_cases):
    row = eight_constraints_trace(matrix_cases)
    result = eval_agent.assess_live_task(matrix_cases['constraints_eight'], row)
    assert not result['passed'] and result['outcome'] == 'manual_review'
    assert result['failure_reasons'] == ['answer_semantics_verified']
    assert result['evidence']['final_referenced_candidate_ids'] == ['observed-resource-0000000000000007']
    assert result['evidence']['turns'][-1]['referenced_candidates'][0]['source_turn'] == 7
    for tool in row['tools']:
        tool['tool'] = 'arbitrary-recorded-read-name'
    renamed = eval_agent.assess_live_task(matrix_cases['constraints_eight'], row)
    assert renamed['checks'] == result['checks'] and renamed['evidence'] == result['evidence']


@pytest.mark.parametrize('mutation', ['foreign_id', 'stale_batch', 'returned_not_requested', 'duplicate', 'missing_id', 'forged_query', 'wrong_season', 'wrong_show', 'wrong_resolution', 'unknown_sdr', 'mixed_hdr', 'missing_observation'])
def test_eight_turn_identity_query_and_source_facts_cannot_be_forged(matrix_cases, mutation):
    row = eight_constraints_trace(matrix_cases)
    source, present = row['tools'][-2:]
    if mutation in {'foreign_id', 'stale_batch'}:
        identity = 'invented-resource-12345' if mutation == 'foreign_id' else json.loads(row['tools'][0]['model_content'])['data']['items'][0]['result_id']
        present['arguments']['result_ids'] = [identity]
        change_receipt(present, lambda data: data.update(result_ids=[identity]))
    elif mutation == 'returned_not_requested':
        present['arguments']['result_ids'] = []
    elif mutation == 'duplicate':
        present['arguments']['result_ids'] *= 2
    elif mutation == 'missing_id':
        change_receipt(source, lambda data: data['items'][0].pop('result_id'))
    elif mutation == 'forged_query':
        change_receipt(present, lambda data: data['effective_query'].update(season=2))
    elif mutation in {'wrong_season', 'wrong_show', 'wrong_resolution', 'unknown_sdr', 'mixed_hdr'}:
        def modify(data):
            item = data['items'][0]
            item['title'] = item['title'].replace({'wrong_season': 'S03', 'wrong_show': 'Black.Mirror', 'wrong_resolution': '1080p', 'unknown_sdr': '.SDR', 'mixed_hdr': '.SDR'}[mutation],
                {'wrong_season': 'S02', 'wrong_show': 'Other.Show', 'wrong_resolution': '2160p', 'unknown_sdr': '.WEB-DL', 'mixed_hdr': '.SDR.HDR10'}[mutation])
        change_receipt(source, modify)
    else:
        source['model_content'] = json.dumps({'ok': True})
    result = eval_agent.assess_live_task(matrix_cases['constraints_eight'], row)
    assert result['outcome'] == 'failed'
    assert not result['checks']['turn_7_no_invalid_structured_delivery']


@pytest.mark.parametrize('number,field,value', [(1,'season',2),(2,'episode',2),(3,'resolution','1080p'),(4,'exclude_hdr',False),(5,'season',2),(6,'dynamic_range',''),(7,'resolution','2160p')])
def test_each_actual_turn_retains_its_own_constraints(matrix_cases, number, field, value):
    row = eight_constraints_trace(matrix_cases)
    source, present = row['tools'][(number-1)*2:number*2]
    for tool in (source, present):
        change_receipt(tool, lambda data: data['effective_query'].update({field:value}))
    result = eval_agent.assess_live_task(matrix_cases['constraints_eight'], row)
    assert not result['checks'][f'turn_{number}_constraints_observed']
    assert result['outcome'] == 'failed'


def test_last_turn_facts_do_not_prove_missing_first_seven_turns(matrix_cases):
    row = eight_constraints_trace(matrix_cases)
    row['tools'] = row['tools'][-2:]
    for tool in row['tools']:
        tool['turn'] = 8
    result = eval_agent.assess_live_task(matrix_cases['constraints_eight'], row)
    assert all(not result['checks'][f'turn_{n}_constraints_observed'] for n in range(1,8))
    assert result['checks']['turn_8_constraints_observed'] and result['outcome'] == 'failed'


@pytest.mark.parametrize('hdr', ['HDR10', 'DoVi', 'HLG'])
def test_known_hdr_cannot_pass_exclusion_by_claiming_sdr_in_evidence(matrix_cases, hdr):
    row = eight_constraints_trace(matrix_cases)
    change_receipt(row['tools'][6], lambda data: data['items'][0].update(title=data['items'][0]['title']+'.'+hdr))
    result = eval_agent.assess_live_task(matrix_cases['constraints_eight'], row)
    assert not result['checks']['turn_4_no_invalid_structured_delivery']


def test_empty_subset_filter_does_not_claim_complete_source_search(matrix_cases):
    row = eight_constraints_trace(matrix_cases)
    source, present = row['tools'][-2:]
    change_receipt(source, lambda data: data['effective_query'].update(dynamic_range=''))
    present['arguments'].update(dynamic_range='SDR')
    scope = {'candidate_count':20,'considered_count':1,'complete':False}
    change_receipt(present, lambda data: data.update(result_ids=[], scope=scope,
        technical_filter={'requested':{'resolution':'1080p','dynamic_range':'SDR','exclude_hdr':True},'unknown_excluded':1}))
    result = eval_agent.assess_live_task(matrix_cases['constraints_eight'], row)
    assert result['outcome'] == 'manual_review' and not result['passed']
    delivery = result['evidence']['turns'][6]['structured_deliveries'][0]
    assert delivery['scope'] == scope and delivery['candidate_ids'] == []
    assert len(delivery['requested_ids']) == 1 and delivery['matches_constraints']


def test_empty_card_without_filter_receipt_or_source_facts_never_proves_absence(matrix_cases):
    row = eight_constraints_trace(matrix_cases)
    source, present = row['tools'][-2:]
    change_receipt(source, lambda data: data['effective_query'].update(dynamic_range=''))
    change_receipt(present, lambda data: data.update(result_ids=[], effective_query=json.loads(source['model_content'])['data']['effective_query']))
    result = eval_agent.assess_live_task(matrix_cases['constraints_eight'], row)
    assert not result['checks']['turn_7_constraints_observed'] and result['outcome'] == 'failed'


def test_pagination_requires_actual_front_pages_and_inventory_counts():
    row: dict[str, Any] = {"matrix": {"scenario": "hybrid_guoman_front_owned", "applied": []}}
    assert not all(eval_agent.assess_pagination_replay(row).values())
    row["matrix"].update(applied=[{"owned_ids": ["1", "2"]}], replay={
        "provenance": {"kind": "synthetic_contract_replay"},
        "pages": [{"page": 1, "ids": ["1"]}, {"page": 2, "ids": ["2"]}, {"page": 3, "ids": ["3"]}],
        "inventory_reads": [{"tool_index": 0, "server": "jellyfin", "media_type": "tv"}]})
    assert all(eval_agent.assess_pagination_replay(row).values())
    row["matrix"]["replay"]["inventory_reads"].append({"tool_index": 1, "server": "jellyfin", "media_type": "tv"})
    assert not eval_agent.assess_pagination_replay(row)["one_inventory_per_request_source_type"]


def test_seven_query_cases_and_pytest_cannot_become_26_matrix_passes():
    assert eval_agent.summarize_task_matrix([])["summary"]["tasks_with_runs"] == 0
    cases = eval_agent.load_live_cases()
    rows = [trace(case) for case in cases]
    result = eval_agent.summarize_task_matrix(rows)
    assert not result["ok"]
    assert result["summary"]["fully_reviewed_passed"] == 0
    for task in result["tasks"]:
        if task["task_id"] in {"T17", "T24", "T25"}:
            assert task["status"] == "not_exercised" and not task["runs"]
        if task["task_id"] in {"T03", "T12"}:
            assert task["status"] == "not_exercised"


def test_matrix_requires_both_read_faults_and_two_year_entities_for_full_task(matrix_cases):
    missing = recovered_trace(matrix_cases["read_missing_query"])
    invalid = recovered_trace(matrix_cases["read_invalid_enum"])
    invalid["tools"][1]["arguments"].pop("media_type")
    dune = trace(matrix_cases["dune_year"])
    dune["tools"] = [receipt("library.search", {"query": "沙丘"}, {"sources": [{"status": "ready", "items": [
        {"title": "沙丘", "year": "2021", "media_type": "movie"}]}]})]
    dune["turns"][0]["answer"] = "媒体库有《沙丘》（2021）。"
    report = eval_agent.summarize_task_matrix([missing, invalid, dune])
    assert next(task for task in report["tasks"] if task["task_id"] == "T14")["status"] == "failed"
    assert next(task for task in report["tasks"] if task["task_id"] == "T11")["status"] == "partial"
    report = eval_agent.summarize_task_matrix([missing])
    assert next(task for task in report["tasks"] if task["task_id"] == "T14")["status"] == "partial"


def test_harness_interference_annotation_does_not_rewrite_original_trace(tmp_path, matrix_cases):
    case = matrix_cases["actor_count_over_fifty"]
    row = trace(case)
    row.update(source="affected-run", tools=[actor_receipt()])
    row["turns"][0]["answer"] = "成龙电影总计71部。"
    result_path, note_path = tmp_path / "result.json", tmp_path / "note.json"
    result_path.write_text(json.dumps(row))
    note_path.write_text(json.dumps({"affected_run": "affected-run/actor_count_over_fifty", "classification": "infrastructure_interference", "cause": "reproduced temporary rename race"}))
    loaded = eval_agent.load_live_traces([result_path], [note_path])
    result = eval_agent.assess_live_task(case, loaded[0])
    assert not result["passed"] and result["outcome"] == "infrastructure_interference"
    assert "classification" not in json.loads(result_path.read_text())


def test_country_genre_adult_are_supplementary_and_frozen_threshold_unchanged():
    case = next(row for row in eval_agent.load_live_cases() if row["id"] == "guoman_twenty")
    row = trace(case)
    row["tools"] = [receipt("discovery.recommend", {}, {"items": [{"tmdb_id": "1", "title": "动画", "year": "2026"}]})]
    row["source_evidence"] = [{"items": [{"id": 1, "origin_country": ["JP"], "genre_ids": [16], "adult": False}]}]
    extra = eval_agent.live_supplementary_evidence(case, row)
    assert extra["source_metadata_rejected_ids"] == ["1"]
    assert not eval_agent.assess_live_task(case, row)["passed"]


def test_partial_calendar_does_not_get_promoted_by_supplementary_event_count():
    case = next(row for row in eval_agent.load_live_cases() if row["id"] == "tencent_week")
    row = trace(case)
    row["tools"] = [receipt("discovery.anime_calendar", {"day": "week", "source": "tencent"}, {
        "items": [{"title": "动画", "date": "2026-10-12", "update_time": "10:00", "audience": who} for who in ("member", "free")],
        "sources": [{"status": "partial"}], "has_more": False})]
    extra = eval_agent.live_supplementary_evidence(case, row)
    assert extra["returned_events"] == 2 and extra["distinct_title_date_time_slots"] == 1
    assert not eval_agent.assess_live_task(case, row)["passed"]


def test_matrix_cli_only_reads_manifest_or_recordings(monkeypatch, capsys, tmp_path, matrix_cases):
    monkeypatch.setattr(eval_agent, "evaluate", lambda *a: pytest.fail("no runtime may be started"))
    assert eval_agent.main(["--matrix"]) == 0
    assert len(json.loads(capsys.readouterr().out)["tasks"]) == 26
    row = trace(matrix_cases["hybrid_guoman_front_owned"])
    path = tmp_path / "result.json"
    path.write_text(json.dumps(row))
    assert eval_agent.main(["--matrix-results", str(path)]) == 1
    report = json.loads(capsys.readouterr().out)
    t10 = next(task for task in report["tasks"] if task["task_id"] == "T10")
    assert t10["status"] == "not_exercised"


def test_v2_same_id_does_not_relabel_a_frozen_v1_matrix_run():
    case = eval_agent.load_live_cases(eval_agent.DEFAULT_NATURAL_FIXTURE)[0]
    with pytest.raises(ValueError, match="未知case"):
        eval_agent.summarize_task_matrix([trace(case)])
