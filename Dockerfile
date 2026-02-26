FROM python:3.12-slim

WORKDIR /app

# Install curl for healthcheck, gosu for entrypoint, and create non-root user
RUN apt-get update && apt-get install -y \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 100 -o appuser \
    && useradd -r -s /bin/false -u 99 -g appuser appuser

# Install uv for fast package management
RUN pip install --no-cache-dir uv

# Copy project files and lockfile
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Install from lockfile (exact pinned versions), then install patchright browser
RUN uv sync --frozen --no-dev --no-editable \
    && .venv/bin/python -m patchright install chromium --with-deps

# Add venv to PATH so python/fetchaller-mcp use the installed packages
ENV PATH="/app/.venv/bin:$PATH"

# Create data directory for persistent state
RUN mkdir -p /app/data

# Change ownership to non-root user
RUN chown -R appuser:appuser /app

# Entrypoint fixes volume permissions then drops to appuser
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Environment variables with defaults
ENV HTTP_PORT=6000
ENV RATE_LIMIT_REQUESTS=100

# Default port; override with HTTP_PORT env var
EXPOSE 6000

# Healthcheck (uses shell form for variable expansion)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${HTTP_PORT}/health || exit 1

# Run in HTTP mode
CMD ["python", "-m", "fetchaller.main", "--http"]
