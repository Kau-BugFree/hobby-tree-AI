"""Run actual local models and a keyless API flow; never call a paid API."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time

PACKAGE = Path(__file__).resolve().parents[2]


def check_health(health: dict) -> None:
    """Fail unless the keyless server is healthy. Recipe vectors are optional: they load only on the
    fp16 (GPU) setup they were built for, and recipe search falls back to BM25 elsewhere."""
    assert health['version'] == '3.0.0' and not health['llm_configured'], health
    retrieval = health['retrieval']
    assert retrieval['documents'] == {'activities': 162, 'knowledge': 486, 'recipes': 544888}, retrieval
    assert not retrieval['embedding_error'] and not set(retrieval['index_errors']) - {'recipes'}, retrieval
    assert {'activities', 'knowledge'} <= set(retrieval['dense_index_scopes']), retrieval


def main() -> None:
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HOME=str(PACKAGE/'backend/var/hf-cache'))
    from fastapi.testclient import TestClient
    import torch
    from app.config import Settings
    from app.main import create_app

    assert Path(sys.prefix).resolve().is_relative_to(PACKAGE), 'Use the bundled Python'
    started = time.perf_counter()
    temporary_root = PACKAGE/'backend/var'
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='smoke-', dir=temporary_root) as directory:
        settings = Settings(anthropic_api_key='', database_path=Path(directory)/'coach.sqlite')
        print('Loading packaged models and index...', flush=True)
        app = create_app(settings)
        # The server accepts only its allowed Host names; call it the way a local browser does.
        with TestClient(app, base_url='http://127.0.0.1:8003') as client:
            health = client.get('/health').json()
            check_health(health)
            searches = {}
            for scope, query in [('activities', '간장 드레싱'), ('knowledge', '간장 드레싱 계량'),
                                 ('recipes', 'potato soup')]:
                response = client.post('/search', json={'query': query, 'scope': scope, 'limit': 3})
                assert response.status_code == 200, response.text
                found = response.json()
                assert found['items'], (scope, found)
                assert not found['diagnostics'].get('embedding_error'), found
                assert not found['diagnostics'].get('reranker_error'), found
                searches[scope] = found
                print(f'{scope}: {len(found["items"])} results', flush=True)
            assert app.state.retrieval._encoder._model is not None
            assert app.state.retrieval._reranker._model is not None
            sid = client.post('/sessions', json={'hobby_key': 'cooking'}).json()['session_id']
            response = client.post('/coach/messages', json={'session_id': sid, 'request_id': 'smoke-1',
                                   'message': '오늘 20분 안에 할 요리 활동 추천해줘'})
            assert response.status_code == 200, response.text
            recommended = response.json()
            assert len(recommended['activities']) >= 2 and recommended['diagnostics']['llm_calls'] == 0
            response = client.post('/coach/messages', json={'session_id': sid, 'request_id': 'smoke-2',
                                   'message': '두 번째 걸로 할게'})
            assert response.status_code == 200, response.text
            assert response.json()['selected_activity_id'] == recommended['activities'][1]['activity_id']
            body = {'session_id': sid, 'request_id': 'smoke-3', 'message': '준비물이 뭐야?'}
            first = client.post('/coach/messages', json=body).json()
            replay = client.post('/coach/messages', json=body).json()
            assert '준비물:' in first['text'] and first['diagnostics']['llm_calls'] == 0
            assert first['message_id'] == replay['message_id'] and replay['diagnostics']['replayed']
            response = client.post('/rag/ask', json={'question': '간장 드레싱 준비물이 뭐야?'})
            assert response.status_code == 200, response.text
            ask = response.json()
            assert ask['evidence'] and ask['llm']['calls'] == 0, ask
            schema = client.get('/openapi.json').json()
            assert schema['info']['version'] == '3.0.0'
            # backend/var is outside the manifest: a smoke run must not make verify_package report changed files.
            output = temporary_root/'smoke-report'
            output.mkdir(exist_ok=True)
            (output/'openapi.json').write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding='utf-8')
            report = {'status': 'passed', 'python': sys.version, 'torch': torch.__version__,
                      'cuda_available': torch.cuda.is_available(), 'health': health,
                      'searches': {scope: {'count': len(found['items']), 'diagnostics': found['diagnostics']}
                                   for scope, found in searches.items()},
                      'checks': ['local_embedding', 'local_reranker', 'three_search_scopes',
                                 'keyless_rag', 'multiturn_selection', 'request_replay', 'openapi'],
                      'paid_calls': 0, 'elapsed_seconds': round(time.perf_counter()-started, 2)}
            (output/'local-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'status': 'passed', 'cuda': report['cuda_available'],
                              'elapsed_seconds': report['elapsed_seconds'], 'paid_calls': 0}), flush=True)


if __name__ == '__main__':
    main()
