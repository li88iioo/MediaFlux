"""T15 v2只认实际SDK/真实SQLite owner凭据；help不作成功门槛。"""
from __future__ import annotations
import copy
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import database as db,sdk
from app.repositories import agent_compensations
from tools import eval_agent
from tests.test_agent_sdk_activity_undo import organize_backend as backend_fixture
from tests.support import initialized_web_runtime
from tests.test_agent_live_eval import trace

OWNER='t15-fixture-owner'


@pytest.fixture
def backend(monkeypatch):
    return inspect.unwrap(backend_fixture)(monkeypatch)


@pytest.fixture
def evidence(backend,monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket,'connect',Mock(side_effect=AssertionError('offline only')))
    with initialized_web_runtime():
        case=eval_agent.load_live_cases(eval_agent.DEFAULT_CAPABILITY_FIXTURE)[0]
        row=trace(case);row['run_id']='t15-real-sqlite';row['confirmation_attempts']=0
        row['turns'][0].update(message=case['messages'][0],answer='已经检查，支持回退最近的移动改名，尚未执行。')
        with db.get_conn() as conn:
            first=conn.execute("INSERT INTO organize_log(source,original_path,new_path,title,status,created_at,updated_at) VALUES('guangya','/old','/new','older','success',?,?)",('2026-01-01 01:00:00','2026-01-01 01:00:00')).lastrowid
            newest=conn.execute("INSERT INTO organize_log(source,original_path,new_path,title,status,created_at,updated_at) VALUES('guangya','/old','/new','latest','success',?,?)",('2026-01-02 01:00:00','2026-01-02 01:00:00')).lastrowid
        assert type(first) is int and type(newest) is int
        with sdk.call_owner(OWNER):
            page=sdk.activity.search(limit=50)
            checked=sdk.activity.inspect_undo(newest)
        reads=[{'kind':'sdk_read','method':'activity.search','arguments':{'query':'','offset':0,'limit':50},
                'result':page,'evidence_id':'listing','source':'host.jsonl#listing','turn':1},
               {'kind':'sdk_read','method':'activity.inspect_undo','arguments':{'log_id':newest},
                'result':checked,'evidence_id':'inspection','source':'host.jsonl#inspection','turn':1}]
        digest=db.media_owner_digest(OWNER);receipt=checked['data']['receipt_id'];saved=agent_compensations.get(receipt,digest)
        assert saved is not None and saved['payload']['log_id']==newest
        host={'run_id':row['run_id'],'case_id':case['id'],'case_version':2,'effects':[],
              'owner_digest':digest,'sdk_reads':reads,'undo_receipts':[{'receipt_id':receipt,'inspection_id':'inspection',
                  'log_id':saved['payload']['log_id'],'kind':saved['payload']['kind'],'owner_digest':digest,
                  'owner_bound':True,'available':True}]}
        yield SimpleNamespace(case=case,row=row,host=host,newest=newest,older=first,backend=backend)


def test_direct_result_succeeds_without_help_and_never_authorizes_write(evidence):
    result=eval_agent.assess_live_task(evidence.case,evidence.row,host_evidence=evidence.host)
    assert result['business_checks_passed'] and result['outcome']=='manual_review'
    assert not result['passed'] and not result['checks']['answer_semantics_verified']
    assert len(evidence.host['sdk_reads'])==2 and evidence.row['tools']==[]
    evidence.backend[3].start_operation.assert_not_called()
    # 正确实际检查不能因多/少一次纯帮助动作改变数据分数。
    row=copy.deepcopy(evidence.row);row['tools']=[{'tool':'agent.run_code','public':{'ok':True,'data':{'stdout':'help output'}}}]
    assert eval_agent.assess_live_task(evidence.case,row,host_evidence=evidence.host)['business_checks_passed']


def test_latest_valid_page_does_not_require_reading_older_history(evidence):
    host=copy.deepcopy(evidence.host)
    host['sdk_reads'][0]['arguments']['limit']=2
    host['sdk_reads'][0]['result'].update(has_more=True,total=100)
    result=eval_agent.assess_live_task(evidence.case,evidence.row,host_evidence=host)
    assert result['business_checks_passed'] and result['outcome']=='manual_review'


