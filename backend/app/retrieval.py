"""활동 검색: SQLite BM25 + BGE-m3-ko 임베딩 + RRF + KURE-Reranker-nano 재정렬.

활동 카탈로그는 서버 시작 시 색인하고, 자료나 임베딩 모델이 바뀌면 벡터를 다시 만든다.
모델이나 벡터가 없을 때는 BM25와 규칙 재정렬만 사용하며 진단에도 그대로 쓴다.
이 모듈은 생성 LLM이나 외부 API를 호출하지 않는다. 모델은 rag_test/models의 로컬 폴더만 읽는다.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

INDEX_VERSION = "hobbytree-fts-v2.0"
WORDS = re.compile(r"[가-힣]+|[a-z]+|\d+", re.I)
STOP_WORDS = {
    "요리", "활동", "추천", "추천해", "추천해줘", "추천해주세요", "해줘", "해주세요",
    "알려줘", "알려주세요", "오늘", "할", "할수있는", "수", "있는", "뭐", "뭐하지",
    "만들기", "만들", "만들어", "싶어", "싶어요", "가능", "분", "시간", "정도",
    "레시피", "요리법", "recipe", "recipes", "recommend", "cooking", "please", "a", "the",
    "설명", "설명해줘", "설명해주세요", "강의", "없이", "없어", "없어요", "있어", "있어요",
    "가능한", "가능해", "가능해요", "안에", "이내", "해볼", "해볼까",
    "방법", "방법은", "과정", "어떻게", "할까", "해볼까요",
}
# 한국어 질문으로 영어 보조 자료를 찾을 때만 쓰는 명시적 검색어 사전이다.
# 번역 모델을 실행하거나 원문 레시피를 한국어로 바꾸는 기능은 아니다.
# 2026-11-05 추가분은 레시피 54만 건의 제목 검색으로 실제 자료가 있는 영어 표현만 골랐다.
# 떡·국·밥·가지·파·무처럼 짧거나 뜻이 여러 개인 단어는 다른 단어 안에서 잘못 걸리므로 넣지 않는다.
QUERY_ALIASES = {
    "감자": "potato potatoes", "닭": "chicken", "닭고기": "chicken", "계란": "egg eggs",
    "달걀": "egg eggs", "돼지고기": "pork", "소고기": "beef", "두부": "tofu",
    "파스타": "pasta", "빵": "bread", "쿠키": "cookie cookies", "수프": "soup",
    "샐러드": "salad", "드레싱": "dressing", "간장": "soy sauce", "볶음밥": "fried rice",
    "초보": "입문 기초 계량", "입문": "기초 계량", "양파": "onion", "초콜릿": "chocolate",
    # 한식: 로마자 표기와 영어 레시피 제목의 일반 명칭
    "김치": "kimchi", "찌개": "jjigae stew", "된장": "doenjang soybean paste", "불고기": "bulgogi",
    "떡볶이": "tteokbokki", "비빔밥": "bibimbap", "잡채": "japchae", "갈비": "galbi kalbi",
    "고추장": "gochujang", "김밥": "kimbap gimbap korean sushi", "만두": "mandu dumpling dumplings",
    "순두부": "sundubu soft tofu", "파전": "pajeon", "삼겹살": "samgyeopsal samgyupsal pork belly",
    "냉면": "naengmyeon cold noodles", "호떡": "hotteok", "미역": "miyeok seaweed",
    "미역국": "miyeokguk seaweed soup", "삼계탕": "samgyetang", "떡국": "tteokguk rice cake soup",
    "짜장면": "jajangmyeon black bean noodles", "부대찌개": "budae army stew", "제육": "jeyuk spicy pork",
    "나물": "namul", "콩나물": "bean sprouts",
    # 재료
    "시금치": "spinach", "오이": "cucumber", "오이스터": "oyster", "굴소스": "oyster sauce",
    "호박": "pumpkin squash", "애호박": "zucchini", "고구마": "sweet potato", "버섯": "mushroom mushrooms",
    "당근": "carrot carrots", "토마토": "tomato tomatoes", "마늘": "garlic", "새우": "shrimp prawn prawns",
    "연어": "salmon", "참치": "tuna", "생선": "fish", "고등어": "mackerel", "오징어": "squid",
    "문어": "octopus", "조개": "clam clams", "장어": "eel", "치즈": "cheese", "우유": "milk",
    "사과": "apple apples", "바나나": "banana", "딸기": "strawberry strawberries", "레몬": "lemon",
    "배추": "cabbage", "브로콜리": "broccoli",
    # 요리 종류
    "국수": "noodle noodles", "라면": "ramen", "카레": "curry", "스테이크": "steak", "피자": "pizza",
    "케이크": "cake", "머핀": "muffin muffins", "팬케이크": "pancake pancakes", "와플": "waffle waffles",
    "스튜": "stew", "샌드위치": "sandwich", "버거": "burger burgers hamburger", "오믈렛": "omelet omelette",
    "리조또": "risotto", "라자냐": "lasagna", "타코": "taco tacos", "타코야키": "takoyaki",
    "브라우니": "brownie brownies", "푸딩": "pudding", "스무디": "smoothie", "그라탕": "gratin",
}
# 다른 단어 안에서 잘못 걸리는 짧은 별칭만 경계를 둔다. 나머지는 '김치찌개'의 찌개처럼 부분어로 찾는다.
# '초보라면'의 연결 어미, '오이스터', '애호박', '타코야키'를 각각 라면·오이·호박·타코로 바꾸지 않는다.
ALIAS_PATTERNS = {"라면": r"(?<![가-힣])라면", "오이": r"오이(?!스터)", "호박": r"(?<!애)호박", "타코": r"타코(?!야키)"}
OUTSIDE_COOKING = re.compile(r"러닝|달리기|마라톤|독서|책\s*추천|피아노|기타\s*연주|코딩|주식|날씨|축구|영화|양자역학|우주여행|자동차|암호화폐")
# 문서 토큰화와 별개로 질문에서만 제거한다. 색인 지문이나 기존 벡터를 바꾸지 않는다.
# 이런 설명 요청어가 우연히 문서에 나온 것만으로 주제가 맞다고 판단하지 않는다.
QUERY_ONLY_STOP_WORDS = {
    "원리", "이유", "정보", "개념", "과정", "방법", "설명", "궁금", "궁금해", "궁금해요",
    "부탁", "부탁해", "부탁해요", "대해", "대한", "관한", "관해", "싶다", "싶습니다",
}
KOREAN_PARTICLES = ("으로부터", "에서부터", "에게서", "으로", "에서", "에게", "한테", "이랑", "까지", "부터", "처럼", "보다", "을", "를", "의", "로", "랑")


def strip_query_particle(word: str) -> str:
    """질문의 명사 뒤 조사만 보수적으로 제거한다. '오이/프라이'처럼 모호한 이·가는 건드리지 않는다."""
    if not re.fullmatch(r"[가-힣]+", word):
        return word
    for particle in KOREAN_PARTICLES:
        if word.endswith(particle) and len(word) > len(particle):
            return word[:-len(particle)]
    return word


TOPIC_PARTICLES = ("은", "는", "이", "가")


def strip_topic_particle(word: str, known_text: str | None) -> str:
    """'밥은'처럼 문서에 없는 형태만 은/는/이/가를 떼어 문서에 있는 명사로 바꾼다.

    '오이'·'달걀프라이'처럼 단어 자체가 문서에 있으면 그대로 둔다. known_text는 활동 문서를
    공백 없이 이어 붙인 소문자 본문이다. 없으면 기존처럼 이·가·은·는을 건드리지 않는다.
    """
    if known_text is None or len(word) < 2 or not word.endswith(TOPIC_PARTICLES) or word in known_text:
        return word
    stem = word[:-1]
    return stem if stem in known_text else word


def matched_aliases(subject: str, scope: str) -> list[str]:
    """질문 주제에 든 별칭의 검색어를 반환한다. 레시피 범위가 아니면 초보·입문만 쓴다."""
    words: list[str] = []
    for korean, alternatives in QUERY_ALIASES.items():
        if (scope == "recipes" or korean in {"초보", "입문"}) \
                and re.search(ALIAS_PATTERNS.get(korean, re.escape(korean)), subject):
            words.extend(alternatives.split())
    return list(dict.fromkeys(words))


def query_anchors(subject: str, scope: str) -> list[str]:
    """부분 글자 조각이 아닌 완전한 주제어와 명시적 번역 별칭을 반환한다."""
    return list(dict.fromkeys(subject.split() + matched_aliases(subject, scope)))


def has_topic_anchor(text: str, anchors: list[str]) -> bool:
    """한글 합성명사는 부분어를 허용하되 영어는 단어 경계를 확인한다. 한글 n-gram 잡음을 막는다."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    korean_compact = re.sub(r"\s+", "", normalized)
    for anchor in anchors:
        if re.fullmatch(r"[가-힣]+", anchor):
            if anchor in korean_compact:
                return True
        elif re.search(r"\b" + re.escape(anchor) + r"\b", normalized):
            return True
    return False


