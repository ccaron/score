.PHONY: run_score test

run_score:
	uv pip install -e .
	uv run score

test:
	uv pip install -e .
	uv run pytest tests/

run_ui:
	-pkill -f "Google Chrome"
	open -a "Google Chrome" --args --kiosk --new-window http://localhost:8000


build_engine:
	docker build -t game-engine .

run_engine: build_engine
	docker run --rm -it --rm -p 8000:8000 game-engine

stop_engine:
	docker ps -q --filter ancestor=game-engine | xargs -r docker stop
