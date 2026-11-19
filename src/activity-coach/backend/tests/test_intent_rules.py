import time
import pytest
from app.intent_rules import TurnContext, classify

ACTIVITY = {"activity_id": "cook-0-2", "title": "계량스푼으로 간장 드레싱 만들기",
            "steps": ["a", "b", "c"], "materials": [], "tools": []}

def ctx(**over):
    base = dict(selected=None, candidate_count=0, candidate_selection_pending=False,
                pending_question=None, issue_open=False, issue_phase=None, constraints_changed=False)
    return TurnContext(**{**base, **over})

SELECTED = ctx(selected=ACTIVITY)
LISTED = ctx(candidate_count=3, candidate_selection_pending=True)

@pytest.mark.parametrize("text,context,category,kind", [
    ("두 번째 걸로 할게", LISTED, "state_change", "select"),
    ("시작할게", SELECTED, "state_change", "start"),
    ("시작할게", ctx(), "state_change", "start"),          # 전제가 없어도 종류는 start
    ("활동 시작하지 마", SELECTED, "state_change", "decline"),
    ("활동 완료했어", SELECTED, "state_change", "complete"),
    ("준비물 알려줘", SELECTED, "lookup", "materials"),
    ("몇 분 걸려?", SELECTED, "lookup", "duration"),
    ("3단계 보여줘", SELECTED, "lookup", "steps"),
    ("다음 단계 알려줘", SELECTED, "lookup", "steps"),
    ("완료 목록 보여줘", SELECTED, "lookup", "records"),
    ("30분 안에 되는 요리 추천해줘", ctx(), "recommend", "recommend"),
    ("오븐 없어", ctx(), "recommend", "recommend"),
    ("이거 냉장 보관해도 돼?", SELECTED, "question", "explain"),
    ("1번이랑 2번 비교해줘", LISTED, "question", "explain"),
    ("반죽이 자꾸 찢어져요", SELECTED, "question", "problem"),
    ("왜 안 익지?", SELECTED, "question", "explain"),      # 열린 문제가 없으면 지금도 설명 경로다
    ("왜 그런 거야?", ctx(selected=ACTIVITY, issue_open=True, issue_phase="open"), "question", "ask"),
    ("해결됐어", ctx(selected=ACTIVITY, issue_open=True, issue_phase="open"), "question", "issue_control"),
    ("안녕", ctx(), "notice", "greeting"),
    ("뭘 도와줄 수 있어?", ctx(), "notice", "capabilities"),
])
def test_clear_requests_are_classified(text, context, category, kind):
    intent = classify(text, context)
    assert (intent.category, intent.kind) == (category, kind)
    assert intent.rule

@pytest.mark.parametrize("text,context", [
    ("시작해도 돼?", SELECTED), ("다 했는데 안 돼요", SELECTED), ("아직 못 끝냈어", SELECTED),
    ("완료했다고 치면 어떻게 돼?", SELECTED), ("3단계에서 설탕이 안 녹아", SELECTED),
    ("재료를 다 넣었어", SELECTED),
])
def test_state_change_and_lookup_reject_reports_and_questions(text, context):
    intent = classify(text, context)
    assert intent is None or intent.category == "question"

BOTH = ctx(selected=ACTIVITY, candidate_count=3, candidate_selection_pending=True)

@pytest.mark.parametrize("text", [
    "준비물 보여주고 시작할게",                                # 조회 + 상태 변경
    "시작할게, 그리고 30분 안에 되는 다른 요리도 찾아줘",       # 상태 변경 + 추천
])
def test_two_requests_in_one_sentence_are_an_overlap(text):
    intent = classify(text, BOTH)
    assert (intent.category, intent.kind, intent.rule, intent.slots) == ("overlap", "overlap", "overlap", {})

def test_candidate_number_with_start_matches_no_rule():
    # 후보 번호를 말한 시작은 시작 규칙이 빼므로(어느 활동인지 정할 수 없다) 상태 변경과 겹치지 않는다. 겹침이 아니라 None이다.
    assert classify("2번으로 하고 바로 시작할게", BOTH) is None

def test_recommend_word_keeps_its_precedence_over_lookup():
    # 지금 조회 분기는 '추천'이 있으면 빠진다. 이 우선순위는 제외 패턴으로 유지한다.
    assert classify("준비물 알려주고 다른 요리도 추천해줘", BOTH).category == "recommend"