def tokens(text: str) -> list[str]:
    """한글 부분어와 영어 단어를 검색용 토큰으로 만든다. 표시 문장은 바꾸지 않는다."""
    result: list[str] = []
    for word in WORDS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if word in STOP_WORDS or word.isdigit():
            continue
        result.append(word)
        if re.fullmatch(r"[가-힣]+", word) and len(word) > 2:
            result.extend(word[i:i + size] for size in (2, 3) for i in range(len(word) - size + 1))
    return result


def query_terms(query: str, scope: str, known_text: str | None = None) -> tuple[str, list[str]]:
    """요청 표현·시간 조건을 빼고 검색할 주제를 만든다. 반환값은 주제와 검색 토큰이다."""
    subject = re.sub(r"\d+(?:\.\d+)?\s*(?:분|시간)(?:만|안에|이내|동안)?", " ", query.casefold())
    subject = re.sub(r"추천(?:해\s*줘|해주세요|해줘요)?|알려(?:\s*줘|주세요)|할\s*수\s*있는|뭐\s*하지", " ", subject)
    # '먹어보고 싶어요' 같은 희망 표현은 요청 방식이다. 앞의 음식·재료 명사를 남긴다.
    subject = re.sub(r"[가-힣]+고\s*싶(?:어요|습니다|어|다)(?=\s|[.!?]|$)", " ", subject)
    words = [strip_topic_particle(strip_query_particle(w), known_text) for w in WORDS.findall(subject) if not w.isdigit()]
    words = [w for w in words if w not in STOP_WORDS and w not in QUERY_ONLY_STOP_WORDS]
    subject = " ".join(words)
    expanded = " ".join([subject, *matched_aliases(subject, scope)])
    return subject, list(dict.fromkeys(tokens(expanded)))[:48]


