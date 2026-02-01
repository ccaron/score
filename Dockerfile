FROM python:3.13-slim

# Prevent Python from writing pyc files & unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system deps (only if needed later)
# RUN apt-get update && apt-get install -y <deps> && rm -rf /var/lib/apt/lists/*

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

# Expose the app port
EXPOSE 8000

# Auto-start the app
CMD ["uv", "run", "score-app", "--host", "0.0.0.0", "--port", "8000"]

