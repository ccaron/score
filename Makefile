.PHONY: run run-app run-cloud run_container test kill-app kill-cloud

# Create virtual environment if it doesn't exist
.venv:
	uv venv

# Install dependencies (depends on venv existing)
.venv/.installed: .venv pyproject.toml
	uv pip install -e .
	@touch .venv/.installed

run: .venv/.installed
	@echo "Starting score-app and score-cloud..."
	@bash -c '\
		trap "echo \"Caught signal, cleaning up...\"; pkill -P $$$$; exit" INT TERM; \
		uv run score-app & \
		uv run score-cloud & \
		wait'

run-app: .venv/.installed
	@echo "Starting score-app in background..."
	@uv run score-app

run-cloud: .venv/.installed
	@echo "Starting score-cloud in background..."
	@uv run score-cloud

kill-app:
	@pkill -f "score-app" || echo "score-app not running"

kill-cloud:
	@pkill -f "score-cloud" || echo "score-cloud not running"

run_container:
	docker build -t game-engine .
	docker run --rm -it --rm -p 8000:8000 game-engine

test: .venv/.installed
	uv run pytest tests/