def document_text(row: dict[str, Any]) -> str:
    """추천 근거가 될 제목·목표·준비물·단계를 묶는다. 리뷰·원문 링크는 포함하지 않는다."""
    return "\n".join(str(v) for v in [
        row.get("title", ""), row.get("description") or "",
        " ".join(row.get("categories", [])), " ".join(row.get("tools", [])),
        " ".join(row.get("materials", [])), " ".join(row.get("steps", [])),
        " ".join(row.get("checks", [])),
    ] if v)


def normalize_tool(value: str) -> str:
    """알려진 도구의 표현만 통일한다. 알 수 없는 표현은 그대로 두어 과도한 충족을 막는다."""
    value = re.sub(r"\s+", "", value.casefold())
    aliases = {
        "프라이팬": "팬", "후라이팬": "팬", "fryingpan": "팬", "pan": "팬",
        "작은볼": "볼", "작업용볼": "볼", "작업용그릇": "볼", "bowl": "볼",
        "숟가락": "스푼", "spoon": "스푼", "계량스푼": "계량스푼", "measuringspoon": "계량스푼",
        "조리용칼": "칼", "knife": "칼", "도마": "도마", "cuttingboard": "도마",
        "pot": "냄비", "oven": "오븐", "microwave": "전자레인지",
    }
    return aliases.get(value, value)


def tools_match(required_json: str, available_json: str) -> int:
    """명시한 보유 도구로 모든 필수 도구를 충족할 때 1을 반환하는 SQLite 함수다."""
    required = json.loads(required_json)
    available = {normalize_tool(v) for v in json.loads(available_json)}
    for value in required:
        if re.search(r"\s또는\s|\sor\s", value):
            alternatives = re.split(r"\s또는\s|\sor\s", value)
            if not any(normalize_tool(x) in available for x in alternatives):
                return 0
        else:
            # '도마·칼'은 두 도구를 모두 뜻한다. 설명이 복잡한 미상 도구는 보수적으로 불충족.
            parts = [x for x in re.split(r"[·/,]", value) if x.strip()]
            if not all(normalize_tool(x) in available for x in parts):
                return 0
    return 1


def excluded_tools_match(required_json: str, excluded_json: str) -> int:
    """사용하지 않을 도구가 필수인 활동을 제외한다. 대안 도구가 명시돼 있으면 대안을 허용한다."""
    excluded = {normalize_tool(value) for value in json.loads(excluded_json)}
    for value in json.loads(required_json):
        if re.search(r"\s또는\s|\sor\s", value):
            options = re.split(r"\s또는\s|\sor\s", value)
            if all(any(tool in normalize_tool(option) for tool in excluded) for option in options):
                return 0
        elif any(tool in normalize_tool(value) for tool in excluded):
            return 0
    return 1


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """압축 여부에 맞춰 한 줄씩 읽는다. 대규모 입력 본문 전체를 메모리에 올리지 않는다."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def input_version(paths: list[Path]) -> str:
    """파일 내용과 색인 규칙 버전으로 지문을 만든다. 절대 경로는 결과에 기록하지 않는다."""
    digest = hashlib.sha256(INDEX_VERSION.encode())
    for path in paths:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    """연결마다 도구 필터 함수를 등록한다. 각 호출이 별도 연결을 쓰므로 스레드 간 공유하지 않는다."""
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.create_function("tools_match", 2, tools_match, deterministic=True)
    connection.create_function("excluded_tools_match", 2, excluded_tools_match, deterministic=True)
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def initialize_database(path: Path) -> None:
    """FTS5 본문과 필터 메타데이터를 담을 테이블을 준비한다. 기존 데이터는 유지한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL, hobby TEXT NOT NULL, total_minutes REAL,
            difficulty REAL, budget REAL, tools_json TEXT NOT NULL,
            tools_known INTEGER NOT NULL, quality TEXT NOT NULL,
            payload TEXT NOT NULL, dense_pos INTEGER
        );
        CREATE INDEX IF NOT EXISTS records_scope ON records(scope,hobby);
        -- 수십만 건에서도 조건별 집계가 큰 본문 JSON을 전부 읽지 않도록
        -- 숫자/품질 메타데이터를 작은 covering index에 함께 보관한다.
        CREATE INDEX IF NOT EXISTS records_filters ON records(
            scope,hobby,quality,total_minutes,difficulty,budget,tools_known
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
            title, body, tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)


