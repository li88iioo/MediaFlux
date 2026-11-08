"""新v2验收仅认宿主实际SDK观察；离线服务替身，不计真实任务完成率。"""
from __future__ import annotations

import copy

import pytest

from tools import eval_agent as evaluation
from tests.test_agent_live_eval import sdk_host, sdk_read, trace


@pytest.fixture
def cases():
    return {row['id']: row for row in evaluation.load_live_cases(evaluation.DEFAULT_SDK_FIXTURE)}


def run(case):
    row = trace(case)
    for turn, message in zip(row['turns'], case['messages']):
        turn['message'] = message
    return row


def test_v2_year_uses_real_sdk_entity_and_retains_v1_score(cases, monkeypatch):
    from app import services
    from app.clients.base import MediaItem
    monkeypatch.setattr(services, '_search_sources', lambda *a, **k: [
        {'server_type': 'jellyfin', 'server_name': 'fixture', 'total': 2,
         'total_source': 'TotalRecordCount', 'has_more': False, 'items': [
             MediaItem(id='dune-new', name='沙丘', year="2021", type='Movie'),
             MediaItem(id='dune-old', name='沙丘', year="1984", type='Movie')]}])
    case = cases['dune_year']
    row = run(case)
    row['turns'][0]['answer'] = '库中有2021版：沙丘（2021）。'
    host = sdk_host(row, [sdk_read('library.search', {'query': '沙丘', 'media_type': 'movie'})])
    result = evaluation.assess_live_task(case, row, host_evidence=host)
    assert result['business_checks_passed'] and result['outcome'] == 'manual_review'
    assert result['evidence']['matched_entities'][0]['id'] == 'dune-new'
    for fault in ('wrong_year', 'no_id', 'host_mismatch', 'model_json'):
        altered, evidence = copy.deepcopy(row), copy.deepcopy(host)
        if fault == 'wrong_year':
            evidence['sdk_reads'][0]['result']['data']['sources'][0]['items'][0]['year'] = '1984'
        elif fault == 'no_id':
            evidence['sdk_reads'][0]['result']['data']['sources'][0]['items'][0].pop('id')
        elif fault == 'host_mismatch':
            evidence['case_version'] = 1
        else:
            altered['host_evidence'] = evidence
            evidence = None
        assert not evaluation.assess_live_task(case, altered, host_evidence=evidence)['business_checks_passed'], fault
    # 旧版同样事实仍按旧版判据，不能把旧录改成v2来消除失败。
    old = copy.deepcopy(case);old['version'] = 1
    row['case_version'] = host['case_version'] = 1
    assert evaluation.assess_live_task(old, row, host_evidence=host)['outcome'] == 'not_exercised'


@pytest.mark.parametrize('fault', ['missing_query', 'invalid_enum'])
def test_v2_recovery_observes_binding_failure_without_model_tool_order(cases, monkeypatch, fault):
    from app import services, sdk
    monkeypatch.setattr(services, '_search_sources', lambda *a, **k: [
        {'server_type': 'jellyfin', 'total': 71, 'items': [], 'total_source': 'TotalRecordCount', 'has_more': False}])
    case = cases['read_'+fault]; row = run(case)
    row['turns'][0]['answer'] = '演员作品准确总数71。'
    arguments = {'search_by': 'actor', 'media_type': 'movie', 'count_only': True}
    if fault == 'invalid_enum':arguments.update(query='周星驰', media_type='matrix_invalid_enum')
    try:
        sdk.METHODS['library.search'](**arguments)
    except (TypeError, ValueError) as exc:
        failed = {'kind': 'sdk_read', 'evidence_id': 'actual-bind-error', 'source': 'host.jsonl#error',
                  'turn': 1, 'method': 'library.search', 'arguments': arguments,
                  'error_type': type(exc).__name__, 'error_code': type(exc).__name__, 'phase': 'binding'}
    else:
        pytest.fail('非法参数必须由实际SDK拒绝')
    valid = sdk_read('library.search', {'query': '周星驰', 'search_by': 'actor', 'media_type': 'movie', 'count_only': True}, evidence_id='corrected')
    host = sdk_host(row, [failed, valid])
    host['injections'] = [{'method': 'library.search', 'variant': fault, 'attempt_id': failed['evidence_id']}]
    # 外层任意数量的本地工作区动作不代替也不抹掉真实业务尝试。
    row['tools'] = [{'tool': 'agent.run_code', 'arguments': {}, 'turn': 1} for _ in range(5)]
    result = evaluation.assess_live_task(case, row, host_evidence=host)
    assert result['business_checks_passed'] and result['outcome'] == 'manual_review'
    for change in ('no_injection', 'duplicate', 'unrecovered', 'wrong_count', 'many_retries'):
        evidence = copy.deepcopy(host)
        if change == 'no_injection':evidence['injections'] = []
        elif change == 'duplicate':evidence['injections'] *= 2
        elif change == 'unrecovered':evidence['sdk_reads'] = [failed]
        elif change == 'wrong_count':evidence['sdk_reads'][-1]['result']['data']['total'] = 50
        else:
            for number in range(3):
                evidence['sdk_reads'].insert(1, {**failed, 'evidence_id': f'retry-{number}'})
        assert not evaluation.assess_live_task(case, row, host_evidence=evidence)['business_checks_passed'], change


