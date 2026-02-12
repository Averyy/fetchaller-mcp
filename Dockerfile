FROM python:3.12-slim

WORKDIR /app

# Install curl for healthcheck, chromium + xvfb for botfighter, gosu for entrypoint, and create non-root user
RUN apt-get update && apt-get install -y \
    curl \
    chromium \
    xvfb \
    gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 100 -o appuser \
    && useradd -r -s /bin/false -u 99 -g appuser appuser

# Install uv for fast package management
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml ./
COPY src/ ./src/

# Install dependencies using uv
RUN uv pip install --system --no-cache ".[botfighter]"

# Create data directory for persistent cookie cache
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
ENV CHROME_IDLE_TIMEOUT=60

# Default port; override with HTTP_PORT env var
EXPOSE 6000

# Healthcheck (uses shell form for variable expansion)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${HTTP_PORT}/health || exit 1

# Run in HTTP mode
CMD ["python", "-m", "fetchaller.main", "--http"]
