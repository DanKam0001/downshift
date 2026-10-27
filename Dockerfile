FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY downshift ./downshift
COPY presets ./presets
COPY static ./static
COPY demo_runs ./demo_runs
COPY tests/fixtures ./tests/fixtures
COPY app.py PLAN.md ./

RUN useradd -m app && mkdir -p /app/runs && chown -R app /app/runs
USER app

EXPOSE 8000
# NEBIUS_API_KEY and NEBIUS_PROJECT_ID come from the host's secret settings, never from the image.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
