FROM python:3.12-slim

WORKDIR /app

# Install curl for healthcheck, gosu for entrypoint, xvfb for the browser
# solver (wafer runs Chrome headful, which needs an X display), and create the
# non-root user.
RUN apt-get update && apt-get install -y \
    curl \
    gosu \
    xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 100 -o appuser \
    && useradd -r -s /bin/false -u 99 -g appuser appuser

# Install uv for fast package management
RUN pip install --no-cache-dir uv

# Copy project files and lockfile
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Browsers must live inside /app so appuser can reach them at runtime.
# The build runs as root, so patchright's default ($HOME/.cache/ms-playwright)
# installs to /root/.cache — which is mode 0700 and unreadable by appuser, leaving
# BrowserSolver unable to launch Chromium. Must be set BEFORE the install below,
# and it persists into the running container so the same path is resolved at
# runtime. See todo-survivereboots.md (Finding E).
ENV PLAYWRIGHT_BROWSERS_PATH=/app/browsers

# Install from lockfile (exact pinned versions), then install the browsers.
#
# TWO browsers, deliberately:
#   chromium — patchright's bundled build, used for generic page rendering.
#   chrome   — real system Google Chrome in /opt/google/chrome. wafer's
#              BrowserSolver hardcodes channel="chrome" for stealth, so WITHOUT
#              this every challenge solve dies at launch with "Chromium
#              distribution 'chrome' is not found". Installing chromium alone
#              looks right and silently does not work.
#
# Google ships no Linux arm64 Chrome build, so that step is amd64-only and must
# not fail the build on arm64 (local Apple Silicon dev); challenge solving is
# simply unavailable there. Production CI builds amd64.
RUN uv sync --frozen --no-dev --no-editable \
    && .venv/bin/python -m patchright install chromium --with-deps \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         .venv/bin/python -m patchright install chrome --with-deps; \
       else \
         echo "WARNING: skipping system Chrome (no Google Chrome build for $(dpkg --print-architecture)); browser challenge solving will be unavailable"; \
       fi

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
# Virtual display for the headful browser solver. Declared here (not exported by
# entrypoint.sh) so `docker exec` sessions see the same value the server uses;
# entrypoint.sh starts the Xvfb instance that serves it.
ENV DISPLAY=:99
# Cookie cache must go in the mounted volume, not $HOME. appuser has no home
# directory in this image, so wafer's default (~/.cache/fetchaller/wafer) fails
# to create and cookie caching silently no-ops. See todo-survivereboots.md
# (Finding D). /app/data is chowned to appuser by entrypoint.sh.
ENV WAFER_CACHE_DIR=/app/data/wafer

# Default port; override with HTTP_PORT env var
EXPOSE 6000

# Healthcheck (uses shell form for variable expansion)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${HTTP_PORT}/health || exit 1

# Run in HTTP mode
CMD ["python", "-m", "fetchaller.main", "--http"]
