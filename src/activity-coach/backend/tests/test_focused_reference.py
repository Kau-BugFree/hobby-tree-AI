"""A source reference shows only the passages the planner cited, never model-written text."""
from fastapi.testclient import TestClient

from test_answer_policy import evidence, policy
from test_api import app_factory, create_session, message, select_activity  # noqa: F401
from test_dialogue import Planner, decision

LINES = '물로 큰술과 작은술의 차이를 확인해요\n가루 재료는 깎아서 계량해요\n설탕이 녹을 때까지 저어요'
CONTEXT = {'allowed_activity_ids': ['a']}


def source(excerpt=LINES, **fields):
    row = evidence()
    row.update(excerpt=excerpt, **fields)
    return row


def render(question, focus, rows):
    target = policy()
    return target.render_reference(target.prepare(question, rows, CONTEXT, [], focus))


def test_reference_shows_only_cited_source_lines():
    result = render('왜 짜?', [{'evidence_id': 'e', 'quote': '가루 재료는 깎아서 계량해요'}], [source()])
    assert '가루 재료는 깎아서 계량해요' in result['text']
    assert '설탕이 녹을 때까지' not in result['text'] and '차이를 확인해요' not in result['text']
    assert '원문 보여줘' in result['text'] and result['cited_evidence_ids'] == ['e']


def test_partial_quote_expands_to_the_whole_source_line():
    result = render('왜 짜?', [{'evidence_id': 'e', 'quote': '깎아서'}], [source()])
    assert '- 가루 재료는 깎아서 계량해요' in result['text']


def test_uncited_evidence_is_left_out_of_the_focused_reference():
    other = source('뜨거운 물에 주의해요', evidence_id='e2')
    result = render('왜 짜?', [{'evidence_id': 'e', 'quote': '깎아서'}], [source(), other])
    assert '뜨거운 물' not in result['text'] and result['cited_evidence_ids'] == ['e']


def test_quote_that_is_not_source_text_falls_back_to_the_full_reference():
    for focus in ([{'evidence_id': 'e', 'quote': '소금을 줄이세요'}], [{'evidence_id': 'unknown', 'quote': '깎아서'}], []):
        result = render('왜 짜?', focus, [source()])
        assert all(line in result['text'] for line in LINES.split('\n'))


def test_explicit_original_request_ignores_the_focus():
    result = render('원문 보여줘', [{'evidence_id': 'e', 'quote': '깎아서'}], [source()])
    assert all(line in result['text'] for line in LINES.split('\n'))


def test_cause_disclaimer_is_kept_in_the_focused_reference():
    result = render('왜 짜?', [{'evidence_id': 'e', 'quote': '깎아서'}], [source()])
    assert '원인을 확정할 검토 자료는 없어요' in result['text']


class QuotingPlanner(Planner):
    """Answers with the steps evidence and cites its second line."""

    def __init__(self):
        super().__init__([])
        self.cited = self.skipped = None

    async def decide(self, question, context, evidence):
        steps = next(row for row in evidence if row['evidence_id'].endswith(':steps'))
        self.skipped, self.cited = steps['excerpt'].split('\n')[:2]
        plan = decision('answer')
        plan.update(evidence_ids=[steps['evidence_id']],
                    quotes=[{'evidence_id': steps['evidence_id'], 'quote': self.cited}])
        return {'decision': plan, 'status': 'planned', 'llm_calls': 1, 'usage': {}}


def test_planner_quotes_narrow_the_coaching_reference(app_factory):
    llm = QuotingPlanner()
    with TestClient(app_factory(llm=llm)) as client:
        sid = create_session(client); select_activity(client, sid)
        reply = message(client, sid, '계량이 잘 안돼', 'q')
        assert reply['mode'] == 'reference'
        assert llm.cited in reply['text'] and llm.skipped not in reply['text']
