"""Read-only intent interpretation; execution authority stays in Coach."""
import re

from .dialogue_models import DialogueState, TurnInterpretation
from .dialogue_state import ABANDON, NEW_ISSUE, UNKNOWN, build_issue_context, new_issue_confirmed, issue_resolved
from .turn_budget import TurnBudget


COACHING_PARTS = (
    # ponytail: denylist for food names containing 이유/왜; switch to word tokens if more appear.
    ('cause', r'원인|이유(?!식)|왜(?!간장)'),
    ('remedy', r'해결\s*(?:방법|책)?|대처\s*(?:방법|법)?|어떻게'),
    ('procedure', r'(?:원문\s*)?(?:(?:완료|확인|점검)\s*(?:확인\s*)?기준|절차|(?:작업\s*)?순서)|원문(?:의)?\s*방법'),
)
COACHING_REQUEST = re.compile('|'.join(pattern for _,pattern in COACHING_PARTS))
OBSERVATION_SUMMARY = re.compile(r'(?:관찰|보고|모르는\s*점).*(?:구분|정리|요약)')
# 받침 ㅆ으로 끝나는 과거형 음절(탔·쳤·췄 …). 음절을 하나씩 나열하면 '탔어', '뭉쳤어'를 놓친다.
# '겠'(추측·의지: 알겠어)과 '있'(현재: 시간 있어)은 받침이 ㅆ이어도 과거 보고가 아니므로 뺀다.
SS_FINAL = ''.join(chr(code) for code in range(0xAC00, 0xD7A4)
                   if (code - 0xAC00) % 28 == 20 and chr(code) not in '겠있')
REPORT_ENDING = re.compile(rf'(?:[{SS_FINAL}](?:어(?:요)?|다)|남아\s*있(?:어|어요|다)|올라와(?:요)?)[.!\s]*$')


def information_request(text: str) -> bool:
    from .coach import INFO_REQUEST
    return bool(INFO_REQUEST.search(text) or re.search(r'말해\s*줘|(?:구분|요약)해', text)
                or withdrawn_requirements(text))


def reported_observation(text: str) -> bool:
    from .dialogue import is_trouble
    # Declarative reports also occur before the user explicitly calls them a problem.
    return not information_request(text) and bool(is_trouble(text) or REPORT_ENDING.search(text))


def observation_quotes(text: str) -> list[str]:
    # shortcut: sentence-separated reports; use intent parsing for richer mixed-clause Korean.
    return [clause.strip() for clause in re.findall(r'[^.!?。？\n]+[.!?。？]?',text)
            if reported_observation(clause.strip())]


def withdrawn_requirements(text: str) -> set[str]:
    withdrawn = set()
    for clause in re.split(r'[.!?。？]',text):
        subjects = sorted((m.start(),m.end(),kind) for kind,pattern in COACHING_PARTS
                          for m in re.finditer(pattern,clause))
        for index,(start,end,kind) in enumerate(subjects):
            segment = clause[end:subjects[index+1][0] if index+1<len(subjects) else len(clause)]
            if (not re.search(r'취소|말고|제외|빼\s*줘|필요\s*없',segment)
                    or re.search(r'(?:취소|제외).*?(?:않|아니|하지\s*(?:마|말))|빼지\s*(?:마|말)',segment)):
                continue
            withdrawn.add(kind)
            # Coordinated nouns share the instruction; a separate request does not.
            while index>0 and re.fullmatch(r'\s*(?:과|와|및|그리고|,)\s*',clause[subjects[index-1][1]:subjects[index][0]]):
                index-=1
                withdrawn.add(subjects[index][2])
    return withdrawn


def coaching_requirements(text: str) -> list[dict]:
    """Extract explicit subrequests even when this turn also answers a question."""
    parts = []
    withdrawn = withdrawn_requirements(text)
    for kind, pattern in COACHING_PARTS:
        match = re.search(pattern, text)
        if match and kind not in withdrawn:
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


class InterpretationFailed(Exception):
    """The model could not interpret the turn. The caller must leave the dialogue state unchanged."""