def build_lexical_index(paths: list[Path], index_dir: Path, scope: str = "activities") -> dict[str, Any]:
    """선택한 자료 범위만 트랜잭션으로 교체하고 BM25 색인을 만든다.

    정상 레코드만 넣고 격리 자료는 건너뛴다. 원문 ID/URL을 추가하지 않는다.
    색인이 바뀌면 이전 dense_pos를 초기화하므로 오래된 벡터를 새 자료에 붙이지 않는다.
    """
    if scope not in {"activities", "recipes", "knowledge"}:
        raise ValueError("scope must be activities or recipes")
    version = input_version(paths)
    database = index_dir / "retrieval.sqlite"
    initialize_database(database)
    count = skipped = duplicates = 0
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM search_fts WHERE rowid IN (SELECT rowid FROM records WHERE scope=?)", (scope,))
        db.execute("DELETE FROM records WHERE scope=?", (scope,))
        for path in paths:
            if scope == "knowledge":
                from .knowledge import load_knowledge, index_record
                source_rows = (index_record(r) for r in load_knowledge(path.parent))
            else:
                source_rows = iter_jsonl(path)
            for row in source_rows:
                if row.get("quality", {}).get("status") == "quarantined" or not row.get("activity_id") or not row.get("title") or not row.get("steps"):
                    skipped += 1
                    continue
                tools = row.get("tools", [])
                tools_known = not ("tools_unknown" in row.get("quality", {}).get("flags", []))
                cursor = db.execute("""INSERT OR IGNORE INTO records
                    (id,scope,hobby,total_minutes,difficulty,budget,tools_json,tools_known,quality,payload)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                    row["activity_id"], scope, row.get("hobby_key", "cooking"),
                    row.get("duration", {}).get("total_minutes"), row.get("difficulty"),
                    row.get("estimated_cost"), json.dumps(tools, ensure_ascii=False), int(tools_known),
                    row.get("quality", {}).get("status", "restricted"),
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                ))
                if not cursor.rowcount:
                    duplicates += 1
                    continue
                db.execute("INSERT INTO search_fts(rowid,title,body) VALUES (?,?,?)", (
                    cursor.lastrowid, " ".join(tokens(row["title"])), " ".join(tokens(document_text(row))),
                ))
                count += 1
        meta = {"version": version, "index_version": INDEX_VERSION, "count": count, "scope": scope}
        db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("index:" + scope, json.dumps(meta)))
    return {**meta, "skipped": skipped, "duplicate_ids": duplicates}


def build_dense_index(index_dir: Path, encoder: Any, scope: str = "activities") -> dict[str, Any]:
    """현재 BM25 자료 전체를 임베딩해 NumPy 파일로 저장한다.

    벡터와 모델/자료 지문이 모두 완성된 뒤 manifest를 교체한다. 서버는 manifest의
    자료 버전·모델 이름이 현재 색인과 같은 벡터만 사용한다. 활동 162개 규모를 전제로
    한 번에 임베딩한다. 수십만 건 자료는 조각 단위 인코딩이 필요하다.
    """
    import numpy as np
    database = index_dir / "retrieval.sqlite"
    with connect(database) as db:
        meta_row = db.execute("SELECT value FROM metadata WHERE key=?", ("index:" + scope,)).fetchone()
        if not meta_row:
            raise ValueError("Build the lexical index before the dense index")
        rows = db.execute("SELECT id,payload FROM records WHERE scope=? ORDER BY rowid", (scope,)).fetchall()
        if not rows:
            raise ValueError("Cannot embed an empty corpus")
        vectors = np.asarray(encoder.encode_documents([document_text(json.loads(row["payload"])) for row in rows]),
                             dtype="float32")
        if vectors.ndim != 2 or len(vectors) != len(rows):
            raise RuntimeError("Encoder returned an unexpected matrix shape")
        ids = [row["id"] for row in rows]
        db.executemany("UPDATE records SET dense_pos=? WHERE id=?", enumerate(ids))
    temporary = index_dir / f"{scope}.vectors.tmp.npy"
    np.save(temporary, vectors)
    temporary.replace(index_dir / f"{scope}.vectors.npy")
    (index_dir / f"{scope}.ids.json").write_text(json.dumps(ids, separators=(",", ":")), encoding="utf-8")
    manifest = {**json.loads(meta_row[0]), "model": encoder.name, "dimensions": int(vectors.shape[1]),
                "dense_count": len(ids)}
    temp_manifest = index_dir / f"{scope}.dense.tmp.json"
    temp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temp_manifest.replace(index_dir / f"{scope}.dense.json")
    return manifest


class Retrieval:
    """앱이 사용하는 검색 서비스. 상태 보고와 필터링을 한곳에서 관리한다."""

    def __init__(self, settings, encoder: Any = None, reranker: Any = None):
        """설정 경로의 카탈로그·별칭을 읽고 사용할 수 있는 색인·모델만 연결한다.

        encoder/reranker를 주입하지 않으면 설정의 로컬 모델 폴더로 실제 모델을 만든다.
        모델 준비에 실패해도 서버는 BM25·규칙 재정렬로 동작하고 오류를 진단에 남긴다.
        """
        self.settings = settings
        self.data_dir = Path(settings.data_dir)
        self.index_dir = Path(settings.index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.index_dir / "retrieval.sqlite"
        self.embedding_enabled = bool(getattr(settings, "embedding_enabled", False))
        self.reranker_enabled = bool(getattr(settings, "reranker_enabled", False))
        self.rerank_candidates = int(getattr(settings, "rerank_candidates", 20))
        self.dense_min_similarity = float(getattr(settings, "dense_min_similarity", 0.40))
        # cross-encoder raw logit 하한. None이면 하한 없이 정렬만 한다.
        self.rerank_min_score = getattr(settings, "rerank_min_score", None)
        device = getattr(settings, "device", "auto")
        if self.embedding_enabled and encoder is None:
            from .encoder import SentenceEncoder
            encoder = SentenceEncoder(settings.embedding_model_path, device)
        if self.reranker_enabled and reranker is None:
            from .reranker import CrossEncoderReranker
            reranker = CrossEncoderReranker(settings.reranker_model_path, device)
        self._encoder = encoder if self.embedding_enabled else None
        self._reranker = reranker if self.reranker_enabled else None
        self._encoder_error: str | None = None
        self._reranker_error: str | None = None
        self._index_errors: dict[str, str] = {}
        self._dense: dict[str, tuple[Any, list[str]]] = {}
        initialize_database(self.database)
        catalog = self.data_dir / "activities.jsonl"
        if catalog.is_file():
            with connect(self.database) as db:
                current = db.execute("SELECT value FROM metadata WHERE key='index:activities'").fetchone()
            if current is None or json.loads(current[0]).get("version") != input_version([catalog]):
                build_lexical_index([catalog], self.index_dir)
        from .knowledge import load_knowledge
        self.knowledge = {r["knowledge_id"]: r for r in load_knowledge(self.data_dir)}
        knowledge_path = self.data_dir / "coaching_knowledge.jsonl"
        if knowledge_path.exists():
            with connect(self.database) as db:
                current = db.execute("SELECT value FROM metadata WHERE key='index:knowledge'").fetchone()
            if current is None or json.loads(current[0]).get("version") != input_version([knowledge_path]):
                build_lexical_index([knowledge_path], self.index_dir, "knowledge")
        else:
            with connect(self.database) as db:
                db.execute("DELETE FROM search_fts WHERE rowid IN (SELECT rowid FROM records WHERE scope='knowledge')")
                db.execute("DELETE FROM records WHERE scope='knowledge'")
                db.execute("DELETE FROM metadata WHERE key='index:knowledge'")
        alias_file = self.data_dir / "activity_aliases.json"
        self.aliases = json.loads(alias_file.read_text(encoding="utf-8")).get("aliases", {}) if alias_file.is_file() else {}
        with connect(self.database) as db:
            rows = db.execute("SELECT payload FROM records WHERE scope='activities'").fetchall()
        # 질문 조사 판별용 활동 본문. 162개 규모라 메모리에 둔다. 대규모 레시피 범위는 넣지 않는다.
        self._known_text = re.sub(r"\s+", "", unicodedata.normalize(
            "NFKC", " ".join(document_text(json.loads(row[0])) for row in rows)).casefold())
        self._prepare_models()
        self._load_dense()

    def _manifest_is_current(self, scope: str) -> bool:
        """저장된 벡터가 현재 색인 버전·임베딩 모델과 같은지 확인한다."""
        manifest_path = self.index_dir / f"{scope}.dense.json"
        if not manifest_path.is_file():
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with connect(self.database) as db:
            current = db.execute("SELECT value FROM metadata WHERE key=?", ("index:" + scope,)).fetchone()
        return bool(current) and manifest.get("version") == json.loads(current[0]).get("version") \
            and manifest.get("model") == self._encoder.name

    def _prepare_models(self) -> None:
        """시작할 때 모델을 올리고 활동 벡터를 최신으로 맞춘다. 첫 질문의 지연에 로딩을 섞지 않는다."""
        if self._encoder is not None:
            try:
                if not self._manifest_is_current("activities"):
                    build_dense_index(self.index_dir, self._encoder, "activities")
                if hasattr(self._encoder, "load"):
                    self._encoder.load()
            except Exception:  # 모델 파일·CUDA·형태 오류 모두 BM25 경로로 내려간다.
                self._encoder_error = "embedding_model_unavailable"
                self._encoder = None
        # 지식 벡터는 보조 범위다. 색인된 지식(초안 제외)이 있을 때만 만들고, 실패해도 활동 임베딩은 유지한다.
        if self._encoder is not None and not self._manifest_is_current("knowledge"):
            with connect(self.database) as db:
                searchable = db.execute("SELECT 1 FROM records WHERE scope='knowledge' LIMIT 1").fetchone()
            try:
                if searchable:
                    build_dense_index(self.index_dir, self._encoder, "knowledge")
            except Exception:
                self._index_errors["knowledge"] = "dense_index_unavailable"
        if self._reranker is not None and hasattr(self._reranker, "load"):
            try:
                self._reranker.load()
            except Exception:
                self._reranker_error = "reranker_model_unavailable"
                self._reranker = None

    def _load_dense(self) -> None:
        """자료 지문·모델명이 일치하는 벡터만 연결한다. 오래된 파일은 검색에 쓰지 않는다."""
        if self._encoder is None:
            return
        import numpy as np
        for scope in ("activities", "recipes", "knowledge"):
            try:
                if not self._manifest_is_current(scope):
                    continue
                manifest = json.loads((self.index_dir / f"{scope}.dense.json").read_text(encoding="utf-8"))
                ids = json.loads((self.index_dir / f"{scope}.ids.json").read_text(encoding="utf-8"))
                vectors = np.load(self.index_dir / f"{scope}.vectors.npy", mmap_mode="r")
                if len(ids) != len(vectors) or len(ids) != manifest.get("dense_count"):
                    continue
                self._dense[scope] = (vectors, ids)
            except (OSError, ValueError, KeyError):
                # 한 보조 색인이 손상돼도 정상 카탈로그 벡터까지 사용 중지하지 않는다.
                self._index_errors[scope] = "dense_index_unavailable"

    def knowledge_for_activity(self, activity_id: str) -> list[dict]:
        from .answer_policy import knowledge_evidence
        order = {'steps':0, 'checks':1, 'safety_notes':2}
        rows = [r for r in self.knowledge.values() if activity_id in r['activity_ids'] and r['review_status'] != 'draft']
        rows.sort(key=lambda r: (order.get(r['kind'], 3), r['knowledge_id']))
        return [knowledge_evidence(r, activity_id) for r in rows[:10]]

    def get(self, activity_id: str) -> dict[str, Any] | None:
        """공통 ID 또는 홈 별칭으로 활동을 조회한다. 모르는 ID를 다른 활동으로 대체하지 않는다."""
        canonical = self.aliases.get(activity_id, activity_id)
        with connect(self.database) as db:
            row = db.execute("SELECT payload FROM records WHERE id=? AND scope!='knowledge'", (canonical,)).fetchone()
        return json.loads(row[0]) if row else None

    def _where(self, hobby_key: str, scope: str, constraints: dict[str, Any]) -> tuple[str, list[Any]]:
        """BM25·벡터 양쪽에 같은 필터를 적용한다. 미상 값은 숫자 조건을 충족하지 않는다."""
        clauses = ["r.hobby=?", "r.scope=?", "r.quality!='quarantined'"]
        params: list[Any] = [hobby_key, scope]
        if scope == "knowledge" and constraints.get("activity_id"):
            clauses.append("EXISTS (SELECT 1 FROM json_each(r.payload,'$.activity_ids') WHERE value=?)")
            params.append(constraints["activity_id"])
        for field, column in (("max_minutes", "total_minutes"), ("max_difficulty", "difficulty"), ("budget_max", "budget")):
            if constraints.get(field) is not None:
                clauses += [f"r.{column} IS NOT NULL", f"r.{column}<=?"]
                params.append(constraints[field])
        if constraints.get("available_tools") is not None:
            clauses += ["r.tools_known=1", "tools_match(r.tools_json,?)=1"]
            params.append(json.dumps(constraints["available_tools"], ensure_ascii=False))
        if constraints.get("excluded_tools"):
            clauses += ["r.tools_known=1", "excluded_tools_match(r.tools_json,?)=1"]
            params.append(json.dumps(constraints["excluded_tools"], ensure_ascii=False))
        return " AND ".join(clauses), params

    def list_activities(self, query: str = "", hobby_key: str = "cooking", limit: int = 20) -> list[dict[str, Any]]:
        """활동 목록 또는 주제 검색을 반환한다. 보조 레시피를 앱 활동 목록에 자동 섞지 않는다."""
        if query.strip():
            return self.search(query, hobby_key=hobby_key, limit=limit)["items"]
        with connect(self.database) as db:
            rows = db.execute("SELECT payload FROM records WHERE scope='activities' AND hobby=? ORDER BY rowid LIMIT ?", (hobby_key, min(max(limit, 1), 100))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _embed_query(self, query: str):
        """질문 벡터를 계산한다. 추론 실패는 BM25 경로로 내려가도록 None을 반환한다."""
        import numpy as np
        try:
            vector = np.asarray(self._encoder.encode_query(query), dtype="float32")
        except Exception:  # CUDA·토크나이저 오류도 검색 전체를 실패시키지 않는다.
            self._encoder_error = "embedding_inference_failed"
            return None
        return vector / max(float(np.linalg.norm(vector)), 1e-12)

    def _rerank(self, query: str, candidates: list[str],
                docs: dict[str, dict[str, Any]]) -> tuple[dict[str, float] | None, str | None]:
        """cross-encoder 점수와 이번 호출의 오류를 반환한다. 점수가 없으면 규칙 순서를 유지한다."""
        if self._reranker is None or not candidates:
            return None, self._reranker_error
        try:
            scores = self._reranker.score(query, [document_text(docs[aid]) for aid in candidates])
        except Exception:  # GPU 메모리 부족 등 추론 오류는 진단에 남기고 규칙 순서로 응답한다.
            return None, "reranker_inference_failed"
        return {aid: float(score) for aid, score in zip(candidates, scores)}, None

    def search(self, query: str, hobby_key: str = "cooking", constraints: dict[str, Any] | None = None, limit: int = 3, scope: str = "activities") -> dict[str, Any]:
        """조건 필터 → BM25/벡터 → RRF → cross-encoder(없으면 규칙) 재정렬 후 근거와 진단을 반환한다."""
        started = time.perf_counter()
        constraints = constraints or {}
        if hasattr(constraints, "model_dump"):
            constraints = constraints.model_dump()
        limit = min(max(int(limit), 1), 100)
        if scope not in {"activities", "recipes", "knowledge"}:
            raise ValueError("Unknown search scope")
        where, params = self._where(hobby_key, scope, constraints)
        # 도구 제약은 메타데이터로 이미 처리한다. '오븐 없이'의 오븐을 긍정 검색어로
        # 다시 쓰면 오히려 오븐 관련 본문만 찾게 되므로 해당 제약 표현을 제거한다.
        topic_query = query
        for excluded in constraints.get("excluded_tools") or []:
            topic_query = topic_query.replace(excluded, " ")
        subject, terms = query_terms(topic_query, scope, self._known_text if scope == "activities" else None)
        # 레시피는 영어 원문이라 한국어 질문만으로는 cross-encoder 점수가 하한 아래로 떨어진다.
        # 재정렬 질문에만 사전의 영어 표현을 덧붙인다. 활동·지식 범위의 재정렬 질문은 그대로다.
        aliases = matched_aliases(subject, scope) if scope == "recipes" else []
        diagnostics: dict[str, Any] = {"search_mode": "bm25", "scope": scope, "query_aliases": aliases, "reranker": "rule_based",
            "reranker_model": None, "reranker_error": self._reranker_error, "embedding_error": self._encoder_error,
            "rerank_min_score": self.rerank_min_score,
            "rrf_k": 60, "bm25_candidates": 0, "dense_candidates": 0, "candidate_ids": [], "ranking": [],
            "missing_filter_fields": [], "dense_available": scope in self._dense}
        diagnostics["minimum_dense_similarity"] = self.dense_min_similarity
        with connect(self.database) as db:
            meta_row = db.execute("SELECT value FROM metadata WHERE key=?", ("index:" + scope,)).fetchone()
            diagnostics["index_version"] = json.loads(meta_row[0])["version"] if meta_row else None
            diagnostics["total_documents"] = db.execute("SELECT count(*) FROM records r WHERE r.hobby=? AND r.scope=?", (hobby_key, scope)).fetchone()[0]
            diagnostics["eligible_documents"] = db.execute(f"SELECT count(*) FROM records r WHERE {where}", params).fetchone()[0]
            for field, column in (("max_minutes", "total_minutes"), ("max_difficulty", "difficulty"), ("budget_max", "budget")):
                if constraints.get(field) is not None:
                    known = db.execute(f"SELECT count(*) FROM records WHERE hobby=? AND scope=? AND {column} IS NOT NULL", (hobby_key, scope)).fetchone()[0]
                    if not known:
                        diagnostics["missing_filter_fields"].append(field)
            if not diagnostics["eligible_documents"] or (hobby_key == "cooking" and OUTSIDE_COOKING.search(query)):
                diagnostics["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                return {"items": [], "diagnostics": diagnostics}
            if not terms:
                # '설명해주세요'처럼 주제가 없는 문장은 임의의 활동 카드로 채우지 않는다.
                # 명시적인 추천 요청이나 시간만 지정한 요리 탐색은 기존 카탈로그 탐색을 유지한다.
                browse_requested = not query.strip() or bool(re.search(r"추천|recommend|뭐\s*하지|뭘\s*할", query, re.I))
                browse_requested = browse_requested or bool(re.search(r"요리|활동|cooking", query, re.I) and not re.search(r"설명|원리|이유|방법|개념", query))
                if not browse_requested:
                    diagnostics.update(empty_reason="missing_search_topic", latency_ms=round((time.perf_counter() - started) * 1000, 2))
                    return {"items": [], "diagnostics": diagnostics}
                rows = db.execute(f"SELECT r.payload FROM records r WHERE {where} ORDER BY r.total_minutes IS NULL,r.total_minutes,r.rowid LIMIT ?", [*params, limit]).fetchall()
                items = [json.loads(row[0]) for row in rows]
                diagnostics.update(search_mode="catalog_browse", candidate_ids=[row["activity_id"] for row in items], latency_ms=round((time.perf_counter() - started) * 1000, 2))
                return {"items": items, "diagnostics": diagnostics}
            expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in terms)
            # FTS5의 rank 정렬은 상위 결과부터 읽는다. bm25 함수 값을 별도 정렬하면
            # 공통 재료가 들어간 수십만 문서를 전부 정렬하므로 대규모 레시피에서 느리다.
            # WHERE 조건을 LIMIT보다 먼저 적용하므로 두 검색 가지의 제약은 동일하다.
            lexical = db.execute(f"""SELECT r.id,r.payload,search_fts.rank AS score
                FROM search_fts JOIN records r ON r.rowid=search_fts.rowid
                WHERE search_fts MATCH ? AND search_fts.rank MATCH 'bm25(5.0,1.0)'
                AND {where} ORDER BY search_fts.rank LIMIT 40""", [expression, *params]).fetchall()
            anchors = query_anchors(subject, scope)
            docs = {}
            for row in lexical:
                document = json.loads(row["payload"])
                if has_topic_anchor(document_text(document), anchors):
                    docs[row["id"]] = document
            bm25_ids = [row["id"] for row in lexical if row["id"] in docs]
            diagnostics["lexical_rejected_without_topic"] = len(lexical) - len(bm25_ids)
            diagnostics["bm25_candidates"] = len(bm25_ids)
            dense_scores: dict[str, float] = {}
            if scope in self._dense:
                vector = self._embed_query(subject)
                if vector is not None:
                    import numpy as np
                    matrix, ids = self._dense[scope]
                    # SQL에서 먼저 조건을 통과한 위치만 읽는다. 결과 수가 많아도 배치로 점수를 계산한다.
                    cursor = db.execute(f"SELECT r.id,r.dense_pos FROM records r WHERE {where} AND r.dense_pos IS NOT NULL", params)
                    candidates: list[tuple[float, str]] = []
                    while batch := cursor.fetchmany(2048):
                        positions = [row["dense_pos"] for row in batch]
                        scores = matrix[positions] @ vector
                        candidates.extend((float(score), row["id"]) for score, row in zip(scores, batch) if float(score) >= diagnostics["minimum_dense_similarity"])
                        candidates = sorted(candidates, key=lambda pair: (-pair[0], pair[1]))[:40]
                    dense_scores = {aid: score for score, aid in candidates}
                    diagnostics["search_mode"] = "hybrid_bm25_dense_rrf"
                    diagnostics["dense_candidates"] = len(dense_scores)
                    for aid in dense_scores:
                        if aid not in docs:
                            item = db.execute("SELECT payload FROM records WHERE id=?", (aid,)).fetchone()
                            if item:
                                docs[aid] = json.loads(item[0])
        # 순위 결합은 검색 점수 단위 차이를 피한다. 규칙 점수로 후보를 고른 뒤 cross-encoder가 최종 순서를 정한다.
        fusion: dict[str, float] = defaultdict(float)
        for rank, aid in enumerate(bm25_ids, 1):
            fusion[aid] += 1 / (60 + rank)
        for rank, aid in enumerate(dense_scores, 1):
            fusion[aid] += 1 / (60 + rank)
        subject_terms = set(subject.split())
        ranked: list[tuple[float, str]] = []
        for aid, score in fusion.items():
            row = docs[aid]
            # '밥'+'냄비'가 '냄비밥' 제목에 모두 포함되는 한국어 합성명사도 반영한다.
            # 문서 n-gram을 늘리지 않으므로 전체 코퍼스를 다시 적재할 필요가 없다.
            title_overlap = sum(has_topic_anchor(row["title"], [term]) for term in subject_terms) / max(len(subject_terms), 1)
            # 동일 주제의 제목과 짧은 첫 활동을 약하게 선호한다. 제한된 원문 사실을 생성하지 않는다.
            exact = bool(subject and subject in row["title"].casefold())
            quality_bonus = 0.002 if row.get("quality", {}).get("status") == "structural_pass" else 0
            reranked = score + 0.03 * title_overlap + 0.02 * exact + quality_bonus
            ranked.append((reranked, aid))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]))
        candidates = [aid for _, aid in ranked[:max(limit, self.rerank_candidates)]]
        rerank_scores, reranker_error = self._rerank(" ".join([query, *aliases]), candidates, docs)
        passed = set(candidates)
        if rerank_scores is not None:
            # 안정 정렬이라 같은 점수는 규칙 순서를 유지한다.
            candidates.sort(key=lambda aid: -rerank_scores[aid])
            diagnostics.update(reranker="cross_encoder", reranker_model=self._reranker.name)
            if self.rerank_min_score is not None:
                # 관련도 하한 미만은 순위가 높아도 근거로 쓰지 않는다. 진단에는 남겨 무엇을 뺐는지 보인다.
                passed = {aid for aid in candidates if rerank_scores[aid] >= self.rerank_min_score}
        bm25_rank = {aid: rank for rank, aid in enumerate(bm25_ids, 1)}
        diagnostics["ranking"] = [
            {"final_rank": rank, "activity_id": aid, "title": docs[aid]["title"],
             "bm25_rank": bm25_rank.get(aid),
             "dense_score": round(dense_scores[aid], 4) if aid in dense_scores else None,
             "rrf_score": round(fusion[aid], 5),
             "rerank_score": round(rerank_scores[aid], 4) if rerank_scores is not None else None,
             "passed": aid in passed}
            for rank, aid in enumerate(candidates, 1)
        ]
        chosen = [aid for aid in candidates if aid in passed][:limit]
        if candidates and not chosen:
            diagnostics["empty_reason"] = "below_rerank_threshold"
        diagnostics.update(candidate_ids=chosen, embedding_error=self._encoder_error, reranker_error=reranker_error,
                           latency_ms=round((time.perf_counter() - started) * 1000, 2))
        return {"items": [docs[aid] for aid in chosen], "diagnostics": diagnostics}

    def status(self) -> dict[str, Any]:
        """구성·실제 색인 상태를 알려 준다. 설정 켜짐만으로 하이브리드가 준비됐다고 표시하지 않는다."""
        with connect(self.database) as db:
            counts = {row["scope"]: row["n"] for row in db.execute("SELECT scope,count(*) AS n FROM records GROUP BY scope")}
        return {"documents": counts, "embedding_enabled": self.embedding_enabled,
                "embedding_model": getattr(self._encoder, "name", None), "dense_index_scopes": sorted(self._dense),
                "embedding_error": self._encoder_error, "index_errors": self._index_errors,
                "reranker_enabled": self.reranker_enabled, "reranker_model": getattr(self._reranker, "name", None),
                "reranker_error": self._reranker_error, "rerank_candidates": self.rerank_candidates,
                "index_version": INDEX_VERSION}
