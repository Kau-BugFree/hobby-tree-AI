/** 추천 엔진 전용 통신/변환 함수. Spring 백엔드 API와 주소·계약이 다르다. */
export const QUESTION_KEYS = [
  "q01", "q02", "q03", "q04", "q05", "q06", "q07",
  "q08", "q09Place", "q09Exclude", "q10", "q11", "q12", "q13",
] as const;
export type QuestionKey = typeof QUESTION_KEYS[number];

/** 화면 전체 상태 대신 서버가 허용하는 설문·실제 입력값만 담는 요청이다. */
export interface RecommendationRequest {
  schema_version?: "onboarding-2026-10-13";
  answers: Partial<Record<QuestionKey, string | string[] | null>>;
  custom_values?: {
    q04_amount_krw?: number;
    q05_amount_krw?: number;
    q06_minutes?: number;
    q02_other?: string;
    q09_exclude_text?: string;
  };
  context_text?: string;
  resources?: Record<string, boolean>;
  monthly_committed_krw?: number;
  top_k?: number;
}

/** 공개 프론트엔드 AppState 중 변환에 필요한 필드만 구조적으로 참조한다. */
export interface FrontendOnboardingState {
  answers: Record<string, string | string[] | null>;
  customAmount: { q04: number; q05: number };
  customMinutes: number;
  freeText: string;
}

/** 프로토타입 초기값과 실제 사용자 확인값을 구분하기 위한 명시적인 변환 옵션. */
export interface OnboardingRequestOptions {
  confirmedQuestionKeys: readonly QuestionKey[];
  includeFreeText?: boolean;
  customText?: { q02_other?: string; q09_exclude_text?: string };
  resources?: Record<string, boolean>;
  monthlyCommittedKrw?: number;
  topK?: number;
}

/**
 * 프론트엔드의 camelCase 상태를 추천 엔진의 snake_case 요청으로 바꾼다.
 * confirmedQuestionKeys에는 사용자가 입력/확인한 질문만 전달한다.
 * parseConfirm·noticeFrequency·로그인 정보·기본 hobbyChecks는 전송하지 않는다.
 */
export function toRecommendationRequest(
  state: FrontendOnboardingState,
  options: OnboardingRequestOptions,
): RecommendationRequest {
  const answers: RecommendationRequest["answers"] = {};
  // 초기값과 같은 값도 실제 선택일 수 있으므로 값 비교로 확인 여부를 추측하지 않는다.
  for (const key of options.confirmedQuestionKeys) {
    if (!(QUESTION_KEYS as readonly string[]).includes(key)) {
      throw new Error(`지원하지 않는 질문 키입니다: ${key}`);
    }
    const value = state.answers[key];
    if (value !== undefined) answers[key] = Array.isArray(value) ? [...value] : value;
  }

  // 직접 입력을 선택한 질문의 값만 보낸다. 다른 질문의 기본 금액은 포함하지 않는다.
  const custom: NonNullable<RecommendationRequest["custom_values"]> = {};
  if (answers.q04 === "직접 금액 입력") custom.q04_amount_krw = state.customAmount.q04;
  if (answers.q05 === "직접 금액 입력") custom.q05_amount_krw = state.customAmount.q05;
  if (answers.q06 === "직접 시간 입력") custom.q06_minutes = state.customMinutes;
  if (Array.isArray(answers.q02) && answers.q02.includes("다른 취미 직접 입력")) {
    custom.q02_other = options.customText?.q02_other;
  }
  if (Array.isArray(answers.q09Exclude) && answers.q09Exclude.includes("직접 입력")) {
    custom.q09_exclude_text = options.customText?.q09_exclude_text;
  }

  // Q14는 별도 필드로 보존한다. parseConfirm의 MOCK 해석을 사실로 취급하지 않는다.
  const context = options.includeFreeText ? state.freeText.trim() : "";
  return {
    schema_version: "onboarding-2026-10-13",
    answers,
    custom_values: custom,
    ...(context ? { context_text: context } : {}),
    ...(options.resources !== undefined ? { resources: { ...options.resources } } : {}),
    ...(options.monthlyCommittedKrw !== undefined ? { monthly_committed_krw: options.monthlyCommittedKrw } : {}),
    ...(options.topK !== undefined ? { top_k: options.topK } : {}),
  };
}

/** 추천된 활동의 비용·시간과 아직 확인되지 않은 조건. */
export interface Activity {
  activity_id: string;
  title: string;
  total_minutes: number;
  active_minutes: number;
  estimated_first_cost_krw: number;
  estimated_repeat_cost_krw: number;
  status: "eligible" | "needs_confirmation";
  confirmation_required: string[];
}

