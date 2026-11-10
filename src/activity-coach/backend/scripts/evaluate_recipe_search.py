"""Frozen source-grounded recipe benchmark; local models only, single threaded."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import platform
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
WARNING = ('Agent-authored known-item labels, non-exhaustive and not expert validated. '
           'Other retrieved recipes may be valid; fixed-ID Hit/MRR are not recall. '
           'Repeated observations are not independent samples.')


def validate_cases(cases: list[dict]) -> None:
    seen = set()
    for case in cases:
        if (not all(case.get(k) for k in ('case_id', 'question', 'category', 'rationale'))
                or not isinstance(case.get('expected_ids'), list)
                or any(not isinstance(x, str) or not x for x in case['expected_ids'])
                or case['case_id'] in seen):
            raise ValueError('Cases require unique IDs, question, category, rationale and explicit expected_ids')
        if case['expected_ids'] and not case.get('source_evidence'):
            raise ValueError('Positive cases require frozen source evidence')
        seen.add(case['case_id'])


def constraint_violations(item: dict, constraints: dict) -> int:
    violations = 0
    fields = {'max_minutes': item.get('duration', {}).get('total_minutes'),
              'max_difficulty': item.get('difficulty'), 'budget_max': item.get('budget')}
    for key, value in fields.items():
        if constraints.get(key) is not None:
            violations += value is None or value > constraints[key]
    if constraints.get('available_tools') is not None or constraints.get('excluded_tools'):
        from app.retrieval import tools_match, excluded_tools_match
        flags = item.get('quality', {}).get('flags', [])
        if 'tools_unknown' in flags or not item.get('tools'):
            violations += 1
        else:
            tools = json.dumps(item['tools'])
            if constraints.get('available_tools') is not None:
                violations += not tools_match(tools, json.dumps(constraints['available_tools']))
            if constraints.get('excluded_tools'):
                violations += not excluded_tools_match(tools, json.dumps(constraints['excluded_tools']))
    return int(violations)


def score_case(case: dict, result: dict, latency_ms: float) -> dict:
    items = result['items'][:5]
    ids = [item['activity_id'] for item in items]
    expected = set(case['expected_ids'])
    ranks = [i for i, identifier in enumerate(ids, 1) if identifier in expected]
    return {'case_id': case['case_id'], 'category': case['category'],
            'question': case.get('question'), 'constraints': case.get('constraints', {}),
            'expected_ids': case['expected_ids'], 'retrieved_ids': ids,
            'hit_at_1': bool(ids and ids[0] in expected) if expected else None,
            'hit_at_5': bool(ranks) if expected else None,
            'mrr_at_5': (1 / min(ranks) if ranks else 0) if expected else None,
            'false_retrieval': bool(ids) if not expected else None,
            'rerank_candidate_hit': bool(expected.intersection(
                r['activity_id'] for r in result['diagnostics'].get('ranking', []))) if expected else None,
            'constraint_violations': sum(constraint_violations(i, case.get('constraints', {})) for i in items),
            'latency_ms': latency_ms, 'items': items, 'diagnostics': result['diagnostics']}


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def summarize(rows: list[dict]) -> dict:
    def average(key: str) -> float | None:
        values = [row[key] for row in rows if row[key] is not None]
        return sum(values) / len(values) if values else None
    latencies = [row['latency_ms'] for row in rows]
    return {'observations': len(rows), 'positive_observations': sum(r['hit_at_5'] is not None for r in rows),
            'negative_observations': sum(r['false_retrieval'] is not None for r in rows),
            'hit_at_1': average('hit_at_1'), 'hit_at_5': average('hit_at_5'),
            'mrr_at_5': average('mrr_at_5'), 'rerank_candidate_hit_rate': average('rerank_candidate_hit'),
            'negative_false_retrieval_rate': average('false_retrieval'),
            'constraint_violations': sum(r['constraint_violations'] for r in rows),
            'warm_p50_ms': percentile(latencies, .5), 'warm_p95_ms': percentile(latencies, .95),
            'searches_per_second': len(rows) * 1000 / sum(latencies) if sum(latencies) else None}


def run_comparison(service: Any, cases: list[dict]) -> dict:
    if 'recipes' not in service._dense:
        raise ValueError('Completed recipe dense index must be loaded for an honest comparison')
    dense = service._dense['recipes']
    rows, warmups = [], []
    def search(case: dict, mode: str) -> tuple[dict, float]:
        if mode == 'bm25':
            service._dense.pop('recipes', None)
        else:
            service._dense['recipes'] = dense
        started = time.perf_counter()
        result = service.search(case['question'], scope='recipes', hobby_key='cooking',
                                constraints=copy.deepcopy(case.get('constraints', {})), limit=5)
        elapsed = (time.perf_counter() - started) * 1000
        diagnostics = result['diagnostics']
        if diagnostics.get('reranker_error') or diagnostics.get('embedding_error'):
            raise RuntimeError('Model inference failed; refusing a fallback comparison')
        return result, elapsed
    try:
        for mode in ('bm25', 'hybrid'):
            _, elapsed = search(cases[0], mode)
            warmups.append({'mode': mode, 'latency_ms': elapsed})
        for repeat in range(3):
            for case in cases:
                modes = ('bm25', 'hybrid') if repeat % 2 == 0 else ('hybrid', 'bm25')
                for mode in modes:
                    result, elapsed = search(case, mode)
                    rows.append(dict(score_case(case, result, elapsed), mode=mode, repeat=repeat + 1))
                    print(json.dumps({'repeat': repeat + 1, 'case_id': case['case_id'],
                                      'mode': mode, 'latency_ms': round(elapsed, 2)}), flush=True)
    finally:
        service._dense['recipes'] = dense
    summary = {}
    for mode in ('bm25', 'hybrid'):
        selected = [r for r in rows if r['mode'] == mode]
        summary[mode] = dict(summarize(selected), categories={category: summarize([
            r for r in selected if r['category'] == category]) for category in sorted({r['category'] for r in selected})})
    return {'warning': WARNING, 'top_k': 5, 'repeats': 3, 'warmups': warmups,
            'cache_protocol': 'One warmup per mode; same process, shared loaded models; OS cache not flushed. '
                              'Paired per case, mode order reversed on repeat 2; no concurrent requests.',
            'summary': summary, 'runs': rows,
            'repeat_consistency': {mode: {case['case_id']: len({
                tuple(r['retrieved_ids']) for r in rows if r['mode'] == mode and r['case_id'] == case['case_id']}) == 1
                for case in cases} for mode in ('bm25', 'hybrid')}}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def source_audit(database: Path, cases: list[dict]) -> dict:
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        count = db.execute("SELECT count(*) FROM records WHERE scope='recipes'").fetchone()[0]
        metadata = db.execute("SELECT value FROM metadata WHERE key='index:recipes'").fetchone()
        known = db.execute("SELECT sum(tools_known),count(difficulty),count(budget) FROM records WHERE scope='recipes'").fetchone()
        if tuple(known) != (0, 0, 0):
            raise ValueError('Missing-metadata negative labels require a new source audit')
        for case in cases:
            for identifier in case['expected_ids']:
                row = db.execute("SELECT payload FROM records WHERE id=? AND scope='recipes'", (identifier,)).fetchone()
                if row is None:
                    raise ValueError(f'Missing labeled recipe: {identifier}')
                payload = json.loads(row[0])
                evidence = case['source_evidence'][identifier]
                if hashlib.sha256(row[0].encode()).hexdigest() != evidence['payload_sha256']:
                    raise ValueError(f'Source changed for labeled recipe: {identifier}')
                if constraint_violations(payload, case.get('constraints', {})):
                    raise ValueError(f'Expected recipe violates constraints: {identifier}')
    return {'recipe_count': count, 'index_metadata': json.loads(metadata[0]) if metadata else None,
            'source_audit': 'All positive IDs, payload hashes and constraints verified before model loading'}


def runtime_metadata(service: Any, settings: Any) -> dict:
    import torch
    from app.recipe_dense import model_fingerprint
    models = {}
    for label, model in (('embedding', service._encoder), ('reranker', service._reranker)):
        inner = getattr(model, '_model', None)
        models[label] = {'name': getattr(model, 'name', None), 'fingerprint': model_fingerprint(model), 'device': str(getattr(inner, 'device', None)),
                         'max_seq_length': getattr(inner, 'max_seq_length', None),
                         'configured_max_length': getattr(model, 'max_length', None),
                         'configured_batch_size': getattr(model, 'batch_size', None)}
    return {'python': platform.python_version(), 'platform': platform.platform(), 'torch': torch.__version__,
            'cuda_available': torch.cuda.is_available(),
            'gpu': torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            'gpu_peak_allocated_bytes': torch.cuda.max_memory_allocated() if torch.cuda.is_available() else None,
            'models': models, 'settings': {key: getattr(settings, key) for key in (
                'device', 'rerank_candidates', 'dense_min_similarity', 'rerank_min_score',
                'embedding_enabled', 'reranker_enabled')}, 'retrieval_status': service.status()}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=BACKEND / 'data/recipe_eval_cases.jsonl')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-cases-sha256', required=True)
    args = parser.parse_args(argv)
    case_hash = sha256(args.cases)
    if case_hash != args.expected_cases_sha256:
        raise ValueError('Frozen case hash mismatch; do not revise labels after looking at results')
    cases = [json.loads(line) for line in args.cases.read_text(encoding='utf-8').splitlines() if line.strip()]
    validate_cases(cases)
    if len(cases) != 40:
        raise ValueError('Approved benchmark requires exactly 40 frozen cases')
    from app.config import Settings
    from app.retrieval import Retrieval
    settings = Settings.from_env()
    if (settings.index_dir / 'recipes.build.lock').exists():
        raise RuntimeError('Recipe build is running; do not compete for GPU resources')
    audit = source_audit(settings.index_dir / 'retrieval.sqlite', cases)
    started = time.perf_counter()
    service = Retrieval(settings)
    cold_loading_ms = (time.perf_counter() - started) * 1000
    if service._encoder is None or service._reranker is None:
        raise RuntimeError('Both actual encoder and reranker required; fallback benchmark refused')
    matrix, ids = service._dense.get('recipes', ([], []))
    if len(ids) != audit['recipe_count'] or len(matrix) != audit['recipe_count']:
        raise RuntimeError('Recipe dense index does not cover full corpus')
    report = run_comparison(service, cases)
    report.update(created_at=datetime.now(timezone.utc).isoformat(), cases_sha256=case_hash,
                  cases=cases, corpus=audit, cold_loading_ms=cold_loading_ms,
                  runtime=runtime_metadata(service, settings))
    manifest_path = settings.index_dir / 'recipes.dense.json'
    report['dense_manifest'] = json.loads(manifest_path.read_text(encoding='utf-8'))
    report['dense_manifest_sha256'] = sha256(manifest_path)
    report['index_files'] = [{'name': str(p.relative_to(settings.index_dir)), 'bytes': p.stat().st_size}
                             for p in settings.index_dir.rglob('*') if p.is_file() and 'recipe' in str(p)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'summary': report['summary']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
