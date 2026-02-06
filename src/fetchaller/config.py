"""Configuration from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Application configuration loaded from environment variables."""

    # Server settings
    http_port: int = 6000
    server_url: str | None = None  # Defaults to http://localhost:{port}
    api_key: str | None = None
    jwt_secret: str | None = None
    rate_limit_requests: int = 100  # Per minute per IP

    # Fetch defaults
    default_max_tokens: int = 25000
    default_timeout_seconds: int = 10
    chars_per_token: int = 4
    max_pdf_size: int = 50 * 1024 * 1024  # 50MB
    pdf_processing_timeout: int = 30  # seconds

    # OAuth TTLs
    auth_code_ttl: int = 10 * 60  # 10 minutes
    access_token_ttl: int = 365 * 24 * 60 * 60  # 1 year
    client_ttl: int = 365 * 24 * 60 * 60  # 1 year

    # Memory limits
    max_oauth_clients: int = 1000
    max_auth_codes: int = 5000
    max_access_tokens: int = 10000
    max_rate_limit_entries: int = 10000

    # Cache settings (NEW)
    cache_default_ttl: int = 300  # 5 minutes
    cache_max_entries: int = 1000
    cache_max_entry_size: int = 1_000_000  # 1MB

    # Reddit queue settings (NEW)
    reddit_max_requests_per_minute: int = 10
    reddit_proactive_threshold: int = 8  # Start slowing at 8/10
    reddit_backoff_rate_limit: int = 60  # After 429
    reddit_backoff_blocked: int = 300  # After 403

    # Retry settings - tuned for fast failure detection
    # With 1 retry, max delay is 0.5s (vs 7s with old defaults)
    retry_max_attempts: int = 1
    retry_initial_delay: float = 0.5
    retry_max_delay: float = 5.0
    retry_exponential_base: int = 2
    retry_jitter: float = 0.1

    @property
    def effective_server_url(self) -> str:
        """Get server URL, defaulting to localhost if not set."""
        return self.server_url or f"http://localhost:{self.http_port}"


def load_config() -> Config:
    """Load configuration from environment variables."""
    # Parse and validate port
    http_port = int(os.environ.get("HTTP_PORT", "6000"))
    if not (1 <= http_port <= 65535):
        raise ValueError(f"HTTP_PORT must be between 1 and 65535, got {http_port}")

    # Parse rate limit with validation
    rate_limit = int(os.environ.get("RATE_LIMIT_REQUESTS", "100"))
    if rate_limit < 1:
        raise ValueError(f"RATE_LIMIT_REQUESTS must be positive, got {rate_limit}")

    return Config(
        http_port=http_port,
        server_url=os.environ.get("MCP_SERVER_URL"),
        api_key=os.environ.get("MCP_API_KEY"),
        jwt_secret=os.environ.get("JWT_SECRET"),
        rate_limit_requests=rate_limit,
    )


# Browser fingerprints for TLS impersonation rotation (NEW)
BROWSER_FINGERPRINTS = ["chrome131", "chrome133a", "chrome136", "chrome142"]

# Tracking params to strip for URL normalization (NEW)
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "msclkid",
    "ref",
    "source",
    "ref_src",
    "ref_url",
    "_ga",
    "_gl",
    "mc_cid",
    "mc_eid",
}
