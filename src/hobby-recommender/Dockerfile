# Python 실행 환경과 컨테이너 내부 작업 디렉터리를 지정한다.
FROM python:3.11-slim
WORKDIR /service
# 바이트코드 파일 생성을 막고 서버 로그를 즉시 표준 출력으로 전달한다.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# 의존성 레이어를 먼저 캐시하고 일반 사용자로 실행할 계정을 만든다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home appuser
# 엔진 코드와 JSON 데이터만 포함하며 호스트의 테스트·가상 환경은 복사하지 않는다.
COPY app ./app
USER appuser
# 앱은 8000 포트에서 제공하며 Spring 백엔드와는 별도 프로세스다.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
