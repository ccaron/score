#!/bin/bash

# Ensure the log file exists
touch /tmp/app.log

# Start your Python app
uv run score --host 0.0.0.0 --port 8000 2>&1 | tee /tmp/app.log &

# Start Grafana Agent
grafana-agent --config.file /etc/agent-config.yaml &

# Wait for both processes
wait
