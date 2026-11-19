FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    ADPICK_FILTERING_APPROVED=false \
    ADPICK_STORAGE_APPROVED=false \
    CATALOG_DB_PATH=/var/lib/item-recommender/catalog.sqlite3 \
    STATE_DB_PATH=/var/lib/item-recommender/provider-state.sqlite3

WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY item_recommender ./item_recommender
COPY sql ./sql
RUN python -m pip install --no-cache-dir ".[postgres]" \
    && mkdir -p /var/lib/item-recommender \
    && chown 10001:10001 /var/lib/item-recommender

USER 10001:10001
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "item_recommender.main:app", "--host", "0.0.0.0", "--port", "8001"]
