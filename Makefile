.PHONY: install run test clean lint format check help

help:
	@echo "Available commands:"
	@echo "  make install  - Install dependencies"
	@echo "  make run      - Run the application"
	@echo "  make test     - Run tests"
	@echo "  make lint     - Run linter"
	@echo "  make format   - Format code"
	@echo "  make check    - Run type checker, linter and formatter"
	@echo "  make clean    - Remove build artifacts"

check: install
	uv run ty check .
	uv run ruff check .
	uv run ruff format .

install:
	uv sync --group dev

run: install
	uv run uvicorn main:app --reload

test:
	uv run pytest


clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache
	rm -rf dist build