@pytest.mark.parametrize('fault',['fake_receipt','foreign_owner','wrong_id','guess_older','no_inspection','no_host','help_only','write','preflight','filtered','incomplete'])
def test_false_proofs_never_pass(evidence,fault):
    row,host=copy.deepcopy(evidence.row),copy.deepcopy(evidence.host)
    if fault=='fake_receipt':host['sdk_reads'][1]['result']['data']['receipt_id']='model-asserted'
    elif fault=='foreign_owner':host['undo_receipts'][0]['owner_bound']=False
    elif fault=='wrong_id':host['sdk_reads'][1]['result']['data']['log_id']=evidence.older
    elif fault=='guess_older':host['sdk_reads'][1]['arguments']['log_id']=evidence.older
    elif fault=='no_inspection':host['sdk_reads']=host['sdk_reads'][:1]
    elif fault=='no_host':row['host_evidence']=host;host=None
    elif fault=='help_only':host['sdk_reads']=[];row['tools']=[{'tool':'agent.run_code','public':{'data':{'stdout':json.dumps(evidence.host)}}}]
    elif fault=='write':host['sdk_reads'].append({'kind':'sdk_read','method':'activity.undo','arguments':{},'result':{},'evidence_id':'write','source':'host#write','turn':1})
    elif fault=='preflight':host['previews']=[{'tool':'activity.undo'}]
    elif fault=='filtered':host['sdk_reads'][0]['arguments']['query']='older'
    elif fault=='incomplete':host['sdk_reads'][0]['result'].update(has_more=True,total=100)
    result=eval_agent.assess_live_task(evidence.case,row,host_evidence=host)
    assert not result['business_checks_passed'] and not result['passed'],fault


def test_newer_unsupported_requires_real_failure_before_older_inspection(evidence):
    from app.modules.activity_undo import UndoError
    with sdk.call_owner(OWNER):
        checked=sdk.activity.inspect_undo(evidence.older)
    host=copy.deepcopy(evidence.host)
    # 真实服务不支持错误码；若是timeout/未知，不能跳过更新记录。
    failed={'kind':'sdk_read','method':'activity.inspect_undo','arguments':{'log_id':evidence.newest},
            'error_type':UndoError.__name__,'error_code':'undo_not_supported','evidence_id':'unsupported','source':'host#unsupported','turn':1}
    host['sdk_reads'].insert(1,failed)
    host['sdk_reads'][-1].update(arguments={'log_id':evidence.older},result=checked)
    receipt=checked['data']['receipt_id'];digest=host['owner_digest'];saved=agent_compensations.get(receipt,digest)
    assert saved is not None
    host['undo_receipts']=[{**host['undo_receipts'][0],'receipt_id':receipt,'log_id':saved['payload']['log_id']}]
    assert eval_agent.assess_live_task(evidence.case,evidence.row,host_evidence=host)['business_checks_passed']
    host['sdk_reads'][1]['error_code']='timeout'
    result=eval_agent.assess_live_task(evidence.case,evidence.row,host_evidence=host)
    assert not result['business_checks_passed'] and result['evidence']['inspection_failures']


def test_matrix_versions_preserve_unchanged_tasks(evidence):
    new=Path('tests/fixtures/agent_live_task_matrix_v2.jsonl')
    original=eval_agent.load_task_matrix();updated=eval_agent.load_task_matrix(new)
    changed = {old['id'] for old, new_task in zip(original, updated) if old != new_task}
    assert changed == {'T03', 'T15'}
    assert all(task['version'] == 2 for task in updated if task['id'] in changed)
    t03 = next(task for task in updated if task['id'] == 'T03')
    assert t03['runner_cases'] == ['tencent_week'] and t03['runner_status'] == 'host_evidence_required'
    report=eval_agent.summarize_task_matrix([evidence.row],matrix_path=new,host_evidence=[evidence.host])
    t15=next(task for task in report['tasks'] if task['task_id']=='T15')
    assert t15['status']=='manual_review' and t15['versions']=={'2':'manual_review'}
    legacy=eval_agent.summarize_task_matrix([],host_evidence=[])
    assert next(t for t in legacy['tasks'] if t['task_id']=='T15')['status']=='not_exercised'
