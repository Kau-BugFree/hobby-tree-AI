"""User-visible limits and provenance, derived from stored decisions."""
from uuid import uuid4

from .dialogue_models import DialogueState, RecommendationSnapshot, TurnInterpretation
from .dialogue_state import apply_transition

STOP_MESSAGES = {
    'missing_observation':'사용자가 관찰한 상태가 더 필요해요. 아직 확인되지 않은 부분을 알려주세요.',
    'missing_knowledge':'현재 자료에 없는 부분은 확정할 수 없어요. 확인 가능한 원문을 구분해 안내합니다.',
    'conflicting_evidence':'근거가 서로 맞지 않아 단정할 수 없어요. 적용 조건을 확인해 주세요.',
    'ambiguous_intent':'어느 문제나 관찰을 뜻하는지 확인이 필요해요. 바꾸려는 내용을 구체적으로 알려주세요.',
    'execution_failure':'요청을 처리하는 중 오류가 발생했어요. 관찰은 보관했으니 다시 요청할 수 있어요.',
    'budget_exhausted':'이번 응답의 처리 한도에 도달했어요. 확인한 부분을 남기고 중단합니다.',
    'context_limit':'한 번에 처리할 맥락의 한도에 도달했어요. 기존 관찰은 보존했으니 새 문제의 범위를 명시해 주세요.',
}


def stop_message(reason: str, partial_reference: str | None = None) -> str:
    return STOP_MESSAGES.get(reason, STOP_MESSAGES['execution_failure']) + ('\n\n'+partial_reference if partial_reference else '')


def recommendation_reason(snapshot, current_constraints: dict, activities: list[dict]) -> str:
    if not snapshot:
        return ('아직 추천한 활동이 없어요. ' if not activities else '') + '추천 당시의 검색 조건 기록이 없어 이유를 복원할 수 없어요. 새 추천을 요청하면 조건과 근거를 함께 남길게요.'
    snapshot=RecommendationSnapshot.model_validate(snapshot).model_dump()
    conditions=[]; constraints=snapshot['constraints']
    for name,label,unit in [('max_minutes','', '분 이내'),('max_difficulty','최대 난이도 ',''),('budget_max','최대 비용 ','원')]:
        if constraints.get(name) is not None:
            conditions.append(f'{label}{constraints[name]:g}{unit}')
    for name,label in [('available_tools','쓸 수 있는 도구'),('excluded_tools','제외한 도구')]:
        if constraints.get(name) is not None:
            conditions.append(label+': '+(', '.join(constraints[name]) or '없음'))
    prefix='현재 조건과 다른 이전 추천입니다. ' if constraints!=current_constraints else ''
    order='소요 시간순' if snapshot['search_mode']=='catalog_browse' else '기록된 검색 순위('+snapshot['search_mode']+')'
    return (prefix+f"'{snapshot['query']}' 검색 결과를 {order}으로 안내했어요. "
            +'적용한 조건: '+(', '.join(conditions) or '없음')+'. 맛 취향이나 실력 같은 개인 정보는 쓰지 않았어요.\n'
            +'\n'.join(a['title'] for a in activities if a['activity_id'] in snapshot['candidate_ids']))


def build_metacognition(state: DialogueState, evidence: list, stop_reason: str | None) -> dict:
    covered={c.requirement_id:c for c in state.coverage}
    return {'supported':[c.requirement_id for c in state.coverage if c.status=='supported'],
        'unresolved':[{'requirement_id':r.id,'text':r.text,'reason':covered[r.id].status if r.id in covered else 'unassessed'}
                      for r in state.requirements if r.id not in covered or covered[r.id].status!='supported'],
        'source_review':{e['evidence_id']:e['review_status'] for e in evidence},
        'stop_reason':stop_reason, 'next_action':STOP_MESSAGES.get(stop_reason,'원문과 사용자 관찰을 구분하여 확인하세요.')}


REVIEW_LABELS = {'source_only':'사람 검토 전 원문', 'reviewed':'검토된 자료'}
REASON_LABELS = {'missing_knowledge':'검토된 자료에 근거 없음', 'missing_observation':'관찰 정보 필요',
                 'conflicting':'근거 충돌', 'unassessed':'아직 판단하지 않음'}


def certainty_text(meta: dict, evidence: list[dict]) -> str:
    """'확실해?'에 저장된 판단 근거만으로 답한다. 새 판단이나 확신 수치를 만들지 않는다."""
    sources=[f"- {e['title']}: {REVIEW_LABELS.get(e['review_status'],e['review_status'])}" for e in evidence]
    unresolved=[f"- {r['text']}: {REASON_LABELS.get(r['reason'],r['reason'])}" for r in meta['unresolved']]
    return '\n'.join(['근거 자료와 검토 상태:', *(sources or ['- 선택한 활동의 근거 자료가 없어요.']),
                      f"원문으로 확인된 요청: {len(meta['supported'])}개",
                      '아직 확인하지 못한 요청:', *(unresolved or ['- 없음']),
                      '원문 참고 안내는 원인 확정이나 실력 판정이 아닙니다.'])


def reflection_response(state: DialogueState, activity: dict, text: str, message_id: str) -> dict:
    current=state.model_copy(deep=True)
    if not activity:
        return {'state':current,'mode':'reference','text':'활동을 먼저 선택하면 원문의 자기 점검 기준으로 관찰을 확인할 수 있어요.'}
    checks=activity.get('checks',[]) if activity else []
    criteria='\n'.join('- '+c for c in checks) or '자료에 자기 점검 기준이 없습니다.'
    pending=current.pending_question
    if pending and pending.get('purpose')=='reflection':
        if text.strip() == pending.get('request_text', '').strip():
            return {'state': current, 'mode': 'clarify', 'clarification': pending,
                    'text': '직접 수행을 보지 못해 실력 평가는 할 수 없어요. 원문의 자기 점검 기준:\n'
                            + criteria + '\n' + pending['text']}
        current,_=apply_transition(current,TurnInterpretation(route='reflection',transition='continue'),
                                   message_id,text,current.activity_id,current.execution_id)
        current.outcome={'quote':text,'source_message_id':message_id,'kind':'self_report'}
        return {'state':current,'mode':'reference', 'text':f'사용자 보고: {text}\n원문 기준:\n{criteria}\n확인되지 않은 부분: 실제 수행과 결과를 직접 관찰하지 않았습니다.\n다음 시도는 사용자가 선택할 수 있어요.'}
    if current.reflection_count or not checks:
        return {'state':current,'mode':'reference','text':'원문 기준:\n'+criteria+'\n실력이나 다음 난이도 진입 여부를 확정할 수 없어요.'}
    question=checks[0]+' — 실제로 관찰한 상태를 알려주세요. 모르겠으면 모르겠다고 답해도 돼요.'
    current.pending_question={'question_id':str(uuid4()),'text':question,'options':['예','아니요','모르겠어요'],
                              'purpose':'reflection','accepts_free_text':True,'request_text':text}
    current.reflection_count=1
    if current.phase not in {'resolved','abandoned'}:
        current.phase='awaiting_observation'
    return {'state':current,'mode':'clarify','clarification':current.pending_question,
            'text':'직접 수행을 보지 못해 실력 평가는 할 수 없어요. 원문의 자기 점검 기준:\n'+criteria+'\n'+question}