def test_condition_words_alone_do_not_compete_with_a_clear_request():
    # 조건 낱말(오븐, 시간)만으로는 추천이 다른 분류와 겨루지 않는다. 지금처럼 아무것도 안 걸렸을 때만 추천이다.
    assert classify("오븐 예열하는 3단계 보여줘", SELECTED).category == "lookup"
    assert classify("시작할게", ctx(selected=ACTIVITY, constraints_changed=True)).kind == "start"

PENDING = ctx(selected=ACTIVITY, issue_open=True, issue_phase="awaiting_observation",
              pending_question={"purpose": "clarification", "text": "어느 단계인가요?"})

@pytest.mark.parametrize("text", ["다 했어요", "시작할게", "2단계요"])
def test_pending_question_takes_plain_answers_as_reply(text):
    assert classify(text, PENDING).category == "reply"

@pytest.mark.parametrize("text,category", [("활동 완료했어", "state_change"), ("준비물 알려줘", "lookup"),
                                           ("다른 요리 추천해줘", "recommend")])
def test_pending_question_yields_to_direct_requests(text, category):
    assert classify(text, PENDING).category == category

@pytest.mark.parametrize("text", ["", "   ", "?!…", "ㅋ" * 2000, "1" * 2000, "이거 어떻게 해?"])
def test_unclassifiable_input_returns_none_quickly(text):
    started = time.perf_counter()
    assert classify(text, ctx()) is None
    assert time.perf_counter() - started < 0.5

def test_state_report_without_trouble_words_is_not_guessed():
    # 지금은 모델이 없을 때 states_condition 근사로 문제로 받는다. 정리 뒤에는 판정하지 않는다(설계 6절 5번).
    assert classify("반죽이 너무 묽어요", SELECTED) is None

@pytest.mark.parametrize("text", ["2번 섞고 다 했어", "2번 뒤집고 완성했어요"])
def test_count_before_completion_stays_a_partial_completion(text):
    intent = classify(text, BOTH)
    assert (intent.category, intent.kind) == ("state_change", "complete")
    assert intent.slots["partial"] is True

OPEN = ctx(selected=ACTIVITY, issue_open=True, issue_phase="open")

def test_followup_in_open_issue_is_a_question_for_the_dialogue_coach():
    # '그럼 …'은 앞선 issue_question 규칙(accepts의 '^그럼' 절)이 받으므로 복원한 절만 받는 문장을 쓴다.
    intent = classify("불을 줄이면 되나요?", OPEN)
    assert (intent.category, intent.kind, intent.rule) == ("question", "ask", "issue_followup")

@pytest.mark.parametrize("interprets_turns", [True, False])
def test_then_question_in_open_issue_is_an_issue_question(interprets_turns):
    # 열린 문제에 '그럼 …'으로 이어 묻는 말은 accepts의 '^그럼' 절(issue_question)이 받는다. 모델 유무와 무관하다.
    context = ctx(selected=ACTIVITY, issue_open=True, issue_phase="open", interprets_turns=interprets_turns)
    intent = classify("그럼 소금은 언제 넣어", context)
    assert (intent.category, intent.kind, intent.rule) == ("question", "ask", "issue_question")

def test_followup_rule_needs_a_model_and_leaves_the_sentence_to_later_rules():
    # 모델 없이는 요리 이름과 후속을 가릴 수 없다. 뒤의 규칙(설명 요청 등)이 받게 둔다.
    no_model = ctx(selected=ACTIVITY, issue_open=True, issue_phase="open", interprets_turns=False)
    assert classify("불을 줄이면 되나요?", no_model).kind == "explain"
    assert classify("김치볶음밥", no_model) is None

def test_followup_rule_needs_an_open_issue():
    intent = classify("불을 줄이면 되나요?", SELECTED)
    assert intent is None or intent.rule != "issue_followup"


def test_recipe_word_alone_does_not_compete_with_a_lookup():
    intent = classify("레시피 재료 알려줘", SELECTED)
    assert (intent.category, intent.kind) == ("lookup", "materials")


def test_recipe_word_alone_still_ends_as_recommend_when_nothing_else_matches():
    assert classify("김치찌개 레시피", ctx()).category == "recommend"


def test_out_of_range_step_preempts_a_later_state_change():
    intent = classify("99단계 완료했어", SELECTED)
    assert (intent.category, intent.kind, intent.rule) == ("lookup", "steps", "step_out_of_range")


