set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

default: lint typecheck test test-web

setup:
    uv sync
    uv run pre-commit install
    uv run pre-commit install --hook-type commit-msg
    uv run playwright install chromium

lint:
    uv run ruff format --check .
    uv run ruff check .

fmt:
    uv run ruff format .
    uv run ruff check --fix .

typecheck:
    uv run mypy

test:
    uv run pytest -m "not live" -q

test-live:
    uv run pytest -m live -q

test-web:
    cd frontend; npx tsc -b --noEmit; npm run lint; npm test

probe:
    uv run crossfoot probe

# Regenerate the corpus from the published seed and check the deterministic tiers
# against the committed scorecard. Writes to its own directory so the corpus the
# published extractions were read from is never overwritten.
repro dataset="data/repro":
    uv run crossfoot gen --seed 42 --out {{dataset}}
    uv run python scripts/repro_check.py {{dataset}}

web:
    cd frontend; npm run dev
