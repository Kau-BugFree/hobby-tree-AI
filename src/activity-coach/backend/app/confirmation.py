"""완료 확인(설계 4.4절): 에이전트 설정이 켜져 있으면 완료를 한 번 묻고, 바로 다음 턴의 답으로 기록합니다.

대기 확인은 session["pending_confirmation"]에 둡니다. 세션이 JSON으로 통째 저장되므로 재시작 뒤에도 남습니다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .action_executors import Turn

ASK = "'{title}' 활동을 완료로 기록할까요?"
DECLINED = "완료로 기록하지 않았어요."
YES = frozenset({"네", "응", "예", "맞아", "기록해줘"})
NO = frozenset({"아니", "아니요", "취소"})


def clear(session: dict) -> None:
    session.pop("pending_confirmation", None)


def pending(session: dict) -> dict | None:
    """대기 중인 확인. 물을 때의 활동·실행이 지금 세션과 다르면 지우고 None입니다."""
    waiting = session.get("pending_confirmation")
    if waiting and (waiting["activity_id"], waiting["execution_id"]) != (
            session["selected_activity_id"], session["execution_id"]):
        clear(session)
        return None
    return waiting


def ask(turn: Turn) -> None:
    """완료를 기록하지 않고 묻습니다. 코칭의 확인 질문(dialogue.pending_question)은 건드리지 않습니다."""
    session, response = turn.session, turn.response
    response.update(mode="clarify", text=ASK.format(title=turn.selected["title"]))
    session["pending_confirmation"] = {
        "kind": "complete", "activity_id": session["selected_activity_id"],
        "execution_id": session["execution_id"], "message_id": response["message_id"]}


def spoken_answer(text: str) -> bool | None:
    """규칙 경로의 답: 긍정이면 True, 부정이면 False, 그 밖은 None입니다."""
    word = text.strip().rstrip(".!~")
    return True if word in YES else False if word in NO else None
