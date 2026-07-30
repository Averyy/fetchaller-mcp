"""Configuration from environment variables."""

import ipaddress
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# OAuth TTL defaults (shared with OAuthStore)
AUTH_CODE_TTL = 10 * 60  # 10 minutes
ACCESS_TOKEN_TTL = 30 * 24 * 60 * 60  # 30 days
REFRESH_TOKEN_TTL = 180 * 24 * 60 * 60  # 180 days
CLIENT_TTL = 365 * 24 * 60 * 60  # 1 year


def _validate_server_origin(value: str | None) -> None:
    """Require one canonical, transport-safe OAuth protected-resource origin."""
    if value is None or value == "":
        return
    if not isinstance(value, str):
        raise ValueError("MCP_SERVER_URL must be a valid absolute origin")
    try:
        parsed_server = urlparse(value)
        server_port = parsed_server.port
    except ValueError:
        raise ValueError("MCP_SERVER_URL must be a valid absolute origin") from None

    server_host = parsed_server.hostname
    valid_host = False
    loopback_host = False
    if server_host:
        try:
            server_address = ipaddress.ip_address(server_host)
        except ValueError:
            valid_host = (
                server_host == "localhost"
                or (
                    len(server_host) <= 253
                    and not server_host.endswith(".")
                    and all(
                        re.fullmatch(
                            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                            label,
                        )
                        for label in server_host.split(".")
                    )
                )
            )
            loopback_host = server_host == "localhost"
        else:
            valid_host = True
            loopback_host = str(server_address) in {"127.0.0.1", "::1"}

    if (
        not value.isascii()
        or any(
            ord(character) <= 0x20
            or ord(character) == 0x7F
            or character == "\\"
            for character in value
        )
        or parsed_server.scheme not in {"http", "https"}
        or not valid_host
        or (parsed_server.scheme == "http" and not loopback_host)
        or parsed_server.username is not None
        or parsed_server.password is not None
        or parsed_server.path
        or parsed_server.params
        or parsed_server.query
        or parsed_server.fragment
        or "?" in value
        or "#" in value
        or server_port == 0
        or parsed_server.netloc.rsplit("@", 1)[-1].endswith(":")
    ):
        raise ValueError(
            "MCP_SERVER_URL must be an HTTPS origin (or exact loopback HTTP origin)"
        )


def _canonical_server_origin(value: str) -> str:
    """Return the browser/HTTP canonical spelling of a validated origin."""

    parsed = urlparse(value)
    hostname = (parsed.hostname or "").casefold()
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    ):
        port = None
    authority = (
        f"{authority_host}:{port}" if port is not None else authority_host
    )
    return f"{parsed.scheme.casefold()}://{authority}"


def _validate_reddit_oauth_value(
    name: str,
    value: str | None,
    *,
    max_length: int,
    allow_spaces: bool = False,
) -> None:
    """Keep OAuth credentials bounded and safe for their eventual HTTP header."""
    if value is None:
        return
    valid_character = (
        (lambda character: 0x20 <= ord(character) <= 0x7E)
        if allow_spaces
        else (lambda character: 0x21 <= ord(character) <= 0x7E)
    )
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or not value.isascii()
        or any(not valid_character(character) for character in value)
    ):
        raise ValueError(
            f"{name} must be non-empty printable ASCII of at most "
            f"{max_length} characters"
        )


def _validate_reddit_oauth_config(config: "Config") -> None:
    """Require either one direct token or one complete refresh credential set."""
    refresh_values = (
        config.reddit_client_id,
        config.reddit_client_secret,
        config.reddit_refresh_token,
    )
    if any(value is not None for value in refresh_values) and not all(
        value is not None for value in refresh_values
    ):
        raise ValueError(
            "REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and REDDIT_REFRESH_TOKEN "
            "must be configured together"
        )

    _validate_reddit_oauth_value(
        "REDDIT_CLIENT_ID",
        config.reddit_client_id,
        max_length=128,
    )
    if config.reddit_client_id is not None and ":" in config.reddit_client_id:
        raise ValueError("REDDIT_CLIENT_ID must not contain ':'")
    _validate_reddit_oauth_value(
        "REDDIT_CLIENT_SECRET",
        config.reddit_client_secret,
        max_length=4096,
    )
    _validate_reddit_oauth_value(
        "REDDIT_REFRESH_TOKEN",
        config.reddit_refresh_token,
        max_length=4096,
    )
    _validate_reddit_oauth_value(
        "REDDIT_ACCESS_TOKEN",
        config.reddit_access_token,
        max_length=8192,
    )
    _validate_reddit_oauth_value(
        "REDDIT_USER_AGENT",
        config.reddit_user_agent,
        max_length=256,
        allow_spaces=True,
    )


