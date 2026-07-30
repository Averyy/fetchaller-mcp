FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG TARGETARCH
ARG CHROME_VERSION=149.0.7827.201
ARG CHROME_SHA256=528800a1e74fbf42ff4d7768de5635fb0c4e1f3c070680f693d7dde6642d4415

WORKDIR /app

# Install curl for healthcheck, gosu for privilege dropping, tini for signal
# forwarding/reaping, and an X server plus a small window manager for wafer's
# headful browser solver.  Chrome's headful window needs a window manager in
# addition to Xvfb; otherwise its window metrics expose the virtual display.
RUN apt-get update && apt-get install -y \
    curl \
    gosu \
    jwm \
    tini \
    unzip \
    x11-utils \
    xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 100 -o appuser \
    && useradd -r -s /bin/false -u 99 -g appuser appuser

# Install uv for fast package management
RUN pip install --no-cache-dir uv==0.11.29

# Copy project files and lockfile
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Image-bundled, pinned reCAPTCHA ONNX assets live outside the mutable
# /app/data volume. Runtime is offline so a customer request can never trigger
# an unbounded model download.
ENV HF_HOME=/app/model-cache

# Install from the lockfile, browser runtime libraries, and the exact official
# Chrome-for-Testing build whose major matches wafer/wreq's TLS emulation.
#
# Using Patchright's moving ``chrome`` channel would silently upgrade the
# browser ahead of wreq and split one protected session across different
# browser/TLS identities. Chrome-for-Testing is an official Google build with
# immutable versioned archives. This build verifies the exact four-part version;
# BrowserSolver validates branded Chrome and warns on a version mismatch. Google
# publishes no Linux arm64 build, so fail that architecture explicitly;
# docker-compose.local.yml requests amd64 for Apple Silicon hosts.
RUN if [ "$TARGETARCH" != "amd64" ]; then \
      echo "fetchaller's browser-complete image requires linux/amd64" >&2; \
      exit 1; \
    fi \
    && uv sync --frozen --no-dev --no-editable \
    && uv run python -m patchright install-deps chromium \
    && curl --fail --location --retry 3 \
      --output /tmp/chrome-for-testing.zip \
      "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-linux64.zip" \
    && printf '%s  %s\n' "$CHROME_SHA256" /tmp/chrome-for-testing.zip \
      | sha256sum -c - \
    && rm -rf /opt/google/chrome \
    && unzip -q /tmp/chrome-for-testing.zip -d /opt/google \
    && mv /opt/google/chrome-linux64 /opt/google/chrome \
    && rm -f /tmp/chrome-for-testing.zip \
    && chown -R root:root /opt/google/chrome \
    && chmod 4755 /opt/google/chrome/chrome_sandbox \
    && ACTUAL_CHROME_VERSION=$(/opt/google/chrome/chrome --version) \
    && echo "Pinned browser: ${ACTUAL_CHROME_VERSION}" \
    && test "$(printf '%s\n' "$ACTUAL_CHROME_VERSION" | awk '{print $NF}')" = \
      "$CHROME_VERSION" \
    && HF_HUB_OFFLINE=0 uv run python -c "from huggingface_hub import snapshot_download; snapshot_download('Averyyyyyy/wafer-models', revision='ee0a26676466f7c6845d75ea7f6ea46a4306bbba', allow_patterns=['wafer_cls_s.onnx', 'wafer_det_s.onnx'])" \
    && printf '%s  %s\n%s  %s\n' \
        '7a012f058e0c64160aaa9511923da66c65ca424579f18fc96a483f8638dccc65' "$HF_HOME/hub/models--Averyyyyyy--wafer-models/snapshots/ee0a26676466f7c6845d75ea7f6ea46a4306bbba/wafer_cls_s.onnx" \
        '6b27ce390befa8afa7de37416084ea79b87d101635d3783f04f5308191da001b' "$HF_HOME/hub/models--Averyyyyyy--wafer-models/snapshots/ee0a26676466f7c6845d75ea7f6ea46a4306bbba/wafer_det_s.onnx" \
        | sha256sum -c -

# Add venv to PATH so python/fetchaller-mcp use the installed packages
ENV PATH="/app/.venv/bin:$PATH"

# Create data directory for persistent state
RUN mkdir -p /app/data

# Keep code, browser assets, and model artifacts immutable to the service
# user. Only /app/data is mutable and is chowned again by entrypoint.sh for a
# caller-provided PUID/PGID.
RUN chown -R root:root /app \
    && chown -R appuser:appuser /app/data \
    && chmod -R a-w /app/model-cache

# Entrypoint fixes volume permissions then drops to appuser
COPY entrypoint.sh /entrypoint.sh
COPY healthcheck.sh /healthcheck.sh
RUN chmod +x /entrypoint.sh /healthcheck.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]

# Environment variables with defaults
ENV HTTP_PORT=6000
ENV HTTP_STARTUP_TIMEOUT=180
ENV APP_SHUTDOWN_TIMEOUT=60
ENV RATE_LIMIT_REQUESTS=100
# Virtual display for the headful browser solver. Declared here (not exported by
# entrypoint.sh) so `docker exec` sessions see the same value the server uses;
# entrypoint.sh starts the Xvfb instance that serves it.
ENV DISPLAY=:99
ENV BROWSER_PREFLIGHT=1
ENV BROWSER_EXECUTABLE_PATH=/opt/google/chrome/chrome
# Chrome-for-Testing still initializes Crashpad and fontconfig when its
# per-context profile is temporary. appuser intentionally has no writable home,
# so give those process-level caches explicit ephemeral locations.
ENV XDG_CONFIG_HOME=/tmp/fetchaller-xdg/config
ENV XDG_CACHE_HOME=/tmp/fetchaller-xdg/cache
ENV DATA_DIR=/app/data
# Cookie cache must go in the mounted volume, not $HOME. appuser has no home
# directory in this image, so wafer's home-directory default is not durable.
# /app/data is chowned to appuser by entrypoint.sh.
ENV WAFER_CACHE_DIR=/app/data/wafer
ENV HF_HUB_OFFLINE=1

# Default port; override with HTTP_PORT env var
EXPOSE 6000

# Healthcheck (uses shell form for variable expansion)
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["/healthcheck.sh"]

# Run in HTTP mode
CMD ["python", "-m", "fetchaller.main", "--http"]
