"""Shared reference, factual-summary and reviewed-claim answer policy."""
import asyncio
import json
import re

from .dialogue_models import AnswerClaim, Evidence, RequirementCoverage
from .claude import validate_answer
from .turn_budget import BudgetExceeded

# 공급자 쪽 실패 상태. 모델이 답을 판단한 결과가 아니므로 다시 호출하지 않고 원문 안내를 유지한다.
PROVIDER_FAILURES = frozenset({'timeout', 'network_error', 'rate_limited', 'authentication_error',
                               'provider_error', 'blocked', 'truncated', 'context_too_long'})


def knowledge_evidence(row: dict, activity_id: str) -> dict:
    return Evidence(evidence_id=row['knowledge_id'], activity_id=activity_id, title=row['title'],
        review_status=row['review_status'], kind=row['kind'], excerpt=row['content'],
        source_refs=row.get('source_refs', []), applies_when=row.get('applies_when', '')).model_dump()


class AnswerPolicy:
    @staticmethod
    def assess_coverage(requirements: list, coverage: list, rows: list, observations=()) -> list[RequirementCoverage]:
        """Only policy-normalized coverage may be persisted or exposed to users."""
        coverage = {c.requirement_id:c for c in (RequirementCoverage.model_validate(
            c.model_dump() if hasattr(c,'model_dump') else c) for c in coverage)}
        by_id = {e['evidence_id']:e for e in rows}
        observations = [o.model_dump() if hasattr(o,'model_dump') else o for o in observations]
        reported = {o.get('answer_text') or o.get('quote','') for o in observations
                    if o.get('status')=='reported' and o.get('provenance')!='legacy'}
        result = []
        for requirement in requirements:
            item = requirement.model_dump() if hasattr(requirement, 'model_dump') else requirement
            c = coverage.get(item['id'], RequirementCoverage(requirement_id=item['id'])).model_copy(deep=True)
            status = c.status
            cited = [by_id[k] for k in c.evidence_ids if k in by_id]
            if status == 'supported':
                if (not c.quotes or not c.evidence_ids or not set(c.evidence_ids)<=by_id.keys()
                        or {q.evidence_id for q in c.quotes}!=set(c.evidence_ids)
                        or any(q.quote not in by_id[q.evidence_id]['excerpt'] for q in c.quotes)):
                    status = 'unassessed'
                elif any(e['review_status']=='reviewed' and e['applies_when'] and e['applies_when'] not in reported for e in cited):
                    status = 'missing_observation'
            relevant = cited if c.status=='supported' else rows
            if (item['kind'] in {'cause','remedy','assessment'} and status!='conflicting'
                    and (not any(e['review_status']=='reviewed' for e in relevant)
                         or c.status=='supported' and any(e['review_status']!='reviewed' for e in cited))):
                status = 'missing_knowledge'
            c.status = status
            result.append(c)
        return result

    @staticmethod
    def _focus_lines(rows: list, focus) -> dict:
        """Source lines touched by the planner's quotes. A quote that is not exact source text is ignored."""
        by_id = {e['evidence_id']: e for e in rows}
        spans = {}
        for item in focus:
            item = item.model_dump() if hasattr(item, 'model_dump') else item
            quote = item.get('quote') or ''
            start = by_id.get(item.get('evidence_id'), {}).get('excerpt', '').find(quote) if quote.strip() else -1
            if start >= 0:
                spans.setdefault(item['evidence_id'], []).append((start, start + len(quote)))
        lines = {}
        for key, ranges in spans.items():
            offset = 0
            for line in by_id[key]['excerpt'].split('\n'):
                if line.strip() and any(offset < end and start < offset + len(line) for start, end in ranges):
                    lines.setdefault(key, []).append(line.strip())
                offset += len(line) + 1
        return lines

    def prepare(self, question: str, evidence: list, context: dict, requirements: list, focus=()) -> dict:
        rows = [Evidence.model_validate(e.model_dump() if isinstance(e, Evidence) else e).model_dump() for e in evidence]
        rows = [e for e in rows if e['review_status'] != 'draft' and e['activity_id'] in context.get('allowed_activity_ids', [])]
        dialogue = context.get('dialogue', {})
        coverage = self.assess_coverage(requirements, dialogue.get('coverage', []), rows, dialogue.get('observations', []))
        texts = {r.id:r.text for r in requirements if hasattr(r,'id')}
        texts.update({r['id']:r['text'] for r in requirements if isinstance(r,dict)})
        covered = [c.requirement_id for c in coverage if c.status=='supported']
        unresolved = [{'requirement_id':c.requirement_id,'text':texts[c.requirement_id],'reason':c.status}
                      for c in coverage if c.status!='supported']
        reason = None if rows else 'missing_knowledge'
        too_long = (len(rows)>5 or sum(len(e['excerpt']) for e in rows)>24000
                    or any(len(e['excerpt'])>6000 for e in rows)
                    or len(json.dumps({'question':question,'evidence':rows,'context':context},ensure_ascii=False))>48000)
        reported = [o.get('answer_text') or o.get('quote','') for o in context.get('dialogue',{}).get('observations',[])
                    if o.get('status') == 'reported' and o.get('provenance') != 'legacy']
        from .turn_router import coaching_requirements
        from .dialogue import is_trouble
        from .coach import SELF_ASSESSMENT
        kinds = ({r.kind if hasattr(r,'kind') else r['kind'] for r in requirements}
                 or {p['kind'] for p in coaching_requirements(question)})
        if is_trouble(question) or re.search(r'해결|대처|고치|고쳐|고칠|바로잡', question):
            kinds.add('remedy')
        if SELF_ASSESSMENT.search(question) or re.search(r'실력|숙련|진단', question):
            kinds.add('assessment')
        source_summary = bool(kinds) and kinds <= {'lookup', 'procedure'}
        original_requested = bool(re.search(
            r'원문(?:을|만|도)?\s*(?:(?:그대로\s*)?(?:보여|출력|읽어|줘)|그대로\s*[.!?]?$)', question)
            and not re.search(r'원문[^.!?]*(?:말고|말아|말라|않|요약|지\s*마)', question))
        conditions_unknown = any(e['applies_when'] and e['applies_when'] not in reported
                                 for e in rows if e['review_status']=='reviewed' or source_summary)
        if too_long:
            reason='context_limit'
        elif conditions_unknown:
            reason='missing_observation'
        elif any(c['reason']=='conflicting' for c in unresolved):
            reason='conflicting_evidence'
        return {'question':question, 'evidence':rows, 'covered':covered, 'unresolved':unresolved,
                'asks_assessment': 'assessment' in kinds, 'original_requested': original_requested,
                'asks_cause_or_remedy':bool(kinds & {'cause','remedy','assessment'}),
                # An explicit request for the original, or an assessment disclaimer, is never narrowed.
                'focus': {} if original_requested or 'assessment' in kinds else self._focus_lines(rows, focus),
                'stop_reason':reason, 'allow_generation':bool(rows) and not reason and not original_requested
                    and (all(e['review_status']=='reviewed' for e in rows)
                         or source_summary)}

    def render_reference(self, prepared: dict) -> dict:
        rows=prepared['evidence']
        # With planner-cited lines, show those lines only; the text stays verbatim source, never model wording.
        focus=prepared.get('focus') or {}
        shown=[e for e in rows if e['evidence_id'] in focus] if focus else rows
        if any(e['review_status']!='reviewed' for e in rows):
            # The cause disclaimer belongs to cause/remedy requests, not to plain lookups or comparisons.
            text=(('원인을 확정할 검토 자료는 없어요. ' if prepared.get('asks_cause_or_remedy') else '')
                  +('지금 상황과 관련된 원문 내용만 추려 안내합니다.' if focus else '다음은 기존 활동 원문의 참고 안내입니다.'))
        else:
            text='다음은 자료 원문의 참고 안내입니다. 개인 상태의 원인이나 실력은 확정하지 않아요.'
        if not rows:
            text='확인 가능한 자료가 없어 답변을 보류합니다.'
        if prepared['asks_assessment'] and not prepared['original_requested']:
            text = ('제공된 자료와 사용자 보고만으로는 실력·숙련도나 다음 난이도 진입 여부를 확정할 수 없어요. '
                    '실제 수행을 직접 관찰하지 않았습니다. 원문의 자기 점검 기준으로 확인한 점과 어려웠던 점을 알려주세요.')
        else:
            text += ''.join('\n\n'+e['title']+'\n'
                            +('\n'.join('- '+line for line in focus[e['evidence_id']]) if focus else e['excerpt'])
                            +('\n적용 조건: '+e['applies_when'] if e['applies_when'] else '') for e in shown)
            if focus:
                text += '\n\n전체 원문이 필요하면 “원문 보여줘”라고 요청해 주세요.'
        if prepared['unresolved']:
            text += '\n\n아직 확인하지 못한 요청: ' + '; '.join(r['text'] for r in prepared['unresolved'])
        return {'text':text, 'mode':'reference' if rows else 'insufficient_evidence', 'status':'reference' if rows else 'no_evidence',
                'sources':shown, 'cited_evidence_ids':[e['evidence_id'] for e in shown],
                'cited_activity_ids':list(dict.fromkeys(e['activity_id'] for e in shown)),
                'llm_calls':0, 'usage':{}, 'validation_errors':[],
                'validation':{'format':'not_run','citations':'not_run','quantities':'not_run','semantic_support':'not_run'},
                'metacognition':{'supported':prepared['covered'],'unresolved':prepared['unresolved'],
                    'source_review':{e['evidence_id']:e['review_status'] for e in rows},
                    'stop_reason':prepared['stop_reason'],'next_action':'원문 확인 또는 미확인 항목 설명'}}

    async def answer(self, question: str, evidence: list, context: dict, requirements: list, llm: object, budget,
                     focus=()) -> dict:
        prepared=self.prepare(question,evidence,context,requirements,focus)
        result=self.render_reference(prepared)
        if not prepared['allow_generation']:
            return result
        if not hasattr(llm, 'explain_claims') or not getattr(llm, 'configured', True):
            return result
        rows=prepared['evidence']; calls_before=budget.llm_calls; usage_before=dict(budget.usage)
        answer_context = {**context, 'question': question}
        errors=[]; validation=result['validation']; previous_claims=[]; previous_verdicts=[]
        unavailable=None  # 공급자 오류나 예산 소진으로 생성기가 답을 내지 못한 사유
        try:
            async with asyncio.timeout(budget.remaining_seconds()):
                for attempt in range(2):
                    generated=await budget.model_call('generation',llm.explain_claims,question,rows,
                        {**answer_context,'validation_errors':errors,'previous_claims':previous_claims,'previous_verdicts':previous_verdicts})
                    if generated.get('status') in PROVIDER_FAILURES:
                        unavailable=generated['status']; break
                    if not generated.get('claims') or len(generated['claims'])>8:
                        errors=['invalid_claims']; validation['format']='failed'; continue
                    try:
                        claims=[AnswerClaim.model_validate(c) for c in generated['claims']]
                    except ValueError:
                        errors=['invalid_claims']; validation['format']='failed'; continue
                    validation['format']='passed'
                    previous_claims=[c.model_dump() for c in claims]; previous_verdicts=[]
                    errors=self._validate_claims(claims,rows,validation)
                    if errors:
                        continue
                    validation['semantic_support']='failed'
                    checked=await budget.model_call('verification',llm.verify_claims,[c.model_dump() for c in claims],rows,answer_context)
                    if checked.get('status') in PROVIDER_FAILURES:
                        validation['semantic_support']='not_run'; unavailable=checked['status']; break
                    previous_verdicts=checked.get('verdicts') or []
                    if not self._supported(checked.get('verdicts'),claims):
                        validation['semantic_support']='failed'; errors=['semantic_support_failed']; continue
                    validation['semantic_support']='model_assessed'
                    result.update(mode='generated',status='generated_repaired' if attempt else 'generated',
                        text='\n'.join(c.text for c in claims),cited_evidence_ids=list(dict.fromkeys(k for c in claims for k in c.evidence_ids)))
                    by_id = {row['evidence_id']: row for row in rows}
                    result['cited_activity_ids'] = list(dict.fromkeys(
                        by_id[key]['activity_id'] for key in result['cited_evidence_ids']))
                    break
        except (BudgetExceeded, TimeoutError):
            result['metacognition']['stop_reason']='budget_exhausted'; unavailable='budget_exhausted'
        except (AttributeError, TypeError, ValueError):
            result['metacognition']['stop_reason']='execution_failure'
        result.update(llm_calls=budget.llm_calls-calls_before,
                      model=getattr(llm, 'model', None),
                      usage={k:v-usage_before.get(k,0) for k,v in budget.usage.items()},
                      validation_errors=[unavailable] if unavailable else errors)
        if result['mode']!='generated' and not result['metacognition']['stop_reason']:
            result['metacognition']['stop_reason']='execution_failure'
        if result['mode'] != 'generated' and unavailable:
            # 원문 안내는 키 없는 배포에서도 나가는 내용이다. 일시적인 공급자 오류 때문에 그보다 적게 주지 않는다.
            result['status'] = 'generation_unavailable'
            result['text'] = '설명을 생성하지 못해 자료 원문을 그대로 안내합니다.\n\n' + result['text']
        elif result['mode'] != 'generated':
            # 검증에서 떨어진 답은 내보내지 않는다. 보류한 응답이 답변·인용으로 집계되지 않게 상태와 인용을 비운다.
            result.update(status='withheld', cited_evidence_ids=[], cited_activity_ids=[],
                          text=('질문에 맞는 답변을 제공된 자료로 확인하지 못해 답변을 보류합니다. '
                                '확인되지 않은 조건을 확정할 수 없어요. 원문이 필요하면 “원문 보여줘”라고 요청해 주세요.'))
            result['metacognition']['next_action'] = '확인할 조건을 좁히거나 원문을 요청해 주세요.'
        return result

    @staticmethod
    def _validate_claims(claims: list, rows: list, validation: dict) -> list:
        by_id={e['evidence_id']:e for e in rows}
        errors=[]
        for claim in claims:
            if (not set(claim.evidence_ids)<=by_id.keys()
                or {q.evidence_id for q in claim.quotes}!=set(claim.evidence_ids)
                or any(q.quote not in by_id[q.evidence_id]['excerpt'] for q in claim.quotes)):
                errors.append('invalid_citation'); continue
            cited=[{**by_id[k],'activity_id':k} for k in claim.evidence_ids]
            errors.extend(validate_answer({'answer':claim.text,'cited_activity_ids':claim.evidence_ids},cited))
        validation['citations']='failed' if 'invalid_citation' in errors else 'passed'
        validation['quantities']='failed' if any(e!='invalid_citation' for e in errors) else 'passed'
        return errors

    @staticmethod
    def _supported(verdicts, claims) -> bool:
        if not isinstance(verdicts,list) or len(verdicts)!=len(claims):
            return False
        try:
            return ({v['claim_index'] for v in verdicts}==set(range(len(claims))) and
                all(v['verdict']=='supported' and v['evidence_ids'] and
                    set(v['evidence_ids'])<=set(claims[v['claim_index']].evidence_ids) for v in verdicts))
        except (KeyError,TypeError,IndexError):
            return False