def test_v2_aba_allows_saved_file_read_but_not_inventory_or_identity_drift(cases, monkeypatch):
    from app.modules import media_library
    def recent(server, **args):
        scope = '电视剧' if args['library_id'] == 'drama' else '动漫'
        return {'scope': {'library': {'id': args['library_id'], 'name': scope}}, 'granularity': 'work',
                'items': [{'id': scope+str(i), 'name': scope+'作品'+str(i)} for i in range(6)]}
    monkeypatch.setattr(media_library, 'recent', recent)
    case = cases['scope_aba']; row = run(case)
    for turn, name in zip(row['turns'], ('电视剧', '动漫', '电视剧')):
        turn['answer'] = '、'.join(name+'作品'+str(i) for i in range(5))
    host = sdk_host(row, [sdk_read('library.recent', {'library_id': identifier}, turn=t, evidence_id=f'call-{t}')
                          for t, identifier in ((1,'drama'),(2,'anime'))])
    row['tools'] = [{'tool': 'agent.run_code', 'arguments': {'code': 'load saved file'}, 'turn': 3}]
    assert evaluation.assess_live_task(case,row,host_evidence=host)['business_checks_passed']
    old = copy.deepcopy(case);old['version'] = row['case_version'] = host['case_version'] = 1
    assert not evaluation.assess_live_task(old,row,host_evidence=host)['passed']
    row['case_version'] = host['case_version'] = 2
    for change in ('new_remote_read','wrong_library','no_real_id','wrong_answer'):
        altered, evidence = copy.deepcopy(row),copy.deepcopy(host)
        if change == 'new_remote_read':evidence['sdk_reads'].append({**evidence['sdk_reads'][0], 'evidence_id':'return-read','turn':3})
        elif change == 'wrong_library':evidence['sdk_reads'][0]['result']['scope']['library']['id'] = 'other'
        elif change == 'no_real_id':
            for item in evidence['sdk_reads'][0]['result']['items']:item.pop('id')
        else:altered['turns'][2]['answer'] = '动漫作品0'
        assert not evaluation.assess_live_task(case,altered,host_evidence=evidence)['business_checks_passed'],change


