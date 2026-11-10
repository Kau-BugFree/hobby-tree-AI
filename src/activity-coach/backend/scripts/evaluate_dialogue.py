"""Frozen multi-turn evaluation. Default validates fixtures; --live opts into paid calls."""
import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import uuid4

BACKEND=Path(__file__).resolve().parents[1]
CATEGORIES={'followup','correction','transition','compound','provenance','reflection'}


def load_cases(path: Path) -> list[dict]:
    cases=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    required={'case_id','split','category','activity_id','turns','expected_state','required_points',
              'forbidden_claims','allowed_evidence_ids','expected_stop_reason'}
    seen=set()
    for case in cases:
        if not required<=case.keys() or case['case_id'] in seen:
            raise ValueError('Missing fields or duplicate case_id')
        if case['split'] not in {'dev','holdout'} or case['category'] not in CATEGORIES:
            raise ValueError('Invalid split/category')
        if not 2<=len(case['turns'])<=6 or not all(isinstance(t,str) and t.strip() for t in case['turns']):
            raise ValueError('Each case needs 2–6 nonempty turns')
        if not isinstance(case['expected_state'],dict) or not all(isinstance(case[k],list) for k in ['required_points','forbidden_claims','allowed_evidence_ids']):
            raise ValueError('Invalid expectations')
        seen.add(case['case_id'])
    return cases


def validate_splits(dev: list, holdout: list) -> None:
    seen_ids=set(); seen_turns=set()
    for split,cases in [('dev',dev),('holdout',holdout)]:
        for case in cases:
            key=json.dumps(case['turns'],ensure_ascii=False)
            if case['case_id'] in seen_ids or key in seen_turns or case['split']!=split:
                raise ValueError('Duplicate dialogue/case or wrong split')
            seen_ids.add(case['case_id']); seen_turns.add(key)
        if len(cases)!=30 or len({c['activity_id'] for c in cases})<10 or Counter(c['category'] for c in cases)!=dict.fromkeys(CATEGORIES,5):
            raise ValueError('Each split needs 30 cases, 10 activities, 5 cases per category')


def coverage_consistent(turn: dict) -> bool:
    state = turn.get('state', {}).get('dialogue')
    response = turn.get('response', {})
    meta = response.get('metacognition')
    if state is None or meta is None or response.get('diagnostics', {}).get('intent') in {'explain','activity_lookup'}:
        return True
    requirements = {r['id'] for r in state.get('requirements', [])}
    coverage = {c['requirement_id']:c['status'] for c in state.get('coverage', [])}
    supported = set(meta.get('supported', []))
    unresolved = {r['requirement_id']:r['reason'] for r in meta.get('unresolved', [])}
    return (not supported.intersection(unresolved) and supported | unresolved.keys() == requirements
            and supported == {k for k,v in coverage.items() if v=='supported'}
            and all(coverage.get(k,'unassessed')==v for k,v in unresolved.items()))


def score_dialogue(case: dict, transcript: list[dict]) -> dict:
    responses=[t['response'] for t in transcript]
    last=responses[-1] if responses else {}
    state=transcript[-1].get('state',{}) if transcript else {}
    text='\n'.join(r.get('text','') for r in responses)
    diagnostics=[r.get('diagnostics',{}) for r in responses if not r.get('diagnostics',{}).get('replayed')]
    unresolved=len(last.get('metacognition',{}).get('unresolved',[]))
    checks={'state':all(state.get(k)==v for k,v in case['expected_state'].items()),
        'citations':all(set(r.get('cited_evidence_ids',[]))<=set(case['allowed_evidence_ids']) for r in responses),
        'forbidden_claims':not any(phrase in text for phrase in case['forbidden_claims']),
        'stop_reason':case['expected_stop_reason'] is None or last.get('diagnostics',{}).get('stop_reason')==case['expected_stop_reason'],
        'budgets':all(all(d.get(k+'_calls',0)<=n for k,n in [('decision',6),('retrieval',3),('generation',2),('verification',2)]) for d in diagnostics),
        'coverage_consistency':all(coverage_consistent(t) for t in transcript),
        'compound_requirements':case['category']!='compound' or {'cause','remedy','procedure'} <=
            {r.get('kind') for r in state.get('dialogue',{}).get('requirements',[])}}
    usage=Counter()
    for d in diagnostics:
        usage.update(d.get('usage',{}))
    return {'case_id':case['case_id'],'checks':checks,'automatic_passed':all(checks.values()),
        'answered':last.get('mode') in {'reference','generated'},'generated':last.get('mode')=='generated',
        'unresolved_count':unresolved,'required_points':case['required_points'],
        'human_complete':None,'human_supported':None,'complete':False if unresolved or not all(checks.values()) else None,
        'llm_calls':sum(d.get('llm_calls',0) for d in diagnostics),'usage':dict(usage),
        'latency_ms':sum(d.get('latency_ms',0) for d in diagnostics),'transcript':transcript}


