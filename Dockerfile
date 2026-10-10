FROM python:3.14-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY static ./static
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN groupadd --gid 10001 uglytags && useradd --uid 10001 --gid 10001 --no-create-home uglytags
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
COPY LICENSE ./LICENSE
USER 10001:10001
CMD ["uglytags"]