def test_v2_native_preview_requires_frozen_ids_cancel_receipt_and_zero_execution(cases):
    case = cases['preview_twenty_cancel'];row = run(case)
    ids = [f'real-{i}' for i in range(20)]
    host = sdk_host(row,[{'kind':'sdk_read','method':'resources.search','arguments':{'title':'fixture'},'turn':1,
                        'evidence_id':'search','source':'host.jsonl#search','result':{'items':[{'result_id':i} for i in ids]}}])
    host.update(previews=[{'plan_id':'frozen','tool':'resources.download','arguments':{'result_ids':ids},
                'preview':{'status':'confirmation_required','data':{'count':20,'resources':[{'result_id':i} for i in ids]}}}],
                cancellations=[{'plan_id':'frozen','cancelled':True}])
    row['turns'][0]['status'] = 'approval_required'
    row['turns'][0]['events'] = [{'type':'effect.approval_required','payload':{'plan':{'plan_id':'frozen'}}}]
    assert evaluation.assess_live_task(case,row,host_evidence=host)['business_checks_passed']
    for change in ('different_plan','wrong_ids','model_only_cancel','executed','other_write','missing_source','wrong_count'):
        altered,evidence=copy.deepcopy(row),copy.deepcopy(host)
        if change=='different_plan':evidence['cancellations'][0]['plan_id']='foreign'
        elif change=='wrong_ids':evidence['previews'][0]['arguments']['result_ids'][-1]='unknown'
        elif change=='model_only_cancel':evidence['cancellations']=[];altered['cancelled_plan']=True
        elif change=='executed':altered['confirmed_executions']=1
        elif change=='other_write':evidence['sdk_attempts']=[{'method':'cloud.change'}]
        elif change=='wrong_count':evidence['previews'][0]['preview']['data']['count']=19
        else:evidence['sdk_reads']=[]
        assert not evaluation.assess_live_task(case,altered,host_evidence=evidence)['business_checks_passed'],change


def test_matrix_v2_is_opt_in_and_cannot_be_masked_by_old_version(cases):
    case=cases['dune_year'];new=run(case)
    with pytest.raises(ValueError,match='未知case'):
        evaluation.summarize_task_matrix([new])
    report=evaluation.summarize_task_matrix([new],cases_path=evaluation.DEFAULT_SDK_FIXTURE)
    task=next(t for t in report['tasks'] if t['task_id']=='T11')
    assert task['versions']=={'2':'inconclusive'}
    old=copy.deepcopy(new);old['case_version']=1
    report=evaluation.summarize_task_matrix([old,new],cases_path=evaluation.DEFAULT_SDK_FIXTURE)
    task=next(t for t in report['tasks'] if t['task_id']=='T11')
    assert set(task['versions'])=={'1','2'} and task['status']==task['versions']['2']


def test_bad_v2_host_shapes_are_failures_not_parser_crashes(cases):
    case=cases['read_missing_query'];row=run(case)
    host=sdk_host(row,[]);host['injections']=['not-a-host-observation']
    outcome=evaluation.assess_live_task(case,row,host_evidence=host)
    assert outcome['outcome']=='failed' and not outcome['business_checks_passed']
    assert 'contract_error' in outcome['evidence']


def test_frozen_v1_and_new_v2_fixture_loading_are_distinct(cases):
    assert all(case['version']==2 for case in cases.values())
    legacy=evaluation.load_live_cases(evaluation.DEFAULT_MATRIX_CASES)
    assert all(case['version']==1 for case in legacy)
    with pytest.raises(ValueError,match='版本不匹配'):
        evaluation.assess_live_task(cases['dune_year'],{**run(cases['dune_year']),'case_version':1})


def test_new_recovery_matrix_requires_both_faults_in_the_same_version(cases,monkeypatch):
    # 这里只测矩阵组合；单项失败/恢复已由上面的真实SDK与RPC测试覆盖。
    def completed(case,row,**kwargs):
        return {'case_id':case['id'],'version':case['version'],'passed':False,'outcome':'manual_review',
                'business_checks_passed':True,'evidence':{}}
    monkeypatch.setattr(evaluation,'assess_live_task',completed)
    first=run(cases['read_missing_query']);second=run(cases['read_invalid_enum'])
    def status(rows):
        report=evaluation.summarize_task_matrix(rows,cases_path=evaluation.DEFAULT_SDK_FIXTURE)
        return next(t for t in report['tasks'] if t['task_id']=='T14')
    assert status([first])['status']=='partial'
    assert status([first,second])['status']=='manual_review'
    old=copy.deepcopy(second);old['case_version']=1
    assert status([first,old])['status']=='partial'
