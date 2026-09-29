# Imagen del bot (FastAPI). R28: un solo worker.
FROM python:3.14-slim

WORKDIR /app

RUN groupadd --system bot && useradd --system --gid bot --no-create-home bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

USER bot

EXPOSE 8020

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020", "--workers", "1"]
