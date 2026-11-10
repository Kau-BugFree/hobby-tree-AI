"""Suspected regex false positives: one ordinary cooking word must not change routing."""
import asyncio

import pytest

from app.dialogue_models import DialogueState, Observation
from app.turn_budget import TurnBudget
from app.turn_router import TurnRouter, coaching_requirements


def _routing(text: str, state: DialogueState) -> tuple[str, str, list[str]]:
    intent = asyncio.run(TurnRouter(None).interpret(text, {}, state, TurnBudget()))
    return intent.route, intent.transition, sorted(p.kind for p in intent.requirement_proposals)


@pytest.mark.parametrize('suspect, control', [
    ('이유식 만드는 순서 알려줘.', '죽 만드는 순서 알려줘.'),  # '이유' inside 이유식
    ('왜간장 넣는 순서 알려줘.', '진간장 넣는 순서 알려줘.'),  # '왜' inside 왜간장
    ('닭가슴살을 찢어서 넣는 순서 알려줘.', '닭가슴살을 썰어서 넣는 순서 알려줘.'),  # '찢어' as trouble
])
def test_cooking_word_does_not_change_lookup_routing(suspect, control):
    assert _routing(control, DialogueState())[0] == 'lookup'
    assert _routing(suspect, DialogueState()) == _routing(control, DialogueState())


@pytest.mark.parametrize('text', ['이유식 만드는 순서 알려줘.', '왜간장 넣는 순서 알려줘.'])
def test_food_name_is_not_a_cause_request(text):
    assert 'cause' not in [part['kind'] for part in coaching_requirements(text)]


def test_discourse_marker_ani_is_not_a_correction():
    state = DialogueState(issue_id='i', phase='open',
                          observations=[Observation(id='o', source_message_id='m', quote='반죽이 질어.')])
    control = _routing('근데 왜 이렇게 질어?', state)
    assert control == ('dialogue', 'continue', ['cause'])
    assert _routing('아니 근데 왜 이렇게 질어?', state) == control
