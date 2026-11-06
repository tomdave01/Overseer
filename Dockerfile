FROM python:3.12-slim AS replay-data

WORKDIR /data
COPY outputs/synthetic_events.json outputs/synthetic_frames.json ./
RUN python -c "import gzip,pathlib; [pathlib.Path(str(path) + '.gz').write_bytes(gzip.compress(path.read_bytes(), compresslevel=9, mtime=0)) for path in pathlib.Path('/data').glob('*.json')]" \
    && rm /data/*.json

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --disable-pip-version-check --no-compile -r requirements.txt \
    && groupadd --system overseer \
    && useradd --system --gid overseer --home-dir /app overseer
COPY agents ./agents
COPY api ./api
COPY engine ./engine
COPY feed ./feed
COPY generator ./generator
COPY llm ./llm
COPY --from=replay-data /data ./outputs
COPY web ./web
COPY telemetry.py .

EXPOSE 8000
USER overseer
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
