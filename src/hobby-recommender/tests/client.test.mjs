/** 외부 서버 호출 없이 요청 변환·HTTP 오류·응답 계약·카드 변환을 검증한다. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  fetchRecommendations, RecommendationApiError, RecommendationProtocolError,
  toRecommendationRequest, toRecommendationCards,
} from "../examples/client.ts";

const baseUrl = "https://recommendations.example.test";
const payload = { answers: {} };

// JSON·텍스트·HTML·빈 본문에서도 500/503 상태와 원문을 잃지 않는지 확인한다.
for (const [status, body, expectedDetail] of [
  [500, '{"detail":"server failure"}', { detail: "server failure" }],
  [503, '{"detail":"service unavailable"}', { detail: "service unavailable" }],
  [500, "Internal Server Error", "Internal Server Error"],
  [503, "<html>Service Unavailable</html>", "<html>Service Unavailable</html>"],
  [503, "", ""],
  [500, '{"detail":', '{"detail":'],
]) {
  test(`${status} preserves the HTTP error for body ${JSON.stringify(body)}`, async (t) => {
    const fetchMock = t.mock.method(globalThis, "fetch", async () => new Response(body, { status }));

    await assert.rejects(fetchRecommendations(baseUrl, payload), (error) => {
      assert.ok(error instanceof RecommendationApiError);
      assert.equal(error.status, status);
      assert.equal(error.retryable, true);
      assert.deepEqual(error.detail, expectedDetail);
      assert.match(error.message, status === 500 ? /서버 오류/ : /일시적으로 이용할 수 없습니다/);
      return true;
    });
    assert.equal(fetchMock.mock.callCount(), 1, "retries remain the caller's choice");
  });
}

// 입력 수정이 필요한 422는 재시도 안내와 구분하고 필드별 오류를 유지한다.
test("422 retains validation details and does not suggest retrying unchanged input", async (t) => {
  const body = { detail: [{ loc: ["body", "answers", "q02"], msg: "Invalid answer" }] };
  t.mock.method(globalThis, "fetch", async () => Response.json(body, { status: 422 }));

  await assert.rejects(fetchRecommendations(baseUrl, payload), (error) => {
    assert.ok(error instanceof RecommendationApiError);
    assert.equal(error.status, 422);
    assert.equal(error.retryable, false);
    assert.deepEqual(error.detail, body);
    return true;
  });
});

// 계약에 맞는 정상 응답은 점수나 순서를 바꾸지 않고 전달한다.
test("200 returns the recommendation response unchanged", async (t) => {
  const body = JSON.parse(await readFile(new URL("../examples/response.json", import.meta.url), "utf8"));
  t.mock.method(globalThis, "fetch", async () => Response.json(body));

  assert.deepEqual(await fetchRecommendations(baseUrl, payload), body);
});

// HTTP 응답 자체가 없는 연결 실패를 서버 상태 코드 오류로 위장하지 않는다.
test("network failures remain distinct from HTTP errors", async (t) => {
  const error = new TypeError("fetch failed");
  t.mock.method(globalThis, "fetch", async () => { throw error; });

  await assert.rejects(fetchRecommendations(baseUrl, payload), (actual) => actual === error);
});

// 오류 본문을 읽다가 사용자가 취소하면 AbortError를 그대로 전달한다.
test("aborting while reading an error body remains an abort", async (t) => {
  const error = new DOMException("The operation was aborted", "AbortError");
  t.mock.method(globalThis, "fetch", async () => ({
    ok: false,
    status: 503,
    text: async () => { throw error; },
  }));

  await assert.rejects(fetchRecommendations(baseUrl, payload), (actual) => actual === error);
});

// 실제 AppState 필드와 화면 전용 답변이 포함된 입력으로 API 경계 변환을 검증한다.
const frontendState = {
  answers: {
    q02: ["독서"], q04: "직접 금액 입력", q05: "1만 원까지", q06: "직접 시간 입력",
    parseConfirm: "맞아요", noticeFrequency: "주 1회",
  },
  customAmount: { q04: 0, q05: 20000 },
  customMinutes: 15,
  freeText: "밤에만 할 수 있어요",
};

// 확인한 설문만 전송하고, 직접 입력을 선택하지 않은 금액은 포함하지 않는다.
test("frontend state maps confirmed fields and preserves zero budgets", () => {
  const request = toRecommendationRequest(frontendState, {
    confirmedQuestionKeys: ["q02", "q04", "q05", "q06"],
    monthlyCommittedKrw: 0, resources: { owned_book: false },
  });
  assert.deepEqual(request, {
    schema_version: "onboarding-2026-10-13",
    answers: { q02: ["독서"], q04: "직접 금액 입력", q05: "1만 원까지", q06: "직접 시간 입력" },
    custom_values: { q04_amount_krw: 0, q06_minutes: 15 },
    monthly_committed_krw: 0, resources: { owned_book: false },
  });
  request.answers.q02.push("커피");
  assert.deepEqual(frontendState.answers.q02, ["독서"], "UI state is not mutated");
});

// 프로토타입에 값이 채워져 있어도 사용자가 확인하지 않은 필드는 보내지 않는다.
test("unconfirmed prototype defaults are not submitted", () => {
  const request = toRecommendationRequest(frontendState, { confirmedQuestionKeys: [] });
  assert.deepEqual(request.answers, {});
  assert.deepEqual(request.custom_values, {});
  assert.equal(request.context_text, undefined);
});

// Q14를 보내기로 한 경우에만 앞뒤 공백을 정리해 독립 필드에 보존한다.
test("explicitly submitted free text is preserved separately", () => {
  const request = toRecommendationRequest(frontendState, {
    confirmedQuestionKeys: [], includeFreeText: true,
  });
  assert.equal(request.context_text, frontendState.freeText);
  assert.equal(request.answers.parseConfirm, undefined);
});

// Q02·Q09 입력문은 서로 다른 필드이며 선택 여부에 따라 전송한다.
test("selected custom text fields retain their separate meanings", () => {
  const request = toRecommendationRequest({ ...frontendState, answers: {
    q02: ["다른 취미 직접 입력"], q09Exclude: ["직접 입력"],
  } }, {
    confirmedQuestionKeys: ["q02", "q09Exclude"],
    customText: { q02_other: "수채화", q09_exclude_text: "특정 재료 제외" },
  });
  assert.deepEqual(request.custom_values, { q02_other: "수채화", q09_exclude_text: "특정 재료 제외" });
});

// JavaScript 호출자가 UI 전용 키를 확인 목록에 넣어도 서버로 흘리지 않는다.
test("UI-only keys cannot be marked as survey questions", () => {
  assert.throws(() => toRecommendationRequest(frontendState, {
    confirmedQuestionKeys: ["parseConfirm"],
  }), /지원하지 않는 질문/);
});

// 경로가 같은 Spring 서버의 정상 응답을 추천 엔진 응답으로 오인하지 않는다.
test("Spring response envelope is rejected before card rendering", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({
    isSuccess: true, code: "RECOMMENDATION201_1", message: "추천 생성", result: {
      recommendationId: 1, preferencesVersion: 1, items: [], requestedCount: 3, returnedCount: 0,
    },
  }, { status: 201 }));
  await assert.rejects(fetchRecommendations(baseUrl, payload), RecommendationProtocolError);
});

// 잘못된 서버/프록시로 연결되어 200 HTML을 받으면 계약 오류로 안내한다.
test("successful HTML responses are reported as protocol errors", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("<html>Wrong server</html>"));
  await assert.rejects(fetchRecommendations(baseUrl, payload), RecommendationProtocolError);
});

// 활동이 없는 추천 카드가 undefined.title 예외로 화면을 중단시키지 않도록 검사한다.
test("malformed recommendation activities are rejected before rendering", async (t) => {
  const body = JSON.parse(await readFile(new URL("../examples/response.json", import.meta.url), "utf8"));
  body.recommendations[0].activities = [];
  t.mock.method(globalThis, "fetch", async () => Response.json(body));
  await assert.rejects(fetchRecommendations(baseUrl, payload), RecommendationProtocolError);
  assert.throws(() => toRecommendationCards(body), RecommendationProtocolError);
});

// 상태 문자열처럼 보이는 배열도 형 변환 없이 계약 오류로 거부한다.
for (const field of ["result", "activity"]) {
  test(`array-valued ${field} status is not coerced to a valid status`, async (t) => {
    const body = JSON.parse(await readFile(new URL("../examples/response.json", import.meta.url), "utf8"));
    if (field === "result") body.status = [body.status];
    else body.recommendations[0].activities[0].status = [body.recommendations[0].activities[0].status];
    t.mock.method(globalThis, "fetch", async () => Response.json(body));
    await assert.rejects(fetchRecommendations(baseUrl, payload), RecommendationProtocolError);
  });
}

// 기존 카드 필드와 함께 엔진 코드·점수 단위·미확인 조건이 보존되는지 확인한다.
test("cards keep engine IDs, score labels, and confirmation requirements", async () => {
  const body = JSON.parse(await readFile(new URL("../examples/response.json", import.meta.url), "utf8"));
  const cards = toRecommendationCards(body);
  assert.equal(cards[0].hobby, "독서");
  assert.equal(cards[0].engineHobbyId, "reading");
  assert.equal(cards[0].scoreLabel, "적합도 100점");
  const cooking = cards.find((card) => card.hobby === "요리");
  assert.equal(cooking.needsConfirmation, true);
  assert.ok(cooking.confirmationRequired.length > 0);
});

// 실제 응답 예제의 악기 후보가 기존 프론트엔드 '악기' 카드 키로 변환되는지 확인한다.
test("MVP instrument response renders with the instrument card key", async () => {
  const body = JSON.parse(await readFile(new URL("../examples/response.json", import.meta.url), "utf8"));
  const cards = toRecommendationCards(body);
  assert.deepEqual(new Set(cards.map(card => card.hobby)), new Set(["요리", "악기", "독서"]));
  const instrument = cards.find(card => card.engineHobbyId === "instrument");
  assert.equal(instrument.hobby, "악기");
  assert.equal(instrument.needsConfirmation, true);
  assert.ok(instrument.confirmationRequired.some(check => check.includes("타건 소음")));
});