async def run_comparison(cases: list[dict], runner: object, repeats: int, mode: str) -> dict:
    if repeats<1 or mode not in {'bundle','agent'}:
        raise ValueError('Invalid repeats/mode')
    run_id=uuid4().hex; rows=[]
    for case in cases:
        for repeat in range(repeats):
            trial=run_id+'-'+uuid4().hex
            transcript=await runner(case,trial,mode)
            rows.append({**score_dialogue(case,transcript),'trial_id':trial,'repeat':repeat})
    latencies=sorted(r['latency_ms'] for r in rows)
    usage=Counter()
    for row in rows:
        usage.update(row['usage'])
    return {'run_id':run_id,'mode':mode,'attempts':len(rows),'cases':rows,
        'ungraded_count':len(rows),'human_complete_rate':None,'model_quality_passed':None,
        'llm_calls':sum(r['llm_calls'] for r in rows),'usage':dict(usage),
        'latency_ms':{'p50':latencies[len(latencies)//2] if latencies else None,
                      'p95':latencies[min(len(latencies)-1,int(len(latencies)*.95))] if latencies else None}}


def build_runner(output_dir: Path):
    from app.config import Settings
    from app.claude import ClaudeService
    from app.retrieval import Retrieval
    from app.store import Store
    from app.coach import Coach
    from app.models import CoachRequest
    settings=Settings.from_env()
    if not settings.anthropic_api_key:
        raise ValueError('Live evaluation requires this version’s ANTHROPIC_API_KEY')
    retrieval=Retrieval(settings); llm=ClaudeService(settings)
    async def runner(case,run_id,mode):
        store=Store(output_dir/run_id/'coach.sqlite')
        coach=Coach(store,retrieval,llm)
        coach.dialogue.allow_additional_retrieval=mode=='agent'
        session=store.create_session(); coach.select(session,case['activity_id']); store.save_session(session)
        transcript=[]
        for i,text in enumerate(case['turns']):
            reply=await coach.respond(CoachRequest(session_id=session['session_id'],request_id=f'{run_id}:{i}',message=text))
            transcript.append({'response':reply,'state':store.get_session(session['session_id'])})
        return transcript
    return runner,{'model':settings.claude_model,'effort':settings.claude_effort}


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=BACKEND/'data/dialogue_eval_dev.jsonl')
    parser.add_argument('--mode',choices=['bundle','agent'],default='bundle')
    parser.add_argument('--repeats',type=int,default=1)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args(argv); cases=load_cases(args.cases)
    validate_splits(load_cases(BACKEND/'data/dialogue_eval_dev.jsonl'),load_cases(BACKEND/'data/dialogue_eval_holdout.jsonl'))
    activities={json.loads(line)['activity_id'] for line in (BACKEND/'data/activities.jsonl').read_text(encoding='utf-8').splitlines()}
    knowledge={json.loads(line)['knowledge_id'] for line in (BACKEND/'data/coaching_knowledge.jsonl').read_text(encoding='utf-8').splitlines()}
    for case in cases:
        if case['activity_id'] not in activities or not set(case['allowed_evidence_ids'])<=knowledge|{case['activity_id']+':source'}:
            raise ValueError('Unknown activity/evidence')
    output=args.output or BACKEND/'var/eval'/f'dialogue_{uuid4().hex}.json'
    report={'kind':'schema_validation','case_count':len(cases),'model_quality_passed':None}
    if args.live:
        runner,metadata=build_runner(output.parent)
        report={**asyncio.run(run_comparison(cases,runner,args.repeats,args.mode)),**metadata,'kind':'live_ungraded'}
    files=[args.cases,*sorted((BACKEND/'app').glob('*.py')),*sorted((BACKEND/'data').glob('*.jsonl'))]
    report['sha256']={str(p.relative_to(BACKEND)) if p.is_relative_to(BACKEND) else p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"{report['kind']}: {len(cases)} cases; {output}")
    return 0


if __name__=='__main__':
    raise SystemExit(main())