def _validate_browser_executable_path(value: str | None) -> None:
    """Keep an optional caller-pinned browser path bounded and unambiguous."""

    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\x00" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(
            "BROWSER_EXECUTABLE_PATH must be a non-empty path without "
            "control characters"
        )


@dataclass(frozen=True)
class Config:
    """Application configuration loaded from environment variables."""

    # Server settings
    http_port: int = 6000
    server_url: str | None = None  # Defaults to http://localhost:{port}
    api_key: str | None = None
    jwt_secret: str | None = None
    allow_ephemeral_jwt: bool = False
    rate_limit_requests: int = 100  # Per minute per IP
    data_dir: str | None = None
    trusted_proxy_ips: tuple[str, ...] = ()
    browser_preflight: bool = False
    browser_executable_path: str | None = None

    # Fetch defaults
    default_max_tokens: int = 25000
    default_timeout_seconds: int = 10
    chars_per_token: int = 4
    max_pdf_size: int = 50 * 1024 * 1024  # 50MB
    pdf_processing_timeout: int = 30  # seconds

    # OAuth TTLs
    auth_code_ttl: int = AUTH_CODE_TTL
    access_token_ttl: int = ACCESS_TOKEN_TTL
    refresh_token_ttl: int = REFRESH_TOKEN_TTL
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

    # Reddit user-context OAuth carries validated public API reads on hosted
    # networks, exact moderator rosters, and some wiki page indexes.
    reddit_client_id: str | None = field(default=None, repr=False)
    reddit_client_secret: str | None = field(default=None, repr=False)
    reddit_refresh_token: str | None = field(default=None, repr=False)
    reddit_access_token: str | None = field(default=None, repr=False)
    reddit_user_agent: str = "fetchaller-mcp/3 exact-reddit-reads"

    # Wafer cookie cache directory (persists cookies across restarts).
    # Set to "" to disable. By default this lives under DATA_DIR.
    wafer_cache_dir: str | None = None

    # Retry settings - tuned for fast failure detection
    # With 1 retry, max delay is 0.5s (vs 7s with old defaults)
    retry_max_attempts: int = 1
    retry_initial_delay: float = 0.5
    retry_max_delay: float = 5.0
    retry_exponential_base: int = 2
    retry_jitter: float = 0.1

    def __post_init__(self) -> None:
        """Validate invariants required by every Config construction path."""
        _validate_server_origin(self.server_url)
        _validate_reddit_oauth_config(self)
        _validate_browser_executable_path(self.browser_executable_path)
        if (
            self.api_key
            and self.jwt_secret
            and len(self.jwt_secret.encode()) < 32
        ):
            raise ValueError("JWT_SECRET must contain at least 32 bytes")

    @property
    def effective_server_url(self) -> str:
        """Get server URL, defaulting to localhost if not set."""
        if self.server_url:
            return _canonical_server_origin(self.server_url)
        return f"http://localhost:{self.http_port}"

    @property
    def reddit_moderator_oauth_configured(self) -> bool:
        """Whether an access token or complete refresh flow is available."""
        return self.reddit_access_token is not None or self.reddit_refresh_token is not None


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

    def _optional(key: str) -> str | None:
        return os.environ.get(key) or None

    # Parse and validate port
    http_port = _int("HTTP_PORT", 6000)
    if not (1 <= http_port <= 65535):
        raise ValueError(f"HTTP_PORT must be between 1 and 65535, got {http_port}")

    # Parse rate limit with validation
    rate_limit = _int("RATE_LIMIT_REQUESTS", 100)
    if rate_limit < 1:
        raise ValueError(f"RATE_LIMIT_REQUESTS must be positive, got {rate_limit}")

    default_data_root = Path(
        os.environ.get(
            "XDG_DATA_HOME",
            str(Path.home() / ".local" / "share"),
        )
    )
    data_dir = os.environ.get("DATA_DIR") or str(default_data_root / "fetchaller")
    wafer_cache_dir = os.environ.get(
        "WAFER_CACHE_DIR",
        str(Path(data_dir) / "wafer"),
    )
    trusted_proxy_ips = tuple(
        value.strip()
        for value in os.environ.get("TRUSTED_PROXY_IPS", "").split(",")
        if value.strip()
    )
    for value in trusted_proxy_ips:
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise ValueError(f"TRUSTED_PROXY_IPS contains invalid CIDR/address: {value!r}") from None

    config = Config(
        http_port=http_port,
        server_url=os.environ.get("MCP_SERVER_URL"),
        wafer_cache_dir=wafer_cache_dir,
        api_key=os.environ.get("MCP_API_KEY"),
        jwt_secret=os.environ.get("JWT_SECRET"),
        allow_ephemeral_jwt=os.environ.get("ALLOW_EPHEMERAL_JWT") == "1",
        data_dir=data_dir,
        trusted_proxy_ips=trusted_proxy_ips,
        browser_preflight=os.environ.get("BROWSER_PREFLIGHT", "0") == "1",
        browser_executable_path=_optional("BROWSER_EXECUTABLE_PATH"),
        rate_limit_requests=rate_limit,
        access_token_ttl=_int("ACCESS_TOKEN_TTL", ACCESS_TOKEN_TTL),
        # Cache settings
        cache_default_ttl=_int("CACHE_DEFAULT_TTL", 300),
        cache_max_entries=_int("CACHE_MAX_ENTRIES", 1000),
        cache_max_entry_size=_int("CACHE_MAX_ENTRY_SIZE", 1_000_000),
        # Reddit queue
        reddit_max_requests_per_minute=_int("REDDIT_MAX_REQUESTS_PER_MINUTE", 10),
        reddit_proactive_threshold=_int("REDDIT_PROACTIVE_THRESHOLD", 8),
        reddit_backoff_rate_limit=_int("REDDIT_BACKOFF_RATE_LIMIT", 60),
        reddit_backoff_blocked=_int("REDDIT_BACKOFF_BLOCKED", 300),
        reddit_client_id=_optional("REDDIT_CLIENT_ID"),
        reddit_client_secret=_optional("REDDIT_CLIENT_SECRET"),
        reddit_refresh_token=_optional("REDDIT_REFRESH_TOKEN"),
        reddit_access_token=_optional("REDDIT_ACCESS_TOKEN"),
        reddit_user_agent=os.environ.get(
            "REDDIT_USER_AGENT",
            "fetchaller-mcp/3 exact-reddit-reads",
        ),
        # Retry
        retry_max_attempts=_int("RETRY_MAX_ATTEMPTS", 1),
        retry_initial_delay=_float("RETRY_INITIAL_DELAY", 0.5),
        retry_max_delay=_float("RETRY_MAX_DELAY", 5.0),
    )

    for name, value in (
        ("RETRY_INITIAL_DELAY", config.retry_initial_delay),
        ("RETRY_MAX_DELAY", config.retry_max_delay),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number")
    # Validate relationships
    if config.retry_initial_delay > config.retry_max_delay:
        raise ValueError(
            f"retry_initial_delay ({config.retry_initial_delay}) must be <= retry_max_delay ({config.retry_max_delay})"
        )
    if config.retry_max_attempts < 0:
        raise ValueError(f"retry_max_attempts must be >= 0, got {config.retry_max_attempts}")
    if config.access_token_ttl <= 0:
        raise ValueError(f"ACCESS_TOKEN_TTL must be positive, got {config.access_token_ttl}")
    if config.reddit_max_requests_per_minute < 1:
        raise ValueError("REDDIT_MAX_REQUESTS_PER_MINUTE must be positive")
    if not 1 <= config.reddit_proactive_threshold <= config.reddit_max_requests_per_minute:
        raise ValueError(
            "REDDIT_PROACTIVE_THRESHOLD must be between 1 and "
            "REDDIT_MAX_REQUESTS_PER_MINUTE"
        )
    if config.reddit_backoff_rate_limit < 0 or config.reddit_backoff_blocked < 0:
        raise ValueError("reddit backoff values must be non-negative")
    if config.cache_default_ttl < 0:
        raise ValueError("CACHE_DEFAULT_TTL must be non-negative")
    if config.cache_max_entries < 1 or config.cache_max_entry_size < 1:
        raise ValueError("CACHE_MAX_ENTRIES and CACHE_MAX_ENTRY_SIZE must be positive")
    return config


# Module-level wafer cache dir — set once at server startup via set_wafer_cache_dir().
# Modules that create wafer sessions import get_wafer_cache_dir() to get the path.
_wafer_cache_dir: str | None = None


def set_wafer_cache_dir(path: str | None) -> None:
    """Set the global wafer cache directory. Called once at server startup."""
    global _wafer_cache_dir
    _wafer_cache_dir = path if path else None


def get_wafer_cache_dir() -> str | None:
    """Get the wafer cache directory, or None if disabled/unset."""
    return _wafer_cache_dir


# Tracking params to strip for URL normalization
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
    # NB: bare "ref" and "source" are intentionally NOT stripped — many sites use
    # them as *semantic* params (?source=homepage vs ?source=email-blast select
    # different content), so collapsing them into one cache key served the wrong
    # cached body within the TTL. Only unambiguous ad/analytics tokens belong here.
    "ref_src",
    "ref_url",
    "_ga",
    "_gl",
    "mc_cid",
    "mc_eid",
}
