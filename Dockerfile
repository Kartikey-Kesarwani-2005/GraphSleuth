# GraphSleuth app image — the agent + web console.
#
# Optional: the quickstart runs the app from the host (`python -m graphsleuth ...`)
# against the FalkorDB container. This image lets the whole thing run in Docker:
#
#     docker compose --profile app up -d      # FalkorDB + app
#     # then open http://127.0.0.1:8765
#
# It is behind a compose profile so a plain `docker compose up -d` still starts
# only FalkorDB (the documented, fastest path).

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
# --retries/--timeout: PyPI is occasionally slow; without these a transient read
# timeout fails the whole image build.
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r requirements.txt

COPY graphsleuth ./graphsleuth

EXPOSE 8765

# Default: the live web console. Override the command to run anything else, e.g.
#   docker compose --profile app run --rm app python -m graphsleuth eval
CMD ["python", "-m", "graphsleuth", "serve", "--host", "0.0.0.0", "--port", "8765"]
