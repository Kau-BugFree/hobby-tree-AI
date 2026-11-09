"""Read-only intent interpretation; execution authority stays in Coach."""
import re

from .dialogue_models import DialogueState, TurnInterpretation
from .dialogue_state import ABANDON, NEW_ISSUE, RESOLVED, UNKNOWN, build_issue_context, new_issue_confirmed
from .turn_budget import TurnBudget


COACHING_PARTS = (
    ('cause', r'원인|이유|왜'),
    ('remedy', r'해결\s*(?:방법|책)?|대처\s*(?:방법|법)?|어떻게'),
    ('procedure', r'(?:원문\s*)?(?:(?:완료|확인|점검)\s*(?:확인\s*)?기준|절차|(?:작업\s*)?순서)'),
)
COACHING_REQUEST = re.compile('|'.join(pattern for _,pattern in COACHING_PARTS))


def coaching_requirements(text: str) -> list[dict]:
    """Extract explicit subrequests even when this turn also answers a question."""
    parts = []
    for kind, pattern in COACHING_PARTS:
        match = re.search(pattern, text)
        if match:
            parts.append({'text':match[0], 'kind':kind, 'source_quote':text})
    # "순서는 어떻게" asks for one procedure, not a separate remedy.
    if any(p['kind']=='procedure' for p in parts) and not re.search(r'해결|대처',text):
        parts=[p for p in parts if not (p['kind']=='remedy' and p['text']=='어떻게')]
    # A bare '어떻게' asks how to do something; it is a remedy only when fixing a reported problem.
    from .dialogue import is_trouble
    if not (re.search(r'해결|대처|고치|고쳐|고칠|바로잡',text) or is_trouble(text)):
        parts=[{**p,'kind':'procedure'} if p['kind']=='remedy' and p['text']=='어떻게' else p for p in parts]
    if len(parts)==1:
        parts[0]['text']=text[:1000]
    if not parts and re.search(r'알려|설명|[?？]|어려|어렵|안\s*돼|헷갈|막혔|막혀|실패',text):
        parts=[{'text':text[:1000], 'kind':'remedy' if re.search(r'어려|어렵|안\s*돼|헷갈|막혔|막혀|실패',text) else 'lookup', 'source_quote':text}]
    return parts


class TurnRouter:
    def __init__(self, llm):
        self.llm = llm

    async def interpret(self, text: str, session: dict, state: DialogueState, budget: TurnBudget) -> TurnInterpretation:
        from .coach import (ACTIVITY_LOOKUP, ASKS_FOR_DISHES, CAPABILITY, NEGATED_ACTION,
                            RECOMMEND_REASON, SELF_ASSESSMENT, INFO_REQUEST)
        from .dialogue import is_trouble
        if NEGATED_ACTION.search(text) and re.search(r'시작|완료|전환', text):
            return TurnInterpretation(route='action')
        if SELF_ASSESSMENT.search(text):
            return TurnInterpretation(route='reflection', transition='continue')
        if RECOMMEND_REASON.search(text) or CAPABILITY.search(text) or re.search(r'확실해|검토.*됐|무슨\s*근거', text):
            return TurnInterpretation(route='meta')
        if ASKS_FOR_DISHES.search(text) and (INFO_REQUEST.search(text) or '추천' in text):
            return TurnInterpretation(route='recommend')
        if ((ACTIVITY_LOOKUP.search(text) and INFO_REQUEST.search(text) or re.search(r'비교|차이', text))
                and not is_trouble(text) and len(coaching_requirements(text))<=1):
            return TurnInterpretation(route='lookup')
        pending = state.pending_question
        transition = 'continue'
        if RESOLVED.search(text) and not re.search(r'않|아직|안\s*해결|[?？]', text):
            transition = 'resolve'
        elif NEW_ISSUE.search(text):
            transition = 'new_issue'
        elif ABANDON.search(text):
            transition = 'abandon'
        elif re.search(r'^(정정|사실|말을\s*잘못)', text) or not pending and re.search(r'^아니', text):
            transition = 'correct'
        if transition == 'continue' and pending and pending.get('purpose') == 'transition':
            return TurnInterpretation(route='dialogue',transition='new_issue' if new_issue_confirmed(state,text) else 'continue',transition_quote=text)
        if transition == 'continue' and pending and pending.get('purpose') == 'reflection':
            return TurnInterpretation(route='reflection', transition='continue')
        clear = bool(pending or is_trouble(text) or COACHING_REQUEST.search(text) or transition != 'continue'
                     or state.issue_id and re.search(r'왜|어떻게|그럼|그러면|그래도|이건|그건', text))
        if clear and transition != 'correct':
            proposals = coaching_requirements(text)
            return TurnInterpretation(route='dialogue', transition=transition, transition_quote=text,
                observation_updates=[{'quote': text, 'status': 'unknown' if UNKNOWN.search(text) else 'reported'}],
                requirement_proposals=proposals)
        if not hasattr(self.llm, 'interpret_turn'):
            return TurnInterpretation(route='dialogue' if clear else 'lookup', transition=transition if clear else 'none')
        result = await budget.model_call('decision', self.llm.interpret_turn, text, build_issue_context(state, session))
        try:
            decision = TurnInterpretation.model_validate(result.get('decision'))
        except ValueError:
            return TurnInterpretation(route='dialogue', transition='correct' if transition == 'correct' else 'none')
        if decision.route == 'action':
            return TurnInterpretation(route='lookup')
        return decision
