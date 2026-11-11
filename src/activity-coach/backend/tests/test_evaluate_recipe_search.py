import pytest
from scripts.evaluate_recipe_search import score_case, summarize, run_comparison, validate_cases


def test_scores_missing_positive_and_negatives_honestly():
    positive = {'case_id':'p','category':'name','expected_ids':['yes'], 'constraints':{}}
    row = score_case(positive, {'items':[{'activity_id':'no'}], 'diagnostics':{}}, 10)
    assert (row['hit_at_1'],row['hit_at_5'],row['mrr_at_5']) == (False,False,0)
    row2 = score_case(positive, {'items':[{'activity_id':'no'},{'activity_id':'yes'}], 'diagnostics':{}}, 20)
    assert row2['mrr_at_5'] == .5
    negative = dict(positive, expected_ids=[])
    row3 = score_case(negative, {'items':[{'activity_id':'no'}], 'diagnostics':{}}, 30)
    result = summarize([row,row2,row3])
    assert result['hit_at_5'] == .5
    assert result['negative_false_retrieval_rate'] == 1
    assert result['mrr_at_5'] == .25


class Fake:
    def __init__(self):
        self._dense = {'recipes':object(), 'activities':object()}
        self.calls = []
    def search(self, question, **kwargs):
        self.calls.append(('hybrid' if 'recipes' in self._dense else 'bm25', question, kwargs))
        return {'items':[], 'diagnostics':{}}


def test_alternates_three_repeats_preserves_constraints_and_restores_dense():
    service = Fake()
    original = service._dense['recipes']
    case = {'case_id':'p','category':'negative','question':'x','expected_ids':[], 'constraints':{'max_minutes':5}}
    report = run_comparison(service,[case])
    measured = service.calls[2:]
    assert [call[0] for call in measured] == ['bm25','hybrid','hybrid','bm25','bm25','hybrid']
    assert all(call[2] == {'scope':'recipes','hobby_key':'cooking','constraints':{'max_minutes':5},'limit':5} for call in service.calls)
    assert service._dense['recipes'] is original
    assert len(report['runs']) == 6


def test_restores_dense_on_failure():
    service = Fake()
    original = service._dense['recipes']
    def fail(*args, **kwargs):
        raise RuntimeError('failure')
    service.search = fail
    with pytest.raises(RuntimeError):
        run_comparison(service,[{'question':'x'}])
    assert service._dense['recipes'] is original


def test_rejects_missing_labels_and_duplicate_cases():
    with pytest.raises(ValueError):
        validate_cases([{'case_id':'x','question':'q'}])
    valid = {'case_id':'x','question':'q','expected_ids':[], 'category':'negative','rationale':'outside domain'}
    with pytest.raises(ValueError):
        validate_cases([valid,valid])


def test_constraint_violations_include_unknown_metadata():
    case = {'case_id':'c','category':'constraint','expected_ids':['x'], 'constraints':{'max_minutes':10,'available_tools':['pan']}}
    result = {'items':[{'activity_id':'x','duration':{'total_minutes':20},'tools':[], 'quality':{'flags':['tools_unknown']}}], 'diagnostics':{}}
    assert score_case(case,result,1)['constraint_violations'] == 2

def test_source_audit_and_hash_validation(tmp_path):
    import hashlib
    import json
    import sqlite3
    from scripts.evaluate_recipe_search import source_audit, sha256
    path = tmp_path / 'source.sqlite'
    raw = json.dumps({'activity_id':'x','duration':{'total_minutes':5}})
    with sqlite3.connect(path) as db:
        db.execute('create table records(id, scope, payload, tools_known, difficulty, budget)')
        db.execute('create table metadata(key,value)')
        db.execute('insert into records values(?,?,?,?,?,?)',('x','recipes',raw,0,None,None))
        db.execute('insert into metadata values(?,?)',('index:recipes','{"version":"v"}'))
    case = {'expected_ids':['x'], 'constraints':{'max_minutes':5}, 'source_evidence':{'x':{'payload_sha256':hashlib.sha256(raw.encode()).hexdigest()}}}
    assert source_audit(path,[case])['recipe_count'] == 1
    assert len(sha256(path)) == 64
    case['constraints']['max_minutes'] = 1
    with pytest.raises(ValueError, match='violates'):
        source_audit(path,[case])
    case['constraints'] = {}
    case['source_evidence']['x']['payload_sha256'] = 'changed'
    with pytest.raises(ValueError, match='Source changed'):
        source_audit(path,[case])
    case['expected_ids'] = ['missing']
    with pytest.raises(ValueError, match='Missing labeled'):
        source_audit(path,[case])


