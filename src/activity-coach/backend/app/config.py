"""서버 설정. Claude API 키는 .env 또는 서버 환경변수에서만 읽습니다."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
# The delivery folder owns its model snapshots, independent of previous versions.
MODELS_DIR = ROOT.parent / "models"


def path_env(name: str, default: Path) -> Path:
    """상대 경로는 실행 위치와 무관하게 backend 폴더 기준으로 해석합니다. 빈 값은 기본 경로입니다."""
    value = Path(os.getenv(name) or str(default)).expanduser()
    return value if value.is_absolute() else ROOT / value


def number_env(name: str, default: str, cast, minimum: float, maximum: float):
    """숫자 설정을 읽습니다. 빈 값은 기본값이고, 숫자가 아니거나 범위 밖이면 어떤 변수가 문제인지 알리고 멈춥니다."""
    raw = os.getenv(name, "").strip() or default
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r}: 숫자가 아닙니다.") from None
    # nan은 어떤 비교도 통과하지 못하고 inf는 상한에 걸린다. 이런 값으로 시작한 서버는 검색·응답에서 500을 낸다.
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}={raw!r}: {minimum}~{maximum} 범위여야 합니다.")
    return value


def flag_env(name: str, default: str = "true") -> bool:
    """'true'만 켜짐으로 읽습니다. 오타가 조용히 켜짐으로 해석되지 않게 합니다."""
    return os.getenv(name, default).strip().lower() == "true"


def token_env(name: str, minimum: int = 32) -> str:
    """공유 토큰을 읽습니다. 빈 값은 '설정 안 함'입니다. 짧은 값은 값을 드러내지 않고 멈춥니다."""
    value = os.getenv(name, "").strip()
    # 토큰 경로에는 요청 수 제한이 없다. 짧은 값은 요청을 되풀이해 맞힐 수 있다.
    if value and len(value) < minimum:
        raise ValueError(f"{name}: {minimum}자 이상의 무작위 값이어야 합니다.")
    return value


@dataclass
class Settings:
    """테스트에서 경로·키를 직접 지정할 수 있는 설정 객체입니다."""

    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    index_dir: Path = field(default_factory=lambda: ROOT / "var" / "index")
    database_path: Path = field(default_factory=lambda: ROOT / "var" / "coach.sqlite")
    embedding_model_path: Path = field(default_factory=lambda: MODELS_DIR / "embedding" / "BGE-m3-ko")
    reranker_model_path: Path = field(default_factory=lambda: MODELS_DIR / "reranker" / "KURE-Reranker-nano")
    embedding_enabled: bool = True
    reranker_enabled: bool = True
    device: str = "auto"
    rerank_candidates: int = 20
    dense_min_similarity: float = 0.40
    # KURE-Reranker-nano raw logit 하한. 2026-11-03 측정: 정답 9건 최저 -0.35, 무관 질문 10건 최고 -3.64.
    # 두 분포 사이의 -2.0(sigmoid 약 0.12)으로 정했다. 표본이 작으므로 질문 세트가 늘면 다시 맞춘다.
    rerank_min_score: float = -2.0
    anthropic_api_key: str = field(default="", repr=False)
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    claude_max_tokens: int = 16000
    claude_timeout_seconds: float = 120.0
    claude_max_retries: int = 2
    cors_origins: tuple[str, ...] = (
        "http://localhost:8081",
        "http://127.0.0.1:8081",
        "http://localhost:19006",
    )
    # 요청의 Host 헤더로 받아들일 이름. 기본값은 이 PC뿐이다. 다른 주소로 서비스하려면 운영자가 명시한다.
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    # Spring 백엔드가 /backend 경로를 부를 때 보내는 공유 토큰. 비어 있으면 그 경로는 닫혀 있다.
    backend_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        """.env는 기존 운영체제 환경변수를 덮어쓰지 않습니다. 키를 출력하지 않습니다."""
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            data_dir=path_env("COACH_DATA_DIR", ROOT / "data"),
            index_dir=path_env("COACH_INDEX_DIR", ROOT / "var/index"),
            database_path=path_env("COACH_DATABASE_PATH", ROOT / "var/coach.sqlite"),
            embedding_model_path=path_env("COACH_EMBEDDING_MODEL_PATH", MODELS_DIR / "embedding" / "BGE-m3-ko"),
            reranker_model_path=path_env("COACH_RERANKER_MODEL_PATH", MODELS_DIR / "reranker" / "KURE-Reranker-nano"),
            embedding_enabled=flag_env("COACH_EMBEDDING_ENABLED"),
            reranker_enabled=flag_env("COACH_RERANKER_ENABLED"),
            device=os.getenv("COACH_DEVICE", "auto"),
            rerank_candidates=number_env("COACH_RERANK_CANDIDATES", "20", int, 1, 200),
            dense_min_similarity=number_env("COACH_DENSE_MIN_SIMILARITY", "0.40", float, -1, 1),
            rerank_min_score=number_env("COACH_RERANK_MIN_SCORE", "-2.0", float, -100, 100),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            claude_model=os.getenv("CLAUDE_MODEL", cls.claude_model),
            claude_effort=os.getenv("CLAUDE_EFFORT", cls.claude_effort),
            claude_max_tokens=number_env("CLAUDE_MAX_TOKENS", "16000", int, 1, 1_000_000),
            claude_timeout_seconds=number_env("CLAUDE_TIMEOUT_SECONDS", "120", float, 1, 3600),
            claude_max_retries=number_env("CLAUDE_MAX_RETRIES", "2", int, 0, 10),
            cors_origins=tuple(
                x.strip()
                for x in os.getenv(
                    "COACH_CORS_ORIGINS", ",".join(cls.cors_origins)
                ).split(",")
                if x.strip()
            ),
            # 빈 값이나 쉼표만 있는 값으로 목록이 비면 모든 요청이 거부되므로 기본값을 쓴다.
            allowed_hosts=tuple(
                x.strip() for x in os.getenv("COACH_ALLOWED_HOSTS", "").split(",") if x.strip()
            ) or cls.allowed_hosts,
            backend_token=token_env("COACH_BACKEND_TOKEN"),
        )
