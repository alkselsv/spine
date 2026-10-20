FROM python:3.12-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir uv==0.8.14

COPY pyproject.toml uv.lock README.md ./
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src

RUN uv sync --frozen --no-dev

CMD ["uv", "run", "uvicorn", "spine.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
