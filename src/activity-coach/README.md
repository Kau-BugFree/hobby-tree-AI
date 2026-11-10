# HobbyTree 답변 챗봇 V3 — 2026-11-09

> **저장소 사본 안내 (hobby-tree-AI)**
>
> 이 폴더는 전달 패키지에서 실행 코드(`backend/app`), 테스트(`backend/tests`), 도구(`backend/scripts`), 활동·코칭·평가 자료(`backend/data`의 작은 파일), API 명세(`docs/`)만 옮긴 것입니다.
>
> 동봉 Python(`runtime/`), 검색 모델(`models/`), 레시피 원자료와 색인(`backend/data/recipes`, `backend/var`), 구조·검증 문서(`verification/` 등), `package_manifest.json`은 들어 있지 않습니다. 그래서 아래 본문의 배치 파일과 일부 문서 링크는 전달 패키지에서만 동작합니다.
>
> - 설치: `backend/requirements.txt` 첫머리의 안내를 따릅니다(Python 3.12).
> - 테스트: `backend`에서 `python -m pytest tests -q`. 모델과 색인 없이 실행되며, 레시피 색인이 필요한 대조 1건은 건너뜁니다.
> - 서버: `backend`에서 `python -m uvicorn app.main:create_app --factory --port 8003`. 모델이 없으면 `backend/.env`에 `COACH_EMBEDDING_ENABLED=false`, `COACH_RERANKER_ENABLED=false`를 넣습니다.
> - Spring 백엔드 연동: [API 명세](docs/HobbyTree_Coach_V3_API_Spec_ko.md) 11장의 `POST /backend/coach/chat`.

이 **2026-11-09 폴더 전체**가 전달 단위입니다. Windows x64 Python, 라이브러리, 검색 모델, 데이터, 색인을 실제 파일로 포함하므로 상위 폴더나 V1/V2가 필요하지 않습니다. 사용자 요청에 따라 프론트엔드와 팀 서비스 연동 구현은 포함하지 않았습니다.

## 바로 실행

1. 폴더 전체를 쓰기 가능한 로컬 디스크에 복사합니다. 긴 경로 문제가 생기면 `C:\HobbyTree\2026-11-09` 같은 짧은 경로를 사용합니다.
2. 처음 실행하기 전에 `verify_package.bat`으로 전달 파일 SHA-256을 확인합니다. 정상 결과는 `problems: []`입니다. `unlisted`에는 매니페스트에 없는 파일이 나옵니다. 받은 직후인데 `backend/.env`나 `backend/var/coach.sqlite`가 보이면 보낸 쪽의 키·대화 기록이 섞여 온 것이니 지우고 보낸 쪽에 알리세요.
3. `start_backend.bat`을 실행합니다. 기본 주소는 `http://127.0.0.1:8003`이며 `/health`에서 상태를 확인합니다. `/docs`의 Swagger UI는 브라우저가 외부 CDN에 접근할 수 있어야 표시될 수 있습니다.
4. Claude 생성이 필요하면 `backend/.env.example`을 `backend/.env`로 복사하고 **본인의 `ANTHROPIC_API_KEY`**를 넣은 뒤 서버를 재시작합니다. 모델 접근 권한에 맞게 `CLAUDE_MODEL`도 설정합니다. 실제 키는 이 패키지에 없습니다.

키 없이도 모델 검색, 활동 선택, 준비물·단계 조회, 근거 원문 응답, 기록 저장이 동작합니다. Claude 생성·의미 검증은 외부 네트워크·유효한 API 키가 필요하며 비용이 발생합니다. Claude 자체의 모델 가중치는 포함할 수 없습니다.

Windows x64용 실행 환경입니다. GPU 드라이버는 포함하지 않습니다. CUDA를 사용할 수 있으면 GPU, 아니면 CPU로 실행하며 CPU에서는 최초 로딩·검색이 오래 걸릴 수 있습니다. 첫 실행 시 모델을 인터넷에서 내려받지 않습니다. 현재 서버는 로컬 단일 사용자 시연용이며 기본 바인딩은 `127.0.0.1`입니다.

## 포함한 내용

| 위치 | 내용 |
|---|---|
| `backend/app` | 대화·RAG API |
| `backend/scripts` | 평가·색인 도구 |
| `backend/tests` | 회귀 테스트 및 패키지 테스트 |
| `backend/data` | 활동 162개, 코칭 지식 486개, 평가 자료, 전처리 레시피·리뷰 |
| `backend/var/index` | 레시피 544,888개를 포함한 SQLite 검색 색인, 활동·지식·레시피 임베딩 색인 |
| `models` | BGE-m3-ko 임베딩, KURE-Reranker-nano 리랭커의 가중치·설정·토크나이저 |
| `runtime/python` | Python 3.12.14 및 실행·테스트 의존성 |
| `source_data/recipes` | 보관된 원본 레시피 CSV 2개 |
| `docs` | 구조 설명, 패키지 범위와 검증 기록 |
| `verification` | 실행 환경 목록, 실제 검색 검증 결과, OpenAPI 명세, 테스트 결과 |
| `package_manifest.json` | 전달 파일 목록·크기·SHA-256 |

모든 실행 배치 파일은 자신의 위치를 기준으로 동작합니다. 개별 Python 명령은 `backend`를 작업 폴더로 하고 `../runtime/python/python.exe`를 사용합니다. 새 대화 데이터는 `backend/var/coach.sqlite`에 생성됩니다. **재전달하기 전에 `verify_package.bat --build`로 매니페스트를 다시 만들고, 출력의 `remove_before_delivery`에 나온 파일(`backend/.env`, `backend/var/coach.sqlite`, 테스트 임시 폴더 등)을 지운 뒤 보내세요.** 이 파일들은 매니페스트에는 없지만 폴더를 통째로 복사하면 함께 나갑니다. 최초 전달 패키지에는 개인 API 키와 기존 운영 대화를 넣지 않았습니다.

