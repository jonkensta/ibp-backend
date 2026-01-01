# Use uv's official Python 3.13 image (includes uv pre-installed)
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Install system dependencies (fonts for label generation, curl for inmate lookups)
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-dejavu curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1

# Configure uv to use system Python
ENV UV_PYTHON_DOWNLOADS=never

# Layer 1: Install dependencies only (cached separately from code changes)
# This layer is only rebuilt when pyproject.toml or uv.lock changes
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Layer 2: Copy application code
COPY ibp ./ibp
COPY sample.conf ./

# Layer 3: Install the project (fast since dependencies already cached)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev

# Create data directory for SQLite
RUN mkdir -p /data

# Expose port
EXPOSE 8000

# Run the application
CMD ["uv", "run", "uvicorn", "ibp.base:app", "--host", "0.0.0.0", "--port", "8000"]
