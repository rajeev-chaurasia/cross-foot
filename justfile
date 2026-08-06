set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

default: lint typecheck test

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

probe:
    uv run crossfoot probe

web:
    cd frontend; npm run dev
