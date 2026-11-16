FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 carevoice

COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY carevoice ./carevoice

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e ".[audio]"

USER carevoice

CMD ["carevoice-worker"]
