# app service — lightweight FastAPI UI, NO GPU (AGENTS.md §2, §8).
FROM python:3.12-slim

WORKDIR /srv

COPY requirements-app.txt .
RUN pip install --no-cache-dir -r requirements-app.txt

# Only the code the app needs (app + pipeline; no OCR/PDF deps).
COPY pipeline/ ./pipeline/
COPY app/ ./app/

# State lives in the mounted volume, never in the image.
ENV DATA_DIR=/data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
