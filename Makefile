.PHONY: run_score test

run_score:
	uv pip install -e .
	uv run score

test:
	uv pip install -e .
	uv run pytest tests/
