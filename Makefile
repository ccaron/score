.PHONY: run run-app run-cloud run_container test kill-app kill-cloud schedule

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

# Run tests with optional filtering
# Usage:
#   make test                              # Run all tests
#   make test FILTER=test_cli.py          # Run specific test file
#   make test FILTER=test_goals.py::test_add_home_goal  # Run specific test
#   make test FILTER="-k goal"            # Run tests matching keyword
#   make test FILTER="-k goal -v"         # Run tests matching keyword with verbose output
FILTER ?=
test: .venv/.installed
	@if [ -z "$(FILTER)" ]; then \
		uv run pytest tests/; \
	else \
		uv run pytest $(FILTER); \
	fi

# Generate a schedule from a YAML config file
# Usage: make schedule CONFIG=examples/schedule.yaml
CONFIG ?= examples/schedule.yaml
schedule: .venv/.installed
	uv run score-schedule $(CONFIG)

# Generate an HTML schedule from a YAML config file
# Usage: make schedule-html CONFIG=examples/schedule.yaml OUTPUT=schedule.html
OUTPUT ?= schedule.html
schedule-html: .venv/.installed
	uv run score-schedule $(CONFIG) --html $(OUTPUT)
