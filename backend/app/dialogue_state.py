"""Pure issue transitions; user quotations are the only source of observations."""
import json
import re
from uuid import UUID, uuid4, uuid5

from .dialogue_models import DialogueState, Observation, Requirement, TurnInterpretation

LEGACY_NAMESPACE = UUID('971586e1-264c-4f1f-a045-59ff8a8b59c2')
PHASE_STATUS = {'idle': 'idle', 'open': 'active', 'awaiting_observation': 'awaiting_clarification',
                'answer_provided': 'answered', 'resolved': 'answered', 'abandoned': 'idle'}
NEW_ISSUE = re.compile(r'((?:새|새로운)\s*문제|다른\s*문제|다른\s*부분)')
RESOLVED = re.compile(r'(해결됐|해결되었|문제.*해결했)')
ABANDON = re.compile(r'(문제.*그만|문제.*중단)')
UNKNOWN = re.compile(r'(모르|확인.*못|기억.*안)')


def new_issue_confirmed(state: DialogueState, text: str, quote: str = '') -> bool:
    if (state.pending_question or {}).get('purpose') == 'transition':
        if re.fullmatch(r'예|네|응|맞아|맞아요|1번?', text.strip()):
            return True
    return (bool(NEW_ISSUE.search(text)) and quote in text
            and not re.search(r'아니|않|하지\s*마|말아',text)
            and not re.search(r'문제(?:인가|일까|야\s*[?？]|예요\s*[?？])',text))


def normalize_dialogue(payload: dict, session: dict) -> DialogueState:
    from .store import StoreError
    version = payload.get('version', 1)
    if version not in (1, 2):
        raise StoreError(409, '지원하지 않는 dialogue version입니다. 원본 상태를 보존했습니다.')
    if version == 2:
        clean = {k: v for k, v in payload.items() if k in DialogueState.model_fields}
        # Compatibility projections are not a second source of truth.
        clean['observations'] = [{k: v for k, v in o.items() if k in Observation.model_fields}
                                 for o in clean.get('observations', [])]
        return DialogueState.model_validate(clean)
    phase = {'awaiting_clarification': 'awaiting_observation', 'answered': 'answer_provided',
             'insufficient_evidence': 'open', 'active': 'open'}.get(payload.get('status'), 'idle')
    issue_id = str(uuid5(LEGACY_NAMESPACE, session['session_id'])) if phase != 'idle' or payload.get('observations') else None
    observations = [Observation(id=str(uuid5(LEGACY_NAMESPACE, f'{issue_id}:{i}')),
        source_message_id=o.get('source_message_id'), quote=o.get('raw_text', o.get('answer', '')),
        question_snapshot={'question_id': None, 'text': o['question'], 'options': [], 'purpose': 'clarification'} if o.get('question') else None,
        answer_text=o.get('answer'), provenance='legacy') for i, o in enumerate(payload.get('observations', []))]
    return DialogueState(issue_id=issue_id, phase=phase, activity_id=session.get('selected_activity_id'),
        execution_id=session.get('execution_id'), observations=observations,
        pending_question=payload.get('pending_question'), clarification_count=payload.get('clarification_count', 0),
        evidence_ids=payload.get('evidence_ids', []), asked_questions=payload.get('asked_questions', []),
        hop_trace=payload.get('hop_trace', []), step_number=payload.get('step_number'))


def serialize_dialogue(state: DialogueState) -> dict:
    result = state.model_dump()
    result['status'] = PHASE_STATUS[state.phase]
    for item in result['observations']:
        item.update(raw_text=item['quote'], answer=item['answer_text'] or item['quote'],
                    question=(item['question_snapshot'] or {}).get('text'))
    return result


