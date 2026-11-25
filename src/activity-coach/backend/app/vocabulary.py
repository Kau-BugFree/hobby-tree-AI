"""취미별 낱말 묶음: 규칙 패턴과 안내 문구에 들어가는 취미 낱말. 규칙의 문법은 intent_rules.py·slots.py에 둡니다."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Vocabulary:
    dish_nouns: tuple[str, ...]      # '다른 ○○'로 찾는 대상 낱말(OTHER_DISHES). 순서가 패턴 순서다.
    tools: tuple[str, ...]           # 없다고 말할 수 있는 도구(NO_TOOL). 한 글자 도구는 앞 글자를 보고 잡는다.
    key_tools: tuple[str, ...]       # 활동 중에도 '없다'고 하면 추천 조건으로 보는 도구
    out_of_scope: tuple[str, ...]    # 취미 밖 주제 낱말(OUT_OF_SCOPE)
    scope_notice: str                # 범위 밖 안내(OUT_OF_SCOPE_TEXT)
    capabilities_notice: str         # 능력 범위 안내(CAPABILITIES_TEXT)
    greeting_notice: str             # 인사 답(GREETING_TEXT)

    def __post_init__(self) -> None:
        # 빈 목록은 패턴에 빈 대안을 만든다('없어'만으로 이름 없는 도구를 뺀다). key_tools는 비어도 된다.
        for name in ("dish_nouns", "tools", "out_of_scope"):
            if not getattr(self, name):
                raise ValueError(f"{name}은 비어 있으면 안 됩니다.")
        if not set(self.key_tools) <= set(self.tools):
            raise ValueError(f"key_tools는 tools 안의 도구여야 합니다: {sorted(set(self.key_tools) - set(self.tools))}")


COOKING = Vocabulary(
    dish_nouns=("요리", "활동", "메뉴", "레시피", "음식"),
    # ponytail: 자주 쓰는 도구만 적었다. '식칼'·'가스레인지' 같은 말은 못 잡는다. 필요하면 카탈로그 도구 사전으로 바꾼다.
    tools=("오븐", "전자레인지", "프라이팬", "후라이팬", "냄비", "도마", "저울", "계량스푼", "계량컵", "찜기", "밀대",
           "거품기", "핸드믹서", "온도계", "뒤집개", "집게", "칼", "체", "팬"),
    key_tools=("오븐",),
    out_of_scope=("날씨", "주식", "정치", "러닝", "달리기", "커피", "독서"),
    scope_notice="현재는 요리 활동을 지원합니다. 오늘 가능한 시간이나 하고 싶은 요리를 알려주세요.",
    capabilities_notice=(
        "저는 요리 활동코치예요. 시간·도구 조건에 맞는 요리 활동 추천, 활동 선택과 준비물·단계·소요 시간 안내, "
        "단계 설명, 막힌 부분에 대한 확인 질문과 활동 원문 근거 안내, 활동 시작·완료 기록을 도와요. "
        "알레르기 판정, 검토된 자료 없이 실패 원인을 확정하는 일, 근거에 없는 수량·시간을 만드는 일, 요리 외 주제는 다루지 않아요."
    ),
    greeting_notice="요리 활동을 함께 진행할게요. 오늘 할 활동을 추천받거나 선택한 활동의 준비물·순서를 물어보세요.",
)

VOCABULARY: dict[str, Vocabulary] = {"cooking": COOKING}


def vocabulary_for(hobby_key: str) -> Vocabulary:
    """취미의 낱말 묶음을 반환합니다. 없는 취미는 KeyError입니다. 요리로 대신하지 않습니다."""
    return VOCABULARY[hobby_key]