/** 엔진의 원본 응답이며 Spring의 isSuccess/result 응답 포장과 구분한다. */
export interface RecommendationResponse {
  status: "recommended" | "browse" | "needs_review" | "no_match";
  schema_version: string;
  rules_version: string;
  catalog_version: string;
  recommendations: Array<{
    hobby_id: string;
    name: string;
    ui_key: string;
    rank: number;
    score: number;
    breakdown: Array<{
      question_key: string;
      weight: number;
      fit: number;
      points: number;
      answered: boolean;
      selected: string[];
    }>;
    reasons: string[];
    activities: Activity[];
  }>;
  excluded: Array<{ hobby_id: string; name: string; reasons: string[] }>;
  unsupported_interests: string[];
  notices: string[];
  follow_up_questions: string[];
  preferences: {
    selection_mode: string;
    recording_mode: string | null;
    burdens: string[];
    weekly_frequency: string | null;
  };
  coverage: { weighted_answer_coverage: number; answered_groups: number; total_groups: number };
}

/** HTTP 오류의 상태와 원본 본문을 보존해 화면의 오류 분기에 전달한다. */
export class RecommendationApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  /** 500·503은 사용자 안내 문구를 제공하고 다른 상태는 호출자가 해석하게 둔다. */
  constructor(status: number, detail: unknown) {
    let message = `Recommendation API returned ${status}`;
    switch (status) {
      case 500:
        message = "추천 처리 중 서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.";
        break;
      case 503:
        message = "추천 서비스를 일시적으로 이용할 수 없습니다. 잠시 후 다시 시도해 주세요.";
        break;
    }
    super(message);
    this.name = "RecommendationApiError";
    this.status = status;
    this.detail = detail;
  }

  /** 재시도 가능 여부만 알려 주며, 자동 재시도나 중복 요청은 실행하지 않는다. */
  get retryable(): boolean {
    return this.status >= 500 && this.status < 600;
  }
}

/** 다른 서버에 연결했거나 성공 응답의 JSON 계약이 깨졌을 때 발생한다. */
export class RecommendationProtocolError extends Error {
  /** 내부 응답 본문을 화면에 노출하지 않고 주소/계약 점검 안내만 전달한다. */
  constructor() {
    super("추천 엔진의 응답 형식이 올바르지 않습니다. Spring 백엔드와 추천 엔진의 주소를 확인해 주세요.");
    this.name = "RecommendationProtocolError";
  }
}

/** null·배열을 제외한 JSON 객체인지 확인해 이후 속성 접근을 안전하게 만든다. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 화면에서 join/map하는 안내 목록이 문자열 배열인지 검사한다. */
function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

/** JSON 숫자가 표시·계산 가능한 유한한 값인지 검사한다. */
function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * HTTP 200이어도 Spring의 다른 응답을 추천 결과로 캐스팅하지 않는다.
 * 카드와 화면 분기에 쓰는 필드를 검증하고, 나머지 원본 필드는 그대로 보존한다.
 */
function parseRecommendationResponse(body: unknown): RecommendationResponse {
  if (!isRecord(body)
      || typeof body.status !== "string"
      || !["recommended", "browse", "needs_review", "no_match"].includes(body.status)
      || typeof body.schema_version !== "string"
      || typeof body.rules_version !== "string"
      || typeof body.catalog_version !== "string"
      || !Array.isArray(body.recommendations)
      || !Array.isArray(body.excluded)
      || !isStringArray(body.unsupported_interests)
      || !isStringArray(body.notices)
      || !isStringArray(body.follow_up_questions)
      || !isRecord(body.preferences)
      || !isRecord(body.coverage)) throw new RecommendationProtocolError();

  const { preferences, coverage } = body;
  if (typeof preferences.selection_mode !== "string"
      || !(preferences.recording_mode === null || typeof preferences.recording_mode === "string")
      || !(preferences.weekly_frequency === null || typeof preferences.weekly_frequency === "string")
      || !isStringArray(preferences.burdens)
      || !isNumber(coverage.weighted_answer_coverage)
      || !isNumber(coverage.answered_groups)
      || !isNumber(coverage.total_groups)) throw new RecommendationProtocolError();

  // 추천 상태에 후보가 없거나 다른 상태에 추천이 섞여 있으면 잘못된 계약이다.
  if ((body.status === "recommended") !== (body.recommendations.length > 0)) {
    throw new RecommendationProtocolError();
  }
  for (const item of body.recommendations) {
    if (!isRecord(item) || typeof item.hobby_id !== "string" || typeof item.name !== "string"
        || typeof item.ui_key !== "string" || !isNumber(item.rank) || !isNumber(item.score)
        || !isStringArray(item.reasons) || !Array.isArray(item.breakdown)
        || !Array.isArray(item.activities) || item.activities.length === 0) {
      throw new RecommendationProtocolError();
    }
    for (const activity of item.activities) {
      if (!isRecord(activity) || typeof activity.activity_id !== "string" || typeof activity.title !== "string"
          || !isNumber(activity.total_minutes) || !isNumber(activity.active_minutes)
          || !isNumber(activity.estimated_first_cost_krw) || !isNumber(activity.estimated_repeat_cost_krw)
          || typeof activity.status !== "string"
          || !["eligible", "needs_confirmation"].includes(activity.status)
          || !isStringArray(activity.confirmation_required)) throw new RecommendationProtocolError();
    }
    for (const part of item.breakdown) {
      if (!isRecord(part) || typeof part.question_key !== "string" || !isNumber(part.weight)
          || !isNumber(part.fit) || !isNumber(part.points) || typeof part.answered !== "boolean"
          || !isStringArray(part.selected)) throw new RecommendationProtocolError();
    }
  }
  for (const excluded of body.excluded) {
    if (!isRecord(excluded) || typeof excluded.hobby_id !== "string" || typeof excluded.name !== "string"
        || !isStringArray(excluded.reasons)) throw new RecommendationProtocolError();
  }
  return body as unknown as RecommendationResponse;
}

