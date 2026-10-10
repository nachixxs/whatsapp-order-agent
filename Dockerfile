# Imagen del bot (FastAPI). R28: un solo worker.
# python:3.14-slim y uv 0.12.17 por digest: la misma imagen en cada build.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

COPY --from=ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc /uv /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never

WORKDIR /app

RUN groupadd --system bot && useradd --system --gid bot --no-create-home bot
# /datos con dueño bot: si no existe en la imagen, el volumen del servidor nace con dueño root y la memoria SQLite no se puede escribir.
RUN mkdir /datos && chown bot:bot /datos

# Dependencias del lock, con hashes verificados; sin pytest ni pre-commit (UV_NO_DEV).
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-cache

ENV PATH="/app/.venv/bin:$PATH"

COPY app/ ./app/

USER bot

EXPOSE 8020

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020", "--workers", "1"]
