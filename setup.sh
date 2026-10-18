#!/usr/bin/env bash
# GraphSleuth — one-shot setup + demo run for macOS and Linux
# Usage:  bash setup.sh
#
# Does everything end to end:
#   1. creates .venv if missing and installs dependencies
#   2. creates .env from .env.example if missing
#   3. starts Docker Desktop on macOS if the engine is not running, and waits
#   4. brings FalkorDB up via docker compose and waits until it answers
#   5. seeds the fraud graph
#   6. runs the investigation demo and saves the report
#
# Windows users: setup.ps1 does the same in PowerShell.

set -euo pipefail
cd "$(dirname "$0")"

echo "== GraphSleuth setup =="

# --- 1. virtual environment + dependencies ---------------------------------
if [ ! -d .venv ]; then
    echo "Creating virtual environment ..."
    python3 -m venv .venv
fi

PY=".venv/bin/python"

echo "Installing dependencies ..."
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt

# --- 2. config file ---------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env (add GROQ_API_KEY — free at console.groq.com — for the real model)."
fi

# --- 3. Docker engine -------------------------------------------------------
if ! docker info >/dev/null 2>&1; then
    if [ "$(uname)" = "Darwin" ] && [ -d "/Applications/Docker.app" ]; then
        echo "Starting Docker Desktop ..."
        open -a Docker
    fi
    echo "Waiting for the Docker engine ..."
    ready=0
    for _ in $(seq 1 60); do
        if docker info >/dev/null 2>&1; then ready=1; break; fi
        sleep 5
    done
    if [ "$ready" -ne 1 ]; then
        echo "Docker engine is not up." >&2
        echo "  * macOS: launch Docker Desktop (accept any first-run prompts)." >&2
        echo "  * Linux:  sudo systemctl start docker" >&2
        echo "  * Docker not installed? https://docs.docker.com/engine/install/" >&2
        exit 1
    fi
fi
echo "Docker engine is up."

# --- 4. FalkorDB ------------------------------------------------------------
echo "Starting FalkorDB ..."
docker compose up -d

echo "Waiting for FalkorDB to accept connections ..."
ok=0
for _ in $(seq 1 30); do
    if "$PY" -m graphsleuth status >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
done
if [ "$ok" -ne 1 ]; then
    echo "FalkorDB did not come up. Container logs:" >&2
    docker compose logs --tail 40
    exit 1
fi

# --- 5. seed ----------------------------------------------------------------
echo "Seeding the fraud graph ..."
"$PY" -m graphsleuth seed

# --- 6. demo ----------------------------------------------------------------
echo
echo "Running the investigation demo ..."
mkdir -p demos/out
"$PY" -m graphsleuth demo --target ACC-9003 --save demos/out/report.md

echo
echo "Done. Report: demos/out/report.md"
echo "Multi-agent pipeline (3 agents):  $PY -m graphsleuth pipeline --target ACC-9003 --verify"
echo "Reproducible scorecard:           $PY -m graphsleuth eval"
echo "Learned playbooks + memory:       $PY -m graphsleuth memory"
echo "Live web console:                 $PY -m graphsleuth serve"
echo "Concurrency bench:                $PY -m graphsleuth bench"
echo "Interactive mode:                 $PY -m graphsleuth chat"
