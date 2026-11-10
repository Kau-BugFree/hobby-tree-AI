import asyncio
import importlib
import json
from pathlib import Path
import pytest


def module():
    return importlib.import_module('scripts.evaluate_dialogue')


def case():
    return {'case_id':'c','split':'dev','category':'followup','activity_id':'a','turns':['왜?','그럼?'],
            'expected_state':{'selected_activity_id':'a'}, 'required_points':['원문'], 'forbidden_claims':['완벽'],
            'allowed_evidence_ids':['e'],'expected_stop_reason':None}


def test_ungraded_semantics_and_partial_answer_are_not_success():
    result=module().score_dialogue(case(),[{'response':{'mode':'reference','text':'원문','cited_evidence_ids':['e'],
        'metacognition':{'unresolved':[{'reason':'missing_knowledge'}]},'diagnostics':{}},'state':{'selected_activity_id':'a'}}])
    assert result['answered'] and not result['generated']
    assert result['human_complete'] is None and result['complete'] is not True
    assert result['unresolved_count']==1


def test_runs_are_isolated_and_replay_calls_excluded():
    seen=[]
    async def runner(c,run_id,mode):
        seen.append((run_id,mode))
        return [{'response':{'mode':'reference','text':'原文','diagnostics':{'replayed':True,'llm_calls':9}},'state':{'selected_activity_id':'a'}}]
    one=asyncio.run(module().run_comparison([case()],runner,2,'bundle'))
    two=asyncio.run(module().run_comparison([case()],runner,1,'agent'))
    assert len({r[0] for r in seen})==3 and one['run_id']!=two['run_id']
    assert one['llm_calls']==0


def test_corpus_splits_are_frozen_and_balanced():
    root=Path(__file__).resolve().parents[1]/'data'
    dev=module().load_cases(root/'dialogue_eval_dev.jsonl')
    holdout=module().load_cases(root/'dialogue_eval_holdout.jsonl')
    module().validate_splits(dev,holdout)
    assert len(dev)==len(holdout)==30
    assert len({c['activity_id'] for c in dev})>=10


def test_duplicate_dialogue_across_splits_is_rejected():
    with pytest.raises(ValueError):
        module().validate_splits([case()],[{**case(),'case_id':'different','split':'holdout'}])


def test_default_cli_never_constructs_live_runner(tmp_path, monkeypatch):
    mod=module()
    monkeypatch.setattr(mod,'build_runner',lambda *a: (_ for _ in ()).throw(AssertionError('must not build')))
    out=tmp_path/'report.json'
    assert mod.main(['--output',str(out)])==0
    report=json.loads(out.read_text(encoding='utf-8'))
    assert report['kind']=='schema_validation' and report['model_quality_passed'] is None


def test_compound_with_no_requirements_is_not_automatic_pass():
    compound={**case(),'category':'compound'}
    result=module().score_dialogue(compound,[{'response':{'mode':'reference','text':'원문',
        'metacognition':{'supported':[],'unresolved':[]}},
        'state':{'selected_activity_id':'a','dialogue':{'requirements':[],'coverage':[]}}}])
    assert not result['automatic_passed']


def test_stored_supported_response_unresolved_is_not_automatic_pass():
    result=module().score_dialogue(case(),[{'response':{'mode':'reference','text':'원문',
        'metacognition':{'supported':[],'unresolved':[{'requirement_id':'r','reason':'missing_knowledge'}]}},
        'state':{'selected_activity_id':'a','dialogue':{'requirements':[{'id':'r'}],
            'coverage':[{'requirement_id':'r','status':'supported'}]}}}])
    assert not result['automatic_passed']