def apply_transition(state: DialogueState, interpretation: TurnInterpretation, message_id: str,
                     text: str, activity_id: str | None, execution_id: str | None) -> tuple[DialogueState, list[DialogueState]]:
    current = state.model_copy(deep=True)
    if interpretation.route not in {'dialogue', 'reflection'}:
        return current, []
    archived = []
    explicit_new = interpretation.transition == 'new_issue' and new_issue_confirmed(state,text,interpretation.transition_quote)
    consent = explicit_new and (state.pending_question or {}).get('purpose') == 'transition' and bool(re.fullmatch(r'예|네|응|맞아|맞아요|1번?',text.strip()))
    changed = state.issue_id and (state.activity_id is not None and state.activity_id != activity_id
                                  or state.execution_id is not None and state.execution_id != execution_id)
    if explicit_new or changed:
        if current.issue_id:
            if current.phase not in {'resolved', 'abandoned'}:
                current.phase = 'abandoned'
            archived.append(current)
        current = DialogueState()
    if not current.issue_id:
        current.issue_id, current.phase = str(uuid4()), 'open'
        proposal = state.pending_question or {}
        current.goal = proposal.get('proposed_goal',text)[:1000] if consent else text[:1000]
        if consent:
            original=proposal['proposed_goal']
            prior=TurnInterpretation.model_validate(proposal.get('proposed_interpretation',{'route':'dialogue'}))
            current.observations=[Observation(id=str(uuid4()),quote=original,answer_text=original,
                source_message_id=proposal.get('source_message_id'),status='unknown' if UNKNOWN.search(original) else 'reported')]
            current.requirements=[Requirement(id=str(uuid4()),text=r.text,kind=r.kind,source_message_id=proposal.get('source_message_id'))
                                  for r in prior.requirement_proposals if r.source_quote in original]
            if not current.requirements:
                current.requirements=[Requirement(id=str(uuid4()),text=current.goal,
                    kind='cause' if '왜' in original else 'remedy' if '어떻게' in original else 'lookup',
                    source_message_id=proposal.get('source_message_id'))]
    current.activity_id, current.execution_id = activity_id, execution_id
    # A self-assessment request is not an observation, nor an answer to a pending question.
    if interpretation.route == 'reflection' and (current.pending_question or {}).get('purpose') != 'reflection':
        return current, archived
    # Closed issues need explicit reopening; reflection can still read their outcome.
    if current.phase in {'resolved', 'abandoned'} and interpretation.route != 'reflection':
        return current, archived
    if interpretation.transition == 'resolve' and RESOLVED.search(text) and not re.search(r'않|아직|안\s*해결|[?？]', text):
        current.phase, current.pending_question = 'resolved', None
        current.outcome = {'quote': text, 'source_message_id': message_id, 'kind': 'self_report'}
        return current, archived
    if interpretation.transition == 'abandon' and ABANDON.search(text) and not re.search(r'아니|않|하지\s*마|말아|[?？]',text):
        current.phase, current.pending_question = 'abandoned', None
        return current, archived
    pending = state.pending_question if consent else current.pending_question if interpretation.transition != 'correct' else None
    updates = [u for u in interpretation.observation_updates if u.quote in text]
    if pending or not updates and interpretation.transition != 'correct':
        from .dialogue_models import ObservationUpdate
        updates = [ObservationUpdate(quote=text, status='unknown' if UNKNOWN.search(text) else 'reported')]
    if len(current.observations) + len(updates) > 64:
        raise ValueError('context_limit')
    for update in updates:
        valid_ids = {o.id for o in current.observations if o.status != 'superseded'}
        targets = update.supersedes if interpretation.transition == 'correct' and set(update.supersedes) <= valid_ids else []
        if interpretation.transition == 'correct' and not targets:
            continue  # An ambiguous correction cannot silently add a conflicting fact.
        for old in current.observations:
            if old.id in targets:
                old.status = 'superseded'
        index, answer, provenance = None, update.quote, 'user_quote'
        if pending:
            options = pending.get('options', [])
            match = re.fullmatch(r'\s*(\d+)\s*번?(?:으로\s*할게)?[.!]?\s*', text)
            if match and 1 <= int(match[1]) <= len(options):
                index = int(match[1]) - 1
            elif re.fullmatch(r'응|네|예|맞아|맞아요', text) and '예' in options:
                index = options.index('예')
            elif re.fullmatch(r'아니|아니요', text) and '아니요' in options:
                index = options.index('아니요')
            if index is not None:
                answer, provenance = options[index], 'server_option'
        current.observations.append(Observation(id=str(uuid4()), source_message_id=message_id, quote=update.quote,
            status='unknown' if UNKNOWN.search(answer) else update.status, supersedes=targets,
            question_snapshot=pending, selected_option_index=index, answer_text=answer, provenance=provenance))
    if updates:
        current.coverage = []  # Changed observations invalidate earlier applicability/coverage judgments.
    if interpretation.transition != 'correct':
        current.pending_question = None
    for proposal in interpretation.requirement_proposals:
        if proposal.source_quote in text and proposal.text not in {r.text for r in current.requirements}:
            if len(current.requirements) >= 8:
                raise ValueError('context_limit')
            current.requirements.append(Requirement(id=str(uuid4()), text=proposal.text, kind=proposal.kind, source_message_id=message_id))
    if current.phase not in {'resolved', 'abandoned'}:
        current.phase = 'awaiting_observation' if current.pending_question else 'open'
    return current, archived


def build_issue_context(state: DialogueState, session: dict) -> dict:
    dialogue = serialize_dialogue(state)
    dialogue['observations'] = [o for o in dialogue['observations'] if o['status'] != 'superseded']
    context = {'selected_activity_id': state.activity_id or session.get('selected_activity_id'),
        'allowed_activity_ids': [state.activity_id or session.get('selected_activity_id')],
        'current_step': state.step_number or session.get('current_step', 1),
        'constraints': session.get('constraints', {}), 'dialogue': dialogue}
    # Only consecutive turns from this activity, execution and issue can resolve follow-up references.
    history = session.get('messages', [])
    recent = []
    boundary = {'selected_activity_id': context['selected_activity_id'],
                'execution_id': session.get('execution_id'), 'issue_id': state.issue_id}
    for index in range(len(history) - 2, max(-1, len(history) - 6), -2):
        user, assistant = history[index:index + 2]
        if (user.get('role') != 'user' or assistant.get('role') != 'assistant'
                or session.get('context_after_message_id') and assistant.get('message_id') == session['context_after_message_id']
                or any(assistant.get('response', {}).get(key) != value for key, value in boundary.items())):
            break
        recent[:0] = [{'role': entry['role'], 'text': entry['text']} for entry in (user, assistant)]
    context['recent_messages'] = recent
    if len(json.dumps(context, ensure_ascii=False)) > 48000:
        raise ValueError('context_limit')
    return context


def archive_issue(session: dict) -> None:
    state = normalize_dialogue(session.get('dialogue', {}), session)
    if state.issue_id:
        if state.phase not in {'resolved', 'abandoned'}:
            state.phase = 'abandoned'
        session.setdefault('_issue_snapshots', []).append(serialize_dialogue(state))
    session['dialogue'] = serialize_dialogue(DialogueState())
