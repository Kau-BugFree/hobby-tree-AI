/** 실행 중인 로컬 추천 엔진에서 프론트 상태→요청→응답→카드 변환을 검증한다. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
  fetchRecommendations, RecommendationApiError,
  toRecommendationRequest, toRecommendationCards,
} from "../examples/client.ts";

/** 실제 사용자 정보 없이 공개 프론트엔드와 같은 상태 구조로 HTTP 경계를 점검한다. */
async function main() {
  const baseUrl = (process.argv[2] ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
  const snapshot = JSON.parse(await readFile(new URL("../tests/fixtures/frontend_onboarding.json", import.meta.url), "utf8"));

  // 호출 대상이 준비되었는지 확인하고 공개 프론트엔드의 선택지와 서버 계약을 비교한다.
  const health = await fetch(`${baseUrl}/health`);
  assert.equal(health.status, 200);
  assert.equal((await health.json()).status, "ok");
  // MVP 후보가 정확히 요리·악기·독서인지 실제 배포 데이터에서도 확인한다.
  const catalogResponse = await fetch(`${baseUrl}/api/v1/catalog`);
  assert.equal(catalogResponse.status, 200);
  const catalog = await catalogResponse.json();
  assert.deepEqual(new Set(catalog.hobbies.map(hobby => hobby.id)), new Set(["cooking", "instrument", "reading"]));
  const questionnaire = await fetch(`${baseUrl}/api/v1/questionnaire`);
  assert.equal(questionnaire.status, 200);
  const { groups } = await questionnaire.json();
  assert.deepEqual(groups.map(group => group.key).sort(), snapshot.groups.map(group => group.key).sort());
  for (const expected of snapshot.groups) {
    const actual = groups.find(group => group.key === expected.key);
    for (const key of ["kind", "options", "exclusive", "max"]) {
      assert.deepEqual(actual[key], expected[key], `${expected.key}.${key}`);
    }
  }

  // 예제 입력이며 실제 앱의 초기 상태를 그대로 제출하는 패턴이 아니다.
  const state = {
    answers: {
      q02: ["독서"], q04: "직접 금액 입력", q05: "직접 금액 입력",
      q06: "직접 시간 입력", q09Exclude: ["없음"], parseConfirm: "맞아요",
    },
    customAmount: { q04: 0, q05: 0 }, customMinutes: 15,
    freeText: "밤에만 할 수 있어요",
  };
  const options = {
    confirmedQuestionKeys: ["q02", "q04", "q05", "q06", "q09Exclude"],
    resources: { owned_book: true }, monthlyCommittedKrw: 0,
  };
  const request = toRecommendationRequest(state, options);
  assert.equal(request.answers.parseConfirm, undefined);
  assert.equal(request.custom_values.q04_amount_krw, 0);
  const result = await fetchRecommendations(baseUrl, request);
  const cards = toRecommendationCards(result);
  assert.equal(result.status, "recommended");
  assert.equal(cards[0].hobby, "독서");
  assert.equal(cards[0].engineHobbyId, "reading");
  assert.equal(cards[0].needsConfirmation, false);
  assert.match(cards[0].scoreLabel, /^적합도 \d+점$/);

  // 악기 관심과 큰 소음 제외를 함께 보내 헤드폰 건반 후보·확인 안내를 검증한다.
  const instrumentResult = await fetchRecommendations(baseUrl, {
    answers: { q02: ["악기 (피아노·기타)"], q09Exclude: ["큰 소음"] },
  });
  const instrumentCard = toRecommendationCards(instrumentResult)[0];
  assert.equal(instrumentCard.hobby, "악기");
  assert.equal(instrumentCard.engineHobbyId, "instrument");
  assert.equal(instrumentCard.activityId, "instrument-keyboard-headphones-15");
  assert.equal(instrumentCard.needsConfirmation, true);

  // 사용자가 제출한 Q14 조건은 확인 없이 추천하거나 무시하지 않는다.
  const review = await fetchRecommendations(baseUrl, toRecommendationRequest(state, {
    ...options, includeFreeText: true,
  }));
  assert.equal(review.status, "needs_review");
  assert.deepEqual(toRecommendationCards(review), []);
  assert.ok(review.follow_up_questions.some(question => question.includes(state.freeText)));

  // 화면 전용 키를 직접 보내면 서버는 여전히 422로 거부한다.
  await assert.rejects(fetchRecommendations(baseUrl, { answers: { parseConfirm: "맞아요" } }), (error) => {
    assert.ok(error instanceof RecommendationApiError);
    assert.equal(error.status, 422);
    return true;
  });
  console.log("PASS: cooking/instrument/reading MVP; 14 question groups; frontend state → HTTP API → cards; instrument sound constraints; Q14 review; 422 branch.");
}

// 명령행에서 실패 원인을 확인할 수 있도록 메시지를 출력하고 비정상 종료 코드를 설정한다.
main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