def evidence_for(activity, step=None):
    """원문 URL 대신 정규화된 내부 활동 ID와 이번 답변의 근거만 전달합니다."""
    if step is not None:
        excerpt = f"{step}단계: {activity['steps'][step-1]}"
    else:
        lines = ["활동 요약: " + activity["description"]] if activity.get("description") else []
        lines.append("준비물: " + ", ".join(activity.get("materials", [])))
        # 시간 조건 질문에 답할 수 있도록 자료에 있는 전체 소요 시간을 근거에 넣는다. 없으면 추측하지 않는다.
        duration = activity.get("duration") or {}
        if duration.get("total_minutes") is not None:
            estimate = " (추정)" if duration.get("is_estimate") else ""
            lines.append(f"예상 소요 시간: {duration['total_minutes']:g}분{estimate}")
        lines += [f"{i+1}. {x}" for i, x in enumerate(activity.get("steps", []))]
        # 자기 점검·검토 여부를 묻는 메타인지 질문에도 근거로 답하도록 확인 항목과 검토 상태를 넣는다.
        if activity.get("checks"):
            lines.append("자기 점검: " + " / ".join(activity["checks"]))
        if activity.get("safety_notes"):
            lines.append("안전 안내: " + " / ".join(activity["safety_notes"]))
        reviewed = (activity.get("quality") or {}).get("human_reviewed")
        lines.append("검토 상태: " + ("사람 검토 완료" if reviewed else "사람 검토 전 자료(참고용)"))
        excerpt = "\n".join(lines)
    return {
        "activity_id": activity["activity_id"],
        "evidence_id": activity["activity_id"] + (f":step:{step}" if step else ":source"),
        "review_status": "source_only", "source_refs": [activity["activity_id"]], "applies_when": "",
        "title": activity["title"],
        "kind": activity["kind"],
        "excerpt": excerpt,
    }

