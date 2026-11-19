import pytest
from app import slots

@pytest.mark.parametrize("text,expected", [
    ("다음 단계", "next"), ("다음 단계 알려줘", "next"), ("이전 단계로 가줘", "prev"),
    ("다음 재료 알려줘", None), ("다음에 뭐 넣어?", None), ("이전에 넣은 재료", None),
])
def test_step_move_requires_step_phrase(text, expected):
    assert slots.step_move(text) == expected

@pytest.mark.parametrize("text,number,selects", [
    ("두 번째 걸로 할게", 2, True), ("좋아 2번", 2, True), ("2번", 2, True),
    ("1번이랑 2번 비교해줘", 1, False), ("두 번째 거 설명해줘", 2, False),
    ("2번 뒤집어도 좋아?", 2, False), ("3단계 보여줘", None, False),
])
def test_candidate_number_and_selection(text, number, selects):
    assert slots.candidate_number(text) == number
    assert slots.selects_candidate(text) is selects

def test_moved_names_stay_importable_from_coach():
    from app import coach
    assert coach.spoken_constraints is slots.spoken_constraints
    assert coach.has_conditions is slots.has_conditions
    assert coach.requested_step("세 번째 단계") == 3