@pytest.mark.parametrize("text,context", [
    ("재료 손질 다 했어", ctx()), ("현재 단계 다 했어", ctx()),
    ("재료 손질 다 했어", SELECTED), ("다 했어, 다른 요리 추천해줘", SELECTED),
])
def test_completion_report_is_not_split_by_later_rules(text: str, context: TurnContext) -> None:
    intent = classify(text, context)
    assert (intent.category, intent.kind) == ("state_change", "complete")


def test_rules_use_the_hobby_vocabulary(monkeypatch):
    from dataclasses import replace
    from app import vocabulary
    cooking = vocabulary.vocabulary_for("cooking")
    monkeypatch.setitem(vocabulary.VOCABULARY, "knitting",
                        replace(cooking, dish_nouns=("도안", "작품"), tools=("대바늘",), key_tools=(),
                                out_of_scope=("요리",)))
    knitting = ctx(hobby_key="knitting")
    assert classify("쉬운 도안 추천해줘", knitting).category == "recommend"
    assert classify("요리 얘기 하자", knitting).kind == "out_of_scope"
    assert classify("쉬운 요리 추천해줘", ctx()).category == "recommend"


def test_unknown_hobby_is_an_error():
    import pytest
    from app.vocabulary import vocabulary_for
    with pytest.raises(KeyError):
        vocabulary_for("unknown")


def test_unknown_hobby_key_makes_classify_raise() -> None:
    with pytest.raises(KeyError):
        classify("날씨 어때", ctx(hobby_key="unknown"))


def test_tool_and_other_words_follow_the_hobby(monkeypatch) -> None:
    from dataclasses import replace
    from app import vocabulary
    from app.slots import spoken_constraints
    cooking = vocabulary.vocabulary_for("cooking")
    monkeypatch.setitem(vocabulary.VOCABULARY, "knitting",
                        replace(cooking, dish_nouns=("도안", "작품"), tools=("대바늘",), key_tools=(),
                                out_of_scope=("요리",)))
    assert spoken_constraints("대바늘 없어", {}, hobby="knitting") == {"excluded_tools": ["대바늘"]}
    assert spoken_constraints("대바늘 없어", {}) == {}
    intent = classify("다른 도안 추천해줘", ctx(hobby_key="knitting"))
    assert intent.slots["other"] is True


def test_cooking_patterns_built_from_vocabulary_are_unchanged() -> None:
    from app.intent_rules import OFF_TOPIC_COMPLAINT, OUT_OF_SCOPE
    from app.slots import NO_TOOL, OTHER_DISHES
    assert OUT_OF_SCOPE.pattern == r"(날씨|주식|정치|러닝|달리기|커피|독서)"
    assert OFF_TOPIC_COMPLAINT.pattern == (
        r"\s*(?:요즘|오늘|아|나|저|내|제)?\s*(날씨|주식|정치|러닝|달리기|커피|독서)(?:이|가|은|는|도)?\s*"
        r"(?:너무|정말|진짜|아주|좀|많이|계속|자꾸|다)?\s*\S+[\s.!~…ㅠㅜ]*")
    assert NO_TOOL.pattern == (
        r"(오븐|전자레인지|프라이팬|후라이팬|냄비|도마|저울|계량스푼|계량컵|찜기|밀대|거품기|핸드믹서|온도계|뒤집개|집게"
        r"|(?<![가-힣])(?:칼|체|팬))\s*(?:이|가|은|는|도)?\s*(?:없|못\s*써|안\s*써)\S*")
    assert OTHER_DISHES.pattern == (
        r"(?:다른|딴)\s*(?:거|것|걸|건|게|요리|활동|메뉴|레시피|음식|종류)(?:으로|로|을|를|은|는|도)?")


def test_stated_limit_pattern_is_unchanged() -> None:
    from app.intent_rules import STATED_LIMIT
    assert STATED_LIMIT.pattern == (
        r"\d\s*(?:분|시간)\s*(?:만(?!\s*더)|안에|안으로|내에|내로|이내|이하)|시간\s*없|오븐"
        r"|시간\s*(?:은|는|이)?\s*(?:제한|상관|관계)")


