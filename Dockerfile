FROM python:3.13-slim

# Prevent Python from writing pyc files & unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system deps for Grafana Agent download
RUN apt-get update && apt-get install -y wget unzip && rm -rf /var/lib/apt/lists/*

# Copy dependency metadata first for caching
COPY pyproject.toml README.md /app/

# Install uv (CLI for uvicorn) and Python dependencies
RUN pip install --upgrade pip \
 && pip install --no-cache-dir uv

# Copy app source
COPY src /app/src

# Set Python path
ENV PYTHONPATH=/app/src

# Editable install of your package (after source is copied)
RUN uv pip install --system -e .

# Download Grafana Agent (ARM64 Linux)
RUN wget https://github.com/grafana/agent/releases/download/v0.37.2/grafana-agent-linux-arm64.zip \
    && apt-get install -y unzip \
    && unzip grafana-agent-linux-arm64.zip \
    && mv grafana-agent-linux-arm64 /usr/local/bin/grafana-agent \
    && chmod +x /usr/local/bin/grafana-agent \
    && rm -rf grafana-agent-linux-arm64.zip

# Copy startup script and agent config
COPY start.sh /app/start.sh
COPY agent-config.yaml /etc/agent-config.yaml
RUN chmod +x /app/start.sh

# Expose the app port (uvicorn metrics) and agent port (optional)
# Default ports as build-time environment variables
EXPOSE 8000
EXPOSE 3100

# Start both the app and the Grafana Agent
CMD ["/app/start.sh"]
