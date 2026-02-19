"""Configuration from environment variables."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

# OAuth TTL defaults (shared with OAuthStore)
AUTH_CODE_TTL = 10 * 60  # 10 minutes
ACCESS_TOKEN_TTL = 365 * 24 * 60 * 60  # 1 year
CLIENT_TTL = 365 * 24 * 60 * 60  # 1 year


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
    auth_code_ttl: int = AUTH_CODE_TTL
    access_token_ttl: int = ACCESS_TOKEN_TTL
    client_ttl: int = CLIENT_TTL

    # Memory limits
    max_oauth_clients: int = 1000
    max_auth_codes: int = 5000
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

    # Botfighter settings
    chrome_idle_timeout: int = 60  # Minutes before idle Chrome shuts down
    cookie_cache_path: str | None = None  # Path to persist bot cookies (JSON)

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


def _load_dotenv() -> None:
    """Load .env file into os.environ (existing vars take precedence)."""
    # Walk up from this file to find .env (supports installed and dev layouts)
    for parent in Path(__file__).resolve().parents:
        env_file = parent / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Don't override existing env vars
                if key and key not in os.environ:
                    os.environ[key] = value
            break


def load_config() -> Config:
    """Load configuration from environment variables."""
    _load_dotenv()

    def _int(key: str, default: int) -> int:
        raw = os.environ.get(key, str(default))
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{key} must be an integer, got {raw!r}") from None

    def _float(key: str, default: float) -> float:
        raw = os.environ.get(key, str(default))
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{key} must be a number, got {raw!r}") from None

    # Parse and validate port
    http_port = _int("HTTP_PORT", 6000)
    if not (1 <= http_port <= 65535):
        raise ValueError(f"HTTP_PORT must be between 1 and 65535, got {http_port}")

    # Parse rate limit with validation
    rate_limit = _int("RATE_LIMIT_REQUESTS", 100)
    if rate_limit < 1:
        raise ValueError(f"RATE_LIMIT_REQUESTS must be positive, got {rate_limit}")

    config = Config(
        http_port=http_port,
        server_url=os.environ.get("MCP_SERVER_URL"),
        api_key=os.environ.get("MCP_API_KEY"),
        jwt_secret=os.environ.get("JWT_SECRET"),
        rate_limit_requests=rate_limit,
        # Cache settings
        cache_default_ttl=_int("CACHE_DEFAULT_TTL", 300),
        cache_max_entries=_int("CACHE_MAX_ENTRIES", 1000),
        cache_max_entry_size=_int("CACHE_MAX_ENTRY_SIZE", 1_000_000),
        # Reddit queue
        reddit_max_requests_per_minute=_int("REDDIT_MAX_REQUESTS_PER_MINUTE", 10),
        reddit_proactive_threshold=_int("REDDIT_PROACTIVE_THRESHOLD", 8),
        reddit_backoff_rate_limit=_int("REDDIT_BACKOFF_RATE_LIMIT", 60),
        reddit_backoff_blocked=_int("REDDIT_BACKOFF_BLOCKED", 300),
        # Botfighter
        chrome_idle_timeout=_int("CHROME_IDLE_TIMEOUT", 60),
        cookie_cache_path=os.environ.get(
            "COOKIE_CACHE_PATH",
            "/app/data/cookies.json" if os.path.isdir("/app/data") else None,
        ),
        # Retry
        retry_max_attempts=_int("RETRY_MAX_ATTEMPTS", 1),
        retry_initial_delay=_float("RETRY_INITIAL_DELAY", 0.5),
        retry_max_delay=_float("RETRY_MAX_DELAY", 5.0),
    )

    # Validate relationships
    if config.retry_initial_delay > config.retry_max_delay:
        raise ValueError(
            f"retry_initial_delay ({config.retry_initial_delay}) must be <= retry_max_delay ({config.retry_max_delay})"
        )
    if config.retry_max_attempts < 0:
        raise ValueError(f"retry_max_attempts must be >= 0, got {config.retry_max_attempts}")
    if config.reddit_proactive_threshold > config.reddit_max_requests_per_minute:
        raise ValueError(
            f"reddit_proactive_threshold ({config.reddit_proactive_threshold}) must be <= reddit_max_requests_per_minute ({config.reddit_max_requests_per_minute})"
        )
    if config.reddit_backoff_rate_limit < 0 or config.reddit_backoff_blocked < 0:
        raise ValueError("reddit backoff values must be non-negative")

    return config


_FALLBACK_FINGERPRINTS = ["chrome133a", "chrome136", "chrome142"]


def _chrome_version(fp: str) -> tuple[int, str]:
    """Sort key for Chrome fingerprints: 'chrome133a' → (133, 'a').

    Sub-versions (e.g. 'a') sort after the base version, so chrome133a > chrome133.
    """
    m = re.match(r"chrome(\d+)(.*)", fp)
    return (int(m.group(1)), m.group(2)) if m else (0, fp)


def _discover_chrome_fingerprints() -> list[str]:
    """Discover Chrome desktop fingerprints from curl_cffi's BrowserType enum.

    Auto-updates when curl_cffi adds new Chrome versions. Returns the newest 3
    sorted by version number. curl_cffi handles all TLS + header fingerprinting
    (including Sec-Ch-Ua) natively via the impersonate parameter — no manual
    header overrides needed.
    """
    try:
        from curl_cffi.requests import BrowserType
        chrome_fps = [
            member.value
            for member in BrowserType
            if member.value.startswith("chrome") and "android" not in member.value
        ]
    except (ImportError, AttributeError):
        chrome_fps = []
    # Fallback: last known good values — only used if curl_cffi changes its
    # BrowserType API or returns no Chrome desktop entries.
    if not chrome_fps:
        chrome_fps = list(_FALLBACK_FINGERPRINTS)
    return sorted(chrome_fps, key=_chrome_version)[-3:]


# Newest 3 Chrome fingerprints for rotation (auto-discovered from curl_cffi).
# When curl_cffi is upgraded with new Chrome versions, this list updates automatically.
# curl_cffi sets Sec-Ch-Ua, User-Agent, and TLS fingerprint natively — no manual
# header dicts needed.
BROWSER_FINGERPRINTS = _discover_chrome_fingerprints()

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