/**
 * 추천 엔진의 baseUrl로 요청하고 HTTP 오류·응답 계약 오류·네트워크 오류를 구분한다.
 * token은 앞단 게이트웨이용 선택 헤더이며, 이 Python 서비스 자체는 JWT를 검증하지 않는다.
 */
export async function fetchRecommendations(
  baseUrl: string,
  payload: RecommendationRequest,
  options: { signal?: AbortSignal; timeoutMs?: number; token?: string } = {},
): Promise<RecommendationResponse> {
  const controller = new AbortController();
  // 사용자의 취소 신호와 시간 제한을 같은 요청 컨트롤러로 전달한다.
  const abort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  else options.signal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(abort, options.timeoutMs ?? 8000);
  try {
    const response = await fetch(`${baseUrl.replace(/\/+$/, "")}/api/v1/recommendations`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}),
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      const text = await response.text();
      let detail: unknown = text;
      try {
        detail = JSON.parse(text);
      } catch {
        // 프록시의 HTML·일반 텍스트·빈 오류 응답에서도 HTTP 상태를 보존한다.
      }
      throw new RecommendationApiError(response.status, detail);
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch (error) {
      // 정상 상태의 HTML/빈 본문은 주소·계약 오류다. 취소·네트워크 오류는 유지한다.
      if (error instanceof SyntaxError) throw new RecommendationProtocolError();
      throw error;
    }
    return parseRecommendationResponse(body);
  } finally {
    // 성공·실패·취소 어느 경우든 타이머와 이벤트 핸들러가 남지 않게 정리한다.
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}

/** 프론트엔드 HobbyKey 중 현재 추천 엔진이 실제 지원하는 세 가지 화면 키. */
type CurrentAppHobbyKey = "요리" | "악기" | "독서";

/** 엔진의 ui_key가 현재 카드 렌더러에서 지원되는지 확인한다. */
function currentAppKey(key: string): CurrentAppHobbyKey {
  if (key === "요리" || key === "악기" || key === "독서") return key;
  throw new Error(`Add the new hobby to the app's card renderer: ${key}`);
}

/** 기존 RecommendedHobby 구조에 맞추되 활동 코드·확인 사항·점수 표기를 보존한다. */
export function toRecommendationCards(response: RecommendationResponse) {
  // fetch를 거치지 않은 저장 응답/외부 입력도 빈 활동 목록으로 화면을 깨뜨리지 않는다.
  parseRecommendationResponse(response);
  return response.recommendations.map((recommendation) => {
    const activity = recommendation.activities[0];
    return {
      hobby: currentAppKey(recommendation.ui_key), // MVP의 요리·악기·독서 화면 키만 허용한다.
      engineHobbyId: recommendation.hobby_id, // Spring의 숫자 hobbyId와 구분한 엔진 코드다.
      name: recommendation.name,
      match: Math.round(recommendation.score), // 기존 카드 필드 호환용 점수이며 확률이 아니다.
      scoreLabel: `적합도 ${Math.round(recommendation.score)}점`,
      why: recommendation.reasons.join(" · "),
      firstActivity: activity.title,
      activityId: activity.activity_id,
      needsConfirmation: activity.status === "needs_confirmation",
      confirmationRequired: activity.confirmation_required,
    };
  });
}
