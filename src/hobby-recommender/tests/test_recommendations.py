"""Public API behavior: validation, explainable scoring and activity feasibility."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


COOKING = "요리 (한식·중식·양식·일식·제과·제빵)"
INSTRUMENT = "악기 (피아노·기타)"
DATA = Path(__file__).resolve().parents[1] / "app" / "data"


@pytest.fixture
def client():
    """데이터 로딩과 종료 처리를 포함한 독립 테스트 클라이언트를 제공한다."""
    with TestClient(create_app()) as session:
        yield session


def recommend(client, answers=None, **kwargs):
    """정상 추천 요청을 보내 200 응답을 확인하고 JSON 결과를 반환한다."""
    response = client.post(
        "/api/v1/recommendations", json={"answers": answers or {}, **kwargs}
    )
    assert response.status_code == 200, response.text
    return response.json()


def ranked_ids(result):
    """추천 순서 검증에 사용할 취미 코드 목록을 추출한다."""
    return [item["hobby_id"] for item in result["recommendations"]]


def scores(result):
    """취미별 점수를 비교하기 쉬운 코드→점수 사전으로 변환한다."""
    return {item["hobby_id"]: item["score"] for item in result["recommendations"]}


def activity_ids(result):
    """모든 추천 후보에 포함된 활동 코드 집합을 추출한다."""
    return {
        activity["activity_id"]
        for hobby in result["recommendations"]
        for activity in hobby["activities"]
    }


def test_empty_answers_are_neutral_and_ties_are_deterministic(client):
    """미응답은 50점이며 동점 순서가 호출마다 동일한지 확인한다."""
    result = recommend(client)
    assert result["status"] == "recommended"
    assert scores(result) == {"cooking": 50.0, "instrument": 50.0, "reading": 50.0}
    assert ranked_ids(result) == ["cooking", "instrument", "reading"]
    assert result["coverage"]["weighted_answer_coverage"] == 0
    assert result["coverage"]["answered_groups"] == 0
    assert ranked_ids(recommend(client)) == ranked_ids(result)


@pytest.mark.parametrize(
    ("interest", "hobby_id"),
    [(COOKING, "cooking"), (INSTRUMENT, "instrument"), ("독서", "reading")],
)
def test_explicit_interest_ranks_the_matching_hobby_first(client, interest, hobby_id):
    """직접 선택한 지원 취미가 관심 가중치에 따라 우선되는지 확인한다."""
    result = recommend(client, {"q02": [interest]})
    assert ranked_ids(result)[0] == hobby_id
    assert scores(result)[hobby_id] == 70.0
    assert all(
        score == 50.0 for hobby, score in scores(result).items() if hobby != hobby_id
    )


@pytest.mark.parametrize(
    "answers",
    [
        {"q03": ["휴식·기분 전환", "건강·체력", "실력 향상·성취감"]},
        {"q02": ["독서", "독서"]},
        {"q02": ["독서", "아직 모르겠어요"]},
        {"q09Exclude": ["없음", "야외"]},
        {"q08": ["해당 없음", "확인이 필요해요"]},
        {"q99": "독서"},
        {"q02": ["목록에 없는 선택지"]},
        {"q10": ["혼자"]},
        {"q02": "독서"},
    ],
)
def test_invalid_answer_shapes_and_constraints_are_rejected(client, answers):
    """잘못된 키·자료형·중복·배타 선택·개수 위반이 422인지 확인한다."""
    response = client.post("/api/v1/recommendations", json={"answers": answers})
    assert response.status_code == 422, response.text


def test_q02_three_is_guidance_not_an_enforced_maximum(client):
    """Q02의 안내 문구를 입력 개수 제한으로 잘못 적용하지 않는지 확인한다."""
    result = recommend(client, {"q02": ["커피", COOKING, "독서", "러닝·걷기"]})
    assert len(result["recommendations"]) == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"answers": {"q04": "직접 금액 입력"}},
        {"answers": {"q05": "직접 금액 입력"}},
        {"answers": {"q06": "직접 시간 입력"}},
        {"answers": {"q02": ["다른 취미 직접 입력"]}},
        {"answers": {"q09Exclude": ["직접 입력"]}},
        {"custom_values": {"q04_amount_krw": 1000}},
        {"custom_values": {"q05_amount_krw": 1000}},
        {"custom_values": {"q06_minutes": 15}},
        {"custom_values": {"q02_other": "수채화"}},
        {"custom_values": {"q09_exclude_text": "특정 재료 제외"}},
    ],
)
def test_custom_values_require_the_corresponding_selection(client, payload):
    """직접 입력 선택과 실제 값 중 하나만 있으면 거부하는지 확인한다."""
    response = client.post("/api/v1/recommendations", json=payload)
    assert response.status_code == 422, response.text


def test_zero_money_is_a_valid_explicit_budget(client):
    """0원 예산을 유효한 명시적 한도로 보존하는지 확인한다."""
    result = recommend(
        client,
        {"q04": "직접 금액 입력", "q05": "직접 금액 입력"},
        custom_values={"q04_amount_krw": 0, "q05_amount_krw": 0},
        monthly_committed_krw=0,
    )
    assert result["recommendations"]
    assert all(
        activity["estimated_first_cost_krw"] == 0
        for hobby in result["recommendations"]
        for activity in hobby["activities"]
    )


@pytest.mark.parametrize("value", [True, "1000", -1, 1.5])
@pytest.mark.parametrize("field", ["q04_amount_krw", "q05_amount_krw"])
def test_money_does_not_coerce_invalid_values(client, field, value):
    """금액 문자열·불리언·음수·소수를 정수 금액으로 자동 변환하지 않는지 확인한다."""
    key = field[:3]
    response = client.post(
        "/api/v1/recommendations",
        json={"answers": {key: "직접 금액 입력"}, "custom_values": {field: value}},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("value", [0, True, "10", -1])
def test_custom_time_requires_a_positive_integer(client, value):
    """직접 입력 시간에 양의 정수만 허용하는지 확인한다."""
    response = client.post(
        "/api/v1/recommendations",
        json={
            "answers": {"q06": "직접 시간 입력"},
            "custom_values": {"q06_minutes": value},
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("interest", ["커피", "러닝·걷기"])
def test_unsupported_interest_is_acknowledged_without_becoming_a_hobby(client, interest):
    """미지원 관심사를 반환하되 존재하지 않는 추천 후보를 만들지 않는지 확인한다."""
    result = recommend(client, {"q02": [interest]})
    assert interest in result["unsupported_interests"]
    assert result["notices"]
    assert set(ranked_ids(result)) == {"cooking", "instrument", "reading"}
    assert set(scores(result).values()) == {50.0}


@pytest.mark.parametrize(
    "answers",
    [
        {"q01": "지금은 취미를 시작할 생각이 없어요"},
        {"q11": "안내 없이 직접 찾기"},
    ],
)
def test_browse_preference_is_respected(client, answers):
    """시작 의사 없음 또는 직접 탐색 선택에서 추천을 강요하지 않는지 확인한다."""
    result = recommend(client, answers)
    assert result["status"] == "browse"
    assert result["recommendations"] == []


def test_single_suggestion_preference_and_explicit_limit(client):
    """Q11 한 개 제안과 명시적인 top_k의 우선순위를 확인한다."""
    answers = {"q11": "한 개를 제안받기", "q02": ["독서"]}
    assert ranked_ids(recommend(client, answers)) == ["reading"]
    assert len(recommend(client, answers, top_k=3)["recommendations"]) == 3


@pytest.mark.parametrize(
    "excluded", [["큰 소음"], ["큰 소음", "외부 시설"]]
)
def test_sound_exclusions_preserve_only_headphone_instrument_activity(client, excluded):
    """큰 소음 제외 시 기타·연습실은 제외하되 헤드폰 건반의 확인 필요 후보는 남긴다."""
    result = recommend(client, {"q02": [INSTRUMENT], "q09Exclude": excluded})
    instrument = next(hobby for hobby in result["recommendations"] if hobby["hobby_id"] == "instrument")
    assert [activity["activity_id"] for activity in instrument["activities"]] == ["instrument-keyboard-headphones-15"]
    assert instrument["activities"][0]["status"] == "needs_confirmation"
    assert any("타건 소음" in check for check in instrument["activities"][0]["confirmation_required"])


def test_unavailable_headphones_and_sound_exclusion_remove_instrument(client):
    """헤드폰을 사용할 수 없고 큰 소음을 제외하면 악기 관심이 높아도 후보에서 뺀다."""
    result = recommend(client, {"q02": [INSTRUMENT], "q09Exclude": ["큰 소음"]}, resources={"headphones": False})
    assert "instrument" not in ranked_ids(result)
    assert "instrument" in [hobby["hobby_id"] for hobby in result["excluded"]]


def test_facility_exclusion_removes_only_practice_room(client):
    """외부 시설 제외는 연습실 활동에 적용하고 집의 건반·기타 후보는 유지한다."""
    result = recommend(client, {"q02": [INSTRUMENT], "q09Exclude": ["외부 시설"]})
    assert "instrument-practice-room-40" not in activity_ids(result)
    assert "instrument-keyboard-headphones-15" in activity_ids(result)
    assert "instrument-acoustic-guitar-20" in activity_ids(result)


@pytest.mark.parametrize("equipment", ["피아노·건반", "기타"])
def test_instrument_equipment_affects_score_without_confirming_resources(client, equipment):
    """Q08 악기는 선호 점수만 올리며 개별 악기·헤드폰·장소 확인을 생략하지 않는다."""
    result = recommend(client, {"q08": [equipment]})
    assert ranked_ids(result)[0] == "instrument"
    assert scores(result)["instrument"] == 55.0
    instrument = next(hobby for hobby in result["recommendations"] if hobby["hobby_id"] == "instrument")
    assert all(activity["status"] == "needs_confirmation" for activity in instrument["activities"])


def test_non_mvp_equipment_does_not_supply_scoring_evidence(client):
    """운동화 선택은 원본 설문에서 허용하되 MVP 세 취미의 점수 근거로 사용하지 않는다."""
    result = recommend(client, {"q08": ["운동화·운동복"]})
    assert set(scores(result).values()) == {50.0}
    assert result["coverage"]["weighted_answer_coverage"] == 0


def test_fire_exclusion_preserves_no_fire_cooking_options(client):
    """화기 제외가 불을 사용하지 않는 요리까지 제거하지 않는지 확인한다."""
    result = recommend(client, {"q09Exclude": ["화기 사용"]})
    assert "cooking" in ranked_ids(result)
    assert "cooking-warm-soup" not in activity_ids(result)
    assert "cooking-pantry-dressing" in activity_ids(result)


def test_no_available_resources_returns_no_match(client):
    """모든 필수 자원을 사용할 수 없을 때 후보 없음 상태인지 확인한다."""
    resource_keys = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))["resources"]
    result = recommend(client, resources={key: False for key in resource_keys})
    assert result["status"] == "no_match"
    assert result["recommendations"] == []


def test_insufficient_total_time_returns_no_match(client):
    """전체 소요 시간 한도를 통과하는 활동이 없으면 후보 없음인지 확인한다."""
    result = recommend(
        client, {"q06": "직접 시간 입력"}, custom_values={"q06_minutes": 1}
    )
    assert result["status"] == "no_match"
    assert result["recommendations"] == []


def test_missing_resources_and_broad_q08_selection_are_not_confirmation(client):
    """Q08 물품 묶음 선택을 구체적인 자원 보유 확정으로 오인하지 않는지 확인한다."""
    result = recommend(client, {"q08": ["읽을 책·전자책", "주방 도구", "피아노·건반"]})
    activities = [a for hobby in result["recommendations"] for a in hobby["activities"]]
    assert activities
    assert all(a["status"] == "needs_confirmation" for a in activities)
    assert all(a["confirmation_required"] for a in activities)


def test_explicit_resource_and_constraint_confirmation_can_make_activity_eligible(client):
    """필요 자원과 조건이 확인된 독서 활동은 실행 가능 상태가 되는지 확인한다."""
    result = recommend(
        client,
        {
            "q04": "0원", "q05": "0원", "q06": "10분",
            "q07": "주 1회", "q09Exclude": ["없음"],
        },
        resources={"owned_book": True},
        monthly_committed_krw=0,
    )
    reading = next(h for h in result["recommendations"] if h["hobby_id"] == "reading")
    owned = next(a for a in reading["activities"] if a["activity_id"] == "reading-owned-10")
    assert owned["status"] == "eligible"
    assert owned["confirmation_required"] == []


def test_monthly_commitments_reduce_the_shared_budget_for_every_hobby(client):
    """이미 지출·예약한 금액을 모든 취미의 월 가용 예산에 반영하는지 확인한다."""
    answers = {"q04": "1만 원까지", "q05": "3만 원까지", "q07": "주 1회"}
    uncommitted = recommend(client, answers, monthly_committed_krw=0)
    committed = recommend(client, answers, monthly_committed_krw=29000)
    assert "cooking-buy-dressing" in activity_ids(uncommitted)
    assert "instrument-practice-room-40" in activity_ids(uncommitted)
    assert "cooking-buy-dressing" not in activity_ids(committed)
    assert "instrument-practice-room-40" not in activity_ids(committed)
    assert "reading-owned-10" in activity_ids(committed)


def test_deferred_budget_is_unknown_rather_than_zero(client):
    """나중에 결정할 예산을 0원으로 해석하지 않는지 확인한다."""
    deferred = recommend(client, {"q04": "나와 맞는지 확인한 뒤 결정"})
    zero = recommend(client, {"q04": "0원"})
    assert "cooking-buy-dressing" in activity_ids(deferred)
    assert "cooking-buy-dressing" not in activity_ids(zero)


def test_freeform_exclusion_requires_review_without_recommendations(client):
    """자유 입력 제외 조건이 확인되기 전에는 추천을 비우는지 확인한다."""
    result = recommend(
        client, {"q09Exclude": ["직접 입력"]},
        custom_values={"q09_exclude_text": "특정 재료를 제외해야 해요"},
    )
    assert result["status"] == "needs_review"
    assert result["recommendations"] == []
    assert result["follow_up_questions"]


def test_recording_and_burden_preferences_do_not_change_hobby_scores(client):
    """기록 방식과 부담 요소가 점수를 바꾸지 않고 선호로 전달되는지 확인한다."""
    baseline = recommend(client, {"q02": ["독서"]})
    preferences = recommend(
        client,
        {"q02": ["독서"], "q12": "기록하지 않기", "q13": ["여러 항목 기록", "기한 목표"]},
    )
    assert scores(preferences) == scores(baseline)
    assert preferences["preferences"]["recording_mode"] == "기록하지 않기"
    assert preferences["preferences"]["burdens"] == ["여러 항목 기록", "기한 목표"]


def test_score_breakdown_adds_up_and_goal_selections_are_averaged(client):
    """질문별 점수 합과 복수 목적 평균 계산이 총점에 맞는지 확인한다."""
    result = recommend(client, {"q03": ["건강·체력", "창작·자기표현"]})
    for hobby in result["recommendations"]:
        assert sum(part["points"] for part in hobby["breakdown"]) == pytest.approx(hobby["score"], abs=.02)
        goal = next(part for part in hobby["breakdown"] if part["question_key"] == "q03")
        if hobby["hobby_id"] == "instrument":
            assert goal["fit"] == pytest.approx(.65)
            assert goal["points"] == pytest.approx(19.5)


def test_api_discovery_endpoints(client):
    """상태·카탈로그·설문·Swagger 경로가 정상 응답하는지 확인한다."""
    assert client.get("/health").status_code == 200
    catalog = client.get("/api/v1/catalog")
    questionnaire = client.get("/api/v1/questionnaire")
    assert catalog.status_code == questionnaire.status_code == 200
    assert len(catalog.json()["hobbies"]) == 3
    assert {hobby["id"] for hobby in catalog.json()["hobbies"]} == {"cooking", "instrument", "reading"}
    assert {hobby["ui_key"] for hobby in catalog.json()["hobbies"]} == {"요리", "악기", "독서"}
    assert len(questionnaire.json()["groups"]) == 14
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/api/v1/recommendations" in schema["paths"]


def test_frontend_context_text_requires_review_without_guessing(client):
    """Q14 내용을 버리거나 구조화된 조건으로 추측하지 않고 확인 요청에 보존한다."""
    result = recommend(client, {"q02": ["독서"]}, context_text="  밤에만 할 수 있어요  ")
    assert result["status"] == "needs_review"
    assert result["recommendations"] == []
    assert any("밤에만 할 수 있어요" in question for question in result["follow_up_questions"])


def test_freeform_exclusion_and_context_are_both_preserved(client):
    """Q09 직접 입력과 Q14가 함께 있으면 어느 하나도 검토 목록에서 누락하지 않는다."""
    result = recommend(
        client, {"q09Exclude": ["직접 입력"]},
        custom_values={"q09_exclude_text": "특정 재료 제외"}, context_text="밤에만 가능",
    )
    assert result["status"] == "needs_review"
    assert len(result["follow_up_questions"]) == 2
    assert any("특정 재료 제외" in question for question in result["follow_up_questions"])
    assert any("밤에만 가능" in question for question in result["follow_up_questions"])


def test_blank_context_does_not_block_recommendation(client):
    """공백만 있는 고려사항은 실제 추가 조건으로 취급하지 않는다."""
    assert recommend(client, context_text="   ")["status"] == "recommended"


@pytest.mark.parametrize("context", ["", "가" * 201, 123])
def test_context_text_contract_is_enforced(client, context):
    """Q14는 프론트엔드와 동일하게 최대 200자 문자열이며 빈 값은 생략해야 한다."""
    response = client.post("/api/v1/recommendations", json={"context_text": context})
    assert response.status_code == 422


def test_browse_intent_takes_precedence_over_context_review(client):
    """시작 의사가 없는 사용자는 자유 입력이 있어도 탐색 상태로 안내한다."""
    result = recommend(client, {"q01": "지금은 취미를 시작할 생각이 없어요"}, context_text="밤에만 가능")
    assert result["status"] == "browse"


def test_spring_create_request_is_not_silently_accepted(client):
    """동일 경로의 Spring 요청을 중립 점수 추천으로 잘못 처리하지 않는다."""
    response = client.post(
        "/api/v1/recommendations",
        json={"preferencesVersion": 1, "count": 3, "excludeHobbyIds": []},
    )
    assert response.status_code == 422


def test_questionnaire_matches_pinned_frontend_contract(client):
    """실제 공개 프론트엔드 커밋의 14개 그룹과 서버의 선택지·선택 제약을 비교한다."""
    fixture = json.loads((Path(__file__).parent / "fixtures/frontend_onboarding.json").read_text(encoding="utf-8"))
    actual = {group["key"]: group for group in client.get("/api/v1/questionnaire").json()["groups"]}
    assert set(actual) == {group["key"] for group in fixture["groups"]}
    for group in fixture["groups"]:
        for field in ("kind", "options", "exclusive", "max"):
            assert actual[group["key"]][field] == group[field], (group["key"], field)
        # Q02·Q09 자유 텍스트는 프론트에 입력 UI가 아직 없어 서버 계약만 존재한다.
        if group["custom_option"] is not None:
            assert actual[group["key"]]["custom_option"] == group["custom_option"]