def test_notice_texts_come_from_the_cooking_vocabulary() -> None:
    from app.intent_handlers import CAPABILITIES_TEXT, GREETING_TEXT, OUT_OF_SCOPE_TEXT
    from app.vocabulary import vocabulary_for
    cooking = vocabulary_for("cooking")
    assert (cooking.scope_notice, cooking.capabilities_notice, cooking.greeting_notice) == (
        OUT_OF_SCOPE_TEXT, CAPABILITIES_TEXT, GREETING_TEXT)
    assert OUT_OF_SCOPE_TEXT == "현재는 요리 활동을 지원합니다. 오늘 가능한 시간이나 하고 싶은 요리를 알려주세요."
    assert CAPABILITIES_TEXT == (
        "저는 요리 활동코치예요. 시간·도구 조건에 맞는 요리 활동 추천, 활동 선택과 준비물·단계·소요 시간 안내, "
        "단계 설명, 막힌 부분에 대한 확인 질문과 활동 원문 근거 안내, 활동 시작·완료 기록을 도와요. "
        "알레르기 판정, 검토된 자료 없이 실패 원인을 확정하는 일, 근거에 없는 수량·시간을 만드는 일, 요리 외 주제는 다루지 않아요."
    )
    assert GREETING_TEXT == "요리 활동을 함께 진행할게요. 오늘 할 활동을 추천받거나 선택한 활동의 준비물·순서를 물어보세요."


def test_key_tools_must_be_listed_tools() -> None:
    from dataclasses import replace
    from app.vocabulary import vocabulary_for
    with pytest.raises(ValueError):
        replace(vocabulary_for("cooking"), tools=("대바늘",))


@pytest.mark.parametrize("field", ["dish_nouns", "tools", "out_of_scope"])
def test_empty_word_list_is_rejected(field: str) -> None:
    # 빈 목록이면 패턴에 빈 대안이 생긴다('없어'만으로 이름 없는 도구를 뺀다).
    from dataclasses import replace
    from app.vocabulary import vocabulary_for
    over = {field: ()} if field != "tools" else {"tools": (), "key_tools": ()}
    with pytest.raises(ValueError, match=field):
        replace(vocabulary_for("cooking"), **over)


def test_empty_key_tools_leaves_stated_limit_without_tool_words() -> None:
    from dataclasses import replace
    from app.intent_rules import scope_patterns
    from app.vocabulary import vocabulary_for
    knitting = replace(vocabulary_for("cooking"), tools=("대바늘",), key_tools=())
    stated_limit = scope_patterns(knitting).stated_limit
    assert stated_limit.pattern == (
        r"\d\s*(?:분|시간)\s*(?:만(?!\s*더)|안에|안으로|내에|내로|이내|이하)|시간\s*없"
        r"|시간\s*(?:은|는|이)?\s*(?:제한|상관|관계)")
    assert not stated_limit.search("오븐 없어") and not stated_limit.search("뜨개질")


OOS_WORDS = ["날씨", "주식", "정치", "러닝", "달리기", "커피", "독서"]
SIDE_DISH = {"activity_id": "cook-x", "title": "주식과 곁들임 고르기", "steps": ["주식과 곁들임을 하나씩 골라요"]}


@pytest.mark.parametrize("text,activity", [
    *[(word, None) for word in OOS_WORDS],
    *[(f"{word} 얘기 하자", ACTIVITY) for word in OOS_WORDS],
    *[(f"{word} 보면서 요리할래", None) for word in OOS_WORDS],
    *[(f"{word}먹고 싶어", None) for word in OOS_WORDS],
    *[(f"{word} 디저트 레시피", ACTIVITY) for word in OOS_WORDS],
    ("주식을 하나 골랐어", SIDE_DISH), ("주식 망했어", SIDE_DISH), ("커피 마실래", SIDE_DISH),
    ("날씨가 습해서 반죽이 안 돼", ACTIVITY), ("오늘 뭐 먹지", None), ("안녕", None),
])
def test_off_topic_matches_the_old_expression_for_cooking(text: str, activity: dict | None) -> None:
    import json
    from app.intent_rules import off_topic
    from app.retrieval import COOKING_WORDS
    import re
    words = re.findall(r"(날씨|주식|정치|러닝|달리기|커피|독서)", text)
    own = json.dumps(activity, ensure_ascii=False) if activity else ""
    old = bool(words) and not COOKING_WORDS.search(text) and not any(word in own for word in words)
    assert off_topic(text, activity) is old
