"""에이전트가 고르는 행동 정의: 형식에 맞는 판단만 행동 모델이 됩니다."""
import json

import anthropic
import pytest

from app.actions import (ACTION_GUIDE, ACTION_NAMES, AgentDecision, Recommend, SelectCandidate, ShowSteps,
                         from_wire, parse_decision)

NEUTRAL = {"action": "ask_user", "number": 0, "restart": False, "accept": False, "move": "none", "step": 0,
           "whole": False, "topic": "", "max_minutes": 0, "clear_time_limit": False, "excluded_tools": [],
           "scope": "keep", "other": False, "target": "none", "candidate_numbers": [], "original": False,
           "route": "dialogue", "transition": "none", "transition_quote": "", "observation_updates": [],
           "requirement_proposals": [], "kind": "none"}


def wire(**values) -> dict:
    """모든 필드를 중립값으로 채운 전송 형식 판단에 주어진 값만 덮어쓴다."""
    return {**NEUTRAL, **values}


@pytest.mark.parametrize("raw,model", [({"action": "select_candidate", "number": 2}, SelectCandidate),
                                       ({"action": "show_steps", "whole": True}, ShowSteps),
                                       ({"action": "recommend"}, Recommend)])
def test_parse_decision_returns_the_action_model(raw, model):
    assert isinstance(parse_decision(raw), model)


@pytest.mark.parametrize("raw", [None, "select", {}, {"action": "delete_everything"}, {"action": "select_candidate"},
                                 {"action": "select_candidate", "number": 2, "extra": 1},
                                 {"action": "notice", "kind": "weather"}])
def test_parse_decision_rejects_what_no_action_defines(raw):
    assert parse_decision(raw) is None


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


def test_decision_schema_has_no_optional_or_union_fields():
    """선택 필드와 합집합은 공급자 문법을 키워 실제 호출이 400으로 거부된다."""
    schema = anthropic.transform_schema(AgentDecision)
    objects = list(_objects(schema))
    assert objects and all(set(item["properties"]) == set(item.get("required", [])) for item in objects)
    assert '"anyOf"' not in json.dumps(schema)


def test_decision_schema_lists_every_action_name():
    schema = anthropic.transform_schema(AgentDecision)
    decision = schema["$defs"]["WireDecision"] if "$ref" in schema["properties"]["decision"] \
        else schema["properties"]["decision"]
    assert decision["properties"]["action"]["enum"] == list(ACTION_NAMES)
    assert len(ACTION_NAMES) == 14 and all(name in ACTION_GUIDE for name in ACTION_NAMES)


def test_from_wire_keeps_only_the_fields_of_the_chosen_action():
    assert from_wire(wire(action="select_candidate", number=2)) == {"action": "select_candidate", "number": 2}
    steps = from_wire(wire(action="show_steps", whole=True))
    assert steps == {"action": "show_steps", "step": None, "move": None, "whole": True}
    assert isinstance(parse_decision(steps), ShowSteps)


def test_from_wire_turns_neutral_values_into_none():
    recommend = from_wire(wire(action="recommend", topic="계란"))
    assert recommend["max_minutes"] is None and recommend["scope"] is None and recommend["topic"] == "계란"
    assert isinstance(parse_decision(recommend), Recommend)


def test_from_wire_leaves_a_missing_target_for_parse_decision_to_reject():
    assert parse_decision(from_wire(wire(action="explain"))) is None


def test_from_wire_returns_an_unknown_action_unchanged():
    raw = {"action": "delete_everything"}
    assert from_wire(raw) == raw and parse_decision(from_wire(raw)) is None
