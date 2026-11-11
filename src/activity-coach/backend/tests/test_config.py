"""환경 변수 설정 검증: 잘못된 값은 시작할 때 어떤 변수가 문제인지 알리고 멈춥니다.

실제 .env는 읽지 않습니다.
"""
import pytest

from app.config import ROOT, Settings

# 환경 변수 이름과 그 값을 받는 Settings 필드
NUMBERS = {"COACH_RERANK_CANDIDATES": "rerank_candidates", "COACH_DENSE_MIN_SIMILARITY": "dense_min_similarity",
           "COACH_RERANK_MIN_SCORE": "rerank_min_score", "CLAUDE_MAX_TOKENS": "claude_max_tokens",
           "CLAUDE_TIMEOUT_SECONDS": "claude_timeout_seconds", "CLAUDE_MAX_RETRIES": "claude_max_retries"}
PATHS = ("COACH_DATA_DIR", "COACH_INDEX_DIR", "COACH_DATABASE_PATH")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """개발 PC의 .env와 셸 환경 변수가 결과를 바꾸지 않게 합니다."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *args, **kwargs: None)
    for name in (*NUMBERS, *PATHS):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("name, value", [
    ("COACH_RERANK_CANDIDATES", "0"), ("COACH_RERANK_CANDIDATES", "many"), ("COACH_RERANK_CANDIDATES", "2.5"),
    ("COACH_DENSE_MIN_SIMILARITY", "nan"), ("COACH_DENSE_MIN_SIMILARITY", "1.5"),
    ("COACH_RERANK_MIN_SCORE", "inf"), ("COACH_RERANK_MIN_SCORE", "-inf"),
    ("CLAUDE_MAX_TOKENS", "0"), ("CLAUDE_TIMEOUT_SECONDS", "-1"), ("CLAUDE_MAX_RETRIES", "-1"),
])
def test_invalid_number_stops_startup_and_names_the_variable(monkeypatch, name, value):
    """nan·inf·범위 밖 값으로 시작한 서버는 검색·응답에서 500을 낸다. 시작할 때 막는다."""
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("name", NUMBERS)
def test_blank_number_uses_the_default(monkeypatch, name):
    monkeypatch.setenv(name, "")
    assert getattr(Settings.from_env(), NUMBERS[name]) == getattr(Settings(), NUMBERS[name])


def test_blank_path_uses_the_default(monkeypatch):
    """빈 COACH_INDEX_DIR이 backend 폴더 자체를 색인 폴더로 만들지 않습니다."""
    for name in PATHS:
        monkeypatch.setenv(name, "")
    settings = Settings.from_env()
    assert settings.index_dir == ROOT / "var" / "index"
    assert settings.database_path == ROOT / "var" / "coach.sqlite"
    assert settings.data_dir == ROOT / "data"


def test_allowed_hosts_default_to_this_computer_and_can_be_configured(monkeypatch):
    """기본값은 이 PC의 주소뿐입니다. 다른 주소로 서비스하려면 운영자가 명시해야 합니다."""
    monkeypatch.delenv("COACH_ALLOWED_HOSTS", raising=False)
    assert Settings.from_env().allowed_hosts == ("127.0.0.1", "localhost")
    monkeypatch.setenv("COACH_ALLOWED_HOSTS", "coach.example.com, 10.0.0.5")
    assert Settings.from_env().allowed_hosts == ("coach.example.com", "10.0.0.5")
    # 빈 값이나 쉼표만 있는 값으로 허용 목록이 비면 모든 요청이 거부된다. 기본값으로 돌린다.
    for blank in ("", " , ,"):
        monkeypatch.setenv("COACH_ALLOWED_HOSTS", blank)
        assert Settings.from_env().allowed_hosts == ("127.0.0.1", "localhost")


def test_backend_token_is_off_by_default_and_never_printed(monkeypatch):
    """백엔드 연동 토큰은 운영자가 넣어야 켜지고, 설정을 출력해도 값이 드러나지 않습니다."""
    monkeypatch.delenv("COACH_BACKEND_TOKEN", raising=False)
    assert Settings.from_env().backend_token == ""
    token = "secret-for-test-" * 2
    monkeypatch.setenv("COACH_BACKEND_TOKEN", f"  {token}  ")
    settings = Settings.from_env()
    assert settings.backend_token == token
    assert token not in repr(settings)


def test_short_backend_token_stops_startup_without_printing_it(monkeypatch):
    """요청 수 제한이 없어 짧은 토큰은 되풀이해 맞힐 수 있습니다. 시작할 때 막고, 오류에 값을 싣지 않습니다."""
    monkeypatch.setenv("COACH_BACKEND_TOKEN", "short-token-value")
    with pytest.raises(ValueError, match="COACH_BACKEND_TOKEN") as error:
        Settings.from_env()
    assert "short-token-value" not in str(error.value)
    monkeypatch.setenv("COACH_BACKEND_TOKEN", "a" * 32)
    assert Settings.from_env().backend_token == "a" * 32


def test_valid_numbers_are_read(monkeypatch):
    monkeypatch.setenv("COACH_RERANK_CANDIDATES", "30")
    monkeypatch.setenv("COACH_RERANK_MIN_SCORE", "-1.5")
    settings = Settings.from_env()
    assert settings.rerank_candidates == 30 and settings.rerank_min_score == -1.5
