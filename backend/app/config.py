"""서버 설정. Claude API 키는 .env 또는 서버 환경변수에서만 읽습니다."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
# 산학프로젝트/rag_test/models: rag_test 실험에서 내려받은 로컬 모델 스냅샷을 복사 없이 읽는다.
MODELS_DIR = ROOT.parents[2] / "rag_test" / "models"


def path_env(name: str, default: Path) -> Path:
    """상대 경로는 실행 위치와 무관하게 backend 폴더 기준으로 해석합니다."""
    value = Path(os.getenv(name, str(default))).expanduser()
    return value if value.is_absolute() else ROOT / value


def flag_env(name: str, default: str = "true") -> bool:
    """'true'만 켜짐으로 읽습니다. 오타가 조용히 켜짐으로 해석되지 않게 합니다."""
    return os.getenv(name, default).strip().lower() == "true"


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
            rerank_candidates=int(os.getenv("COACH_RERANK_CANDIDATES", "20")),
            dense_min_similarity=float(os.getenv("COACH_DENSE_MIN_SIMILARITY", "0.40")),
            rerank_min_score=float(os.getenv("COACH_RERANK_MIN_SCORE", "-2.0")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            claude_model=os.getenv("CLAUDE_MODEL", cls.claude_model),
            claude_effort=os.getenv("CLAUDE_EFFORT", cls.claude_effort),
            claude_max_tokens=int(os.getenv("CLAUDE_MAX_TOKENS", "16000")),
            claude_timeout_seconds=float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "120")),
            claude_max_retries=int(os.getenv("CLAUDE_MAX_RETRIES", "2")),
            cors_origins=tuple(
                x.strip()
                for x in os.getenv(
                    "COACH_CORS_ORIGINS", ",".join(cls.cors_origins)
                ).split(",")
                if x.strip()
            ),
        )