def test_frozen_cases_are_valid_and_source_grounded():
    import json
    from scripts.evaluate_recipe_search import BACKEND, sha256, source_audit
    path = BACKEND / 'data/recipe_eval_cases.jsonl'
    assert sha256(path) == 'e0a508e030d5ae094180a791c89faaf53c70b2b2aaa7945a4b13811ff96f5122'
    cases = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    validate_cases(cases)
    assert len(cases) == 40
    index = BACKEND / 'var/index/retrieval.sqlite'
    if not index.exists():
        # 레시피 색인은 전달 패키지에만 있다. 저장소 사본에서는 사례 형식까지만 확인한다.
        pytest.skip('레시피 색인(var/index/retrieval.sqlite)이 없어 원본 대조를 건너뜁니다.')
    assert source_audit(index, cases)['recipe_count'] == 544888


def test_candidate_hit_is_separate_from_final_hit():
    case = {'case_id':'x','category':'semantic','expected_ids':['wanted']}
    row = score_case(case,{'items':[], 'diagnostics':{'ranking':[{'activity_id':'wanted','passed':False}]}},1)
    assert row['rerank_candidate_hit'] is True
    assert row['hit_at_5'] is False


def test_missing_dense_is_rejected():
    service = Fake()
    service._dense.pop('recipes')
    with pytest.raises(ValueError,match='Completed'):
        run_comparison(service,[])


def test_percentiles_empty_and_interpolation():
    from scripts.evaluate_recipe_search import percentile
    assert percentile([], .5) is None
    assert percentile([20,10],.95) == 19.5
    assert summarize([])['hit_at_5'] is None


def test_validation_positive_requires_evidence():
    case = {'case_id':'x','question':'q','category':'title','rationale':'r','expected_ids':['x']}
    with pytest.raises(ValueError, match='evidence'):
        validate_cases([case])
    case['source_evidence'] = {'x':{}}
    validate_cases([case])

def test_main_writes_evidence_with_fake_service(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from scripts import evaluate_recipe_search as module
    from app.config import Settings
    import app.retrieval
    case = {'case_id':'x','question':'q','category':'negative','rationale':'outside','expected_ids':[]}
    cases = [dict(case,case_id=str(i)) for i in range(40)]
    path = tmp_path / 'cases.jsonl'
    path.write_text('\n'.join(json.dumps(x) for x in cases),encoding='utf-8')
    settings = SimpleNamespace(index_dir=tmp_path)
    monkeypatch.setattr(Settings,'from_env',lambda:settings)
    monkeypatch.setattr(module,'source_audit',lambda *args:{'recipe_count':1})
    monkeypatch.setattr(module,'runtime_metadata',lambda *args:{'fake':True})
    service = Fake()
    service._encoder = object()
    service._reranker = object()
    service._dense['recipes'] = ([0],['id'])
    monkeypatch.setattr(app.retrieval,'Retrieval',lambda settings:service)
    (tmp_path / 'recipes.dense.json').write_text('{}')
    output = tmp_path / 'out.json'
    args = ['--cases',str(path),'--expected-cases-sha256',module.sha256(path),'--output',str(output)]
    module.main(args)
    report = json.loads(output.read_text())
    assert len(report['runs']) == 240
    assert report['cases_sha256'] == module.sha256(path)
    assert report['runtime']['fake']
    with pytest.raises(ValueError,match='hash mismatch'):
        module.main(['--cases',str(path),'--expected-cases-sha256','wrong','--output',str(output)])
    (tmp_path / 'recipes.build.lock').touch()
    with pytest.raises(RuntimeError,match='running'):
        module.main(args)
    (tmp_path / 'recipes.build.lock').unlink()
    service._dense['recipes'] = ([],[])
    with pytest.raises(RuntimeError,match='full corpus'):
        module.main(args)
    service._encoder = None
    with pytest.raises(RuntimeError,match='actual encoder'):
        module.main(args)
    path.write_text(json.dumps(case),encoding='utf-8')
    args[args.index('--expected-cases-sha256')+1] = module.sha256(path)
    with pytest.raises(ValueError,match='exactly 40'):
        module.main(args)

def test_inference_failure_refuses_fallback_and_restores_dense():
    service = Fake()
    original = service._dense['recipes']
    service.search = lambda *args,**kwargs: {'items':[], 'diagnostics':{'reranker_error':'failed'}}
    with pytest.raises(RuntimeError,match='fallback'):
        run_comparison(service,[{'question':'x'}])
    assert service._dense['recipes'] is original
