.PHONY: run run-cloud run-both test kill kill-app kill-cloud

run:
	@uv pip install -e .
	@echo "Starting score-app and score-cloud..."
	@bash -c '\
		trap "echo \"Caught signal, cleaning up...\"; pkill -P $$$$; exit" INT TERM; \
		uv run score-app & \
		uv run score-cloud & \
		wait'

run_container:
	docker build -t game-engine .
	docker run --rm -it --rm -p 8000:8000 game-engine

test:
	uv pip install -e .
	uv run pytest tests/
