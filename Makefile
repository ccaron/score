.PHONY: install run test clean lint format check help build

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
	uv run ruff check --fix .
	uv run ruff format .

install:
	uv sync --group dev

run: install
	uv run python main.py

build:
	uv run pyinstaller --windowed --noconsole --onedir -y --name game_clock --add-data "static:static" main.py --debug=all

test:
	uv run pytest tests


clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache
	rm -rf dist build