class TurnRouter:
    def __init__(self, llm):
        self.llm = llm

    async def interpret(self, text: str, session: dict, state: DialogueState, budget: TurnBudget) -> TurnInterpretation:
        from .coach import (ACTIVITY_LOOKUP, CAPABILITY, NEGATED_ACTION,
                            RECOMMEND_REASON, SELF_ASSESSMENT, INFO_REQUEST, asks_for_dishes)
        from .dialogue import is_trouble
        quotes = observation_quotes(text)
        if issue_resolved(text):
            return TurnInterpretation(route='dialogue', transition='resolve', transition_quote=text)
        if OBSERVATION_SUMMARY.search(text):
            return TurnInterpretation(route='dialogue', transition='continue')
        # Only a refusal of the action itself ('시작하지 마') is an action turn. '시작 못 했어요' and
        # '완료 못 했는데 반죽이 안 돼' are reports; the 시작 in '녹기 시작하지 않아요' is a change of state.
        if (NEGATED_ACTION.search(text) and re.search(r'(?<!기)(?<!기\s)(?:시작|완료|전환)', text)
                and not quotes and not is_trouble(text)):
            return TurnInterpretation(route='action')
        if SELF_ASSESSMENT.search(text):
            return TurnInterpretation(route='reflection', transition='continue')
        if RECOMMEND_REASON.search(text) or CAPABILITY.search(text) or re.search(r'확실해|검토.*됐|무슨\s*근거', text):
            return TurnInterpretation(route='meta')
        if asks_for_dishes(text) and (INFO_REQUEST.search(text) or '추천' in text):
            return TurnInterpretation(route='recommend')
        if ((ACTIVITY_LOOKUP.search(text) and INFO_REQUEST.search(text) or re.search(r'비교|차이', text))
                and not quotes and not is_trouble(text) and len(coaching_requirements(text))<=1):
            return TurnInterpretation(route='lookup')
        pending = state.pending_question
        transition = 'continue'
        if NEW_ISSUE.search(text):
            transition = 'new_issue'
        elif ABANDON.search(text):
            transition = 'abandon'
        elif re.search(r'^(정정|사실|말을\s*잘못)', text) or not pending and re.search(r'^아니(?!\s*(?:근데|그런데))', text):  # '아니 근데'는 담화 표지
            transition = 'correct'
        if transition=='continue' and not quotes and information_request(text) and not is_trouble(text) and not COACHING_REQUEST.search(text):
            return TurnInterpretation(route='lookup')
        if transition == 'continue' and pending and pending.get('purpose') == 'transition':
            return TurnInterpretation(route='dialogue',transition='new_issue' if new_issue_confirmed(state,text) else 'continue',transition_quote=text)
        if transition == 'continue' and pending and pending.get('purpose') == 'reflection':
            return TurnInterpretation(route='reflection', transition='continue')
        clear = bool(pending or is_trouble(text) or quotes or COACHING_REQUEST.search(text) or transition != 'continue'
                     or state.issue_id and information_request(text)
                     or state.issue_id and re.search(r'왜|어떻게|그럼|그러면|그래도|이건|그건', text))
        if clear and transition != 'correct':
            proposals = coaching_requirements(text)
            if pending and not quotes and not information_request(text):
                quotes = [text]
            return TurnInterpretation(route='dialogue', transition=transition, transition_quote=text,
                observation_updates=[{'quote': quote, 'status': 'unknown' if UNKNOWN.search(quote) else 'reported'} for quote in quotes],
                requirement_proposals=proposals)
        if not hasattr(self.llm, 'interpret_turn'):
            return TurnInterpretation(route='dialogue' if clear else 'lookup', transition=transition if clear else 'none')
        result = await budget.model_call('decision', self.llm.interpret_turn, text, build_issue_context(state, session))
        try:
            decision = TurnInterpretation.model_validate(result.get('decision'))
        except ValueError:
            # Without a key the rules above are all there is. A provider error or an invalid answer is a
            # failed turn: guessing 'dialogue' would open an issue for a dish name.
            if result.get('status') != 'not_configured':
                raise InterpretationFailed(result.get('status') or 'invalid_decision')
            return TurnInterpretation(route='dialogue', transition='correct' if transition == 'correct' else 'none')
        if decision.route == 'action':
            return TurnInterpretation(route='lookup')
        if transition == 'correct' and decision.transition != 'new_issue':
            return decision.model_copy(update={'route':'dialogue', 'transition':'correct'})
        return decision
