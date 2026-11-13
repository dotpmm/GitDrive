FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 10000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "10000", "--proxy-headers", "--forwarded-allow-ips=*"]