## 확인 및 상세 문서

- `run_tests.bat`: 모델/Claude를 대역으로 바꾼 자동 회귀 테스트.
- `smoke_test.bat`: 실제 포함 모델로 검색하고 키 없는 멀티턴 API 흐름을 확인. 유료 호출 0회. 결과는 매니페스트 밖인 `backend/var/smoke-report/`에 씁니다.
- `verify_package.bat`: 전달 당시 파일의 전체 내용 해시 확인. `--quick`은 크기만 검사.
- [현재 구조·이전 대비 변화·남은 한계](docs/ARCHITECTURE.md)
- [멀티턴 결함 수정](docs/MULTITURN_FIXES.md)과 [동일 질문 재평가 보고서](verification/comparison/REPORT_FIXED.md)
- [데이터·모델·실행 환경 범위](docs/PACKAGING.md)
- [검증 결과와 확인하지 않은 범위](docs/VALIDATION.md)

서버 사용·평가·파일 수정 후에는 원래 전달 해시와 일부 파일이 달라질 수 있습니다. 원본 전달본은 따로 보관하고, 수신 시 검증을 먼저 실행하세요.

## 2026-11-10 레시피 하이브리드 검색

레시피 544,888건의 배치 임베딩을 추가했습니다. BM25와 벡터 후보를 RRF로 결합하고 기존 KURE 리랭커를 적용합니다. 재생성은 `build_recipe_embeddings.bat`을 사용하며, 중단 후 같은 자료·모델 설정에서 재개할 수 있습니다. 생성·활성화 시 서버와 적재 작업을 중지하세요.

- [생성·재개·복구와 CPU 제한](docs/RECIPE_HYBRID.md)
- [기존 BM25 대비 실측 결과 및 테스트 한계](verification/recipe-hybrid-2026-11-10/REPORT.md)

기존 검증 기록은 해당 날짜의 결과이며, 이번 전체 회귀 결과에는 기존 라우터 테스트 실패 6건이 포함되어 있습니다. 새 결과는 위 보고서를 기준으로 확인하세요.

## 2026-11-10 코드 리뷰 반영

정밀 코드 리뷰에서 나온 결함을 고쳤습니다. `docs/`와 `verification/`의 통과 수·줄 번호는 이 수정 이전 기록입니다.

- 대화: 질문이나 문제 보고가 후보 선택·활동 시작·완료 기록으로 처리되지 않습니다. 실패 보고가 추천 검색으로 빠지지 않고, 요리 질문에 섞인 '날씨'·'주식' 같은 말로 차단되지 않습니다.
- 실패 처리: Claude 호출이 공급자 오류로 실패하면 키 없는 실행과 같은 원문 안내를 유지합니다. 검증에서 떨어진 답은 보류하며 상태가 `withheld`로 구분됩니다. 의도 해석이 실패한 턴은 대화 상태를 바꾸지 않습니다.
- 시작: 서버는 `app.main:create_app` factory로 시작합니다(`start_backend.bat`에 반영). 잘못된 숫자 설정은 시작할 때 변수 이름과 함께 거부합니다. 깨진 벡터 메타데이터는 서버 시작을 막지 않고 원인을 로그와 `/health`에 남깁니다.
- 전달: 검증 도구가 목록 밖 파일을 알려 주고, smoke 검사가 현재 색인 구성(레시피 벡터 포함)에 맞습니다.

자동 회귀 테스트는 이 수정 뒤 `run_tests.bat` 기준 전부 통과합니다.

### 여러 사용자가 한 서버를 쓸 때

기본 실행은 여전히 이 PC에서만 접속하는 단일 사용자 시연입니다. 여러 사용자가 쓰는 서버로 옮길 때를 대비해 다음을 바꿨습니다.

- **세션 ID가 열쇠입니다.** `/records`와 `/diagnostics/traces`는 `session_id`가 필수입니다. 범위 없는 전체 조회는 다른 세션의 ID를 드러내므로 없앴습니다. 없는 세션은 두 경로 모두 404입니다.
- **세션 없이 보내는 첫 요청**(`session_id` 생략)의 재전송 보호는 `request_id`가 UUID 형식일 때만 적용됩니다. `demo-1`처럼 여러 사용자가 겹쳐 쓸 수 있는 값은 매번 새 대화가 됩니다.
- **허용한 주소로 온 요청만 받습니다.** 기본값은 `127.0.0.1`과 `localhost`입니다. 다른 주소로 서비스하려면 `backend/.env`의 `COACH_ALLOWED_HOSTS`에 그 이름을 적고 `start_backend.bat --host 0.0.0.0`처럼 실행합니다.
- **worker를 둘 이상 띄워도 대화 상태가 조용히 덮어써지지 않습니다.** 두 요청이 같은 대화를 동시에 고치면 나중 것이 409를 받으며, 같은 요청을 다시 보내면 됩니다. 다만 worker마다 검색 모델을 따로 올리므로 단일 worker를 권장합니다.
- **로그인과 요청 수 제한은 없습니다.** 서버에 접속할 수 있는 사람은 누구나 대화를 만들 수 있고, Claude 키가 설정돼 있으면 유료 호출을 일으킬 수 있습니다. 외부에 공개하려면 앞단에서 인증과 요청 제한을 걸어야 합니다.
