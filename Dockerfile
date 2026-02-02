FROM node:20-slim

WORKDIR /app

# Install curl for healthcheck and create non-root user
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -s /bin/false appuser

# Copy package files
COPY package*.json ./

# Install production dependencies only
RUN npm ci --omit=dev

# Copy application code
COPY index.js ./

# Change ownership to non-root user
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Environment variables with defaults
ENV NODE_ENV=production
ENV HTTP_PORT=6000
ENV RATE_LIMIT_REQUESTS=100

EXPOSE 6000

# Healthcheck (docker-compose can override if needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:6000/health || exit 1

# Run in HTTP mode
CMD ["node", "index.js", "--http"]
