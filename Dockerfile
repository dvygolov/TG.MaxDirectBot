FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install .

RUN useradd --create-home --uid 10001 bridge \
    && mkdir -p /app/data /app/certs \
    && chown -R bridge:bridge /app

USER bridge

EXPOSE 8000

CMD ["uvicorn", "tg_max_direct_bot.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
