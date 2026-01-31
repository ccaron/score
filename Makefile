.PHONY: run_score test

run:
	uv pip install -e .
	uv run score

run_container:
	docker build -t game-engine .
	docker run --rm -it --rm -p 8000:8000 game-engine

test:
	uv pip install -e .
	uv run pytest tests/





