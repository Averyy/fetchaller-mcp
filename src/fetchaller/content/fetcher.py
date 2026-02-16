"""Content fetcher with curl_cffi TLS fingerprint impersonation."""

import asyncio
import os
import random
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession, Response

from ..config import BROWSER_FINGERPRINTS, FINGERPRINT_SEC_CH_UA, Config


def _find_ca_bundle() -> str | None:
    """Find the system CA certificate bundle for SSL verification.

    curl_cffi's built-in CA bundle is incomplete (missing some CAs like
    Cloudflare's intermediate certs), so we use the system bundle instead.

    Returns path to CA bundle, or None to use curl_cffi's default.
    """
    # 1. Respect explicit environment override
    env_path = os.environ.get("CURL_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. System CA bundle (works on macOS + Linux)
    system_ca = ssl.get_default_verify_paths().cafile
    if system_ca and os.path.isfile(system_ca):
        return system_ca

    # 3. Fall back to curl_cffi's default
    return None


@dataclass
class FetchResult:
    """Result from fetching a URL."""

    content: bytes
    content_type: str
    status_code: int
    final_url: str
    headers: dict[str, str]


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    # Defaults match Config defaults for consistency
    max_retries: int = 1
    initial_delay: float = 0.5
    max_delay: float = 5.0
    exponential_base: int = 2
    jitter: float = 0.1  # +/- 10% randomization

    @classmethod
    def from_config(cls, config: Config) -> "RetryConfig":
        return cls(
            max_retries=config.retry_max_attempts,
            initial_delay=config.retry_initial_delay,
            max_delay=config.retry_max_delay,
            exponential_base=config.retry_exponential_base,
            jitter=config.retry_jitter,
        )


class ContentFetcher:
    """
    Fetches content using curl_cffi with TLS fingerprint impersonation.

    Features:
    - Browser fingerprint rotation (chrome131, chrome133a, chrome136, chrome142)
    - Smart retry with exponential backoff
    - Max response size enforcement (default 20MB)
    """

    MAX_RESPONSE_SIZE = 20 * 1024 * 1024  # 20MB

    # Browser navigation headers (Sec-Ch-Ua set dynamically per-request)
    _DEFAULT_HEADERS_BASE = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "DNT": "1",
        "Priority": "u=0, i",
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    # JSON API headers (Sec-Ch-Ua set dynamically per-request)
    _JSON_HEADERS_BASE = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(
        self,
        retry_config: RetryConfig | None = None,
    ):
        self.retry_config = retry_config or RetryConfig()
        self._browser = random.choice(BROWSER_FINGERPRINTS)
        self._session: AsyncSession | None = None
        self._identity_pinned: bool = False

    async def _get_session(self) -> AsyncSession:
        """Get or create async session with system CA bundle for SSL."""
        if self._session is None:
            ca_bundle = _find_ca_bundle()
            self._session = AsyncSession(verify=ca_bundle) if ca_bundle else AsyncSession()
        return self._session

    def rotate_fingerprint(self) -> None:
        """Rotate to a different browser fingerprint (call after 403/block).

        Skipped when identity is pinned (using cached botfighter cookies).
        """
        if self._identity_pinned:
            return
        old = self._browser
        available = [f for f in BROWSER_FINGERPRINTS if f != old]
        self._browser = random.choice(available) if available else old

    async def fetch(
        self,
        url: str,
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
        use_json_headers: bool = False,
        allow_redirects: bool = True,
    ) -> FetchResult:
        """
        Fetch a URL with retry and fingerprint rotation.

        Args:
            url: URL to fetch
            timeout: Request timeout in seconds
            headers: Optional custom headers (merged with defaults)
            use_json_headers: Use JSON API headers instead of HTML headers
            allow_redirects: Follow redirects

        Returns:
            FetchResult with content, type, status, final URL, and headers
        """
        session = await self._get_session()
        delay = self.retry_config.initial_delay

        # Build headers with Sec-Ch-Ua matching the active TLS fingerprint
        base_headers = self._JSON_HEADERS_BASE.copy() if use_json_headers else self._DEFAULT_HEADERS_BASE.copy()
        sec_ch_ua = FINGERPRINT_SEC_CH_UA.get(self._browser)
        if sec_ch_ua:
            base_headers["Sec-Ch-Ua"] = sec_ch_ua
        # Auto-set Referer to the site origin — real browsers always send this.
        # Only set if not already provided in custom headers.
        if not (headers and "Referer" in headers):
            try:
                parsed = urlparse(url)
                base_headers["Referer"] = f"{parsed.scheme}://{parsed.hostname}/"
            except Exception:
                pass
        if headers:
            base_headers.update(headers)

        last_error: Exception | None = None

        for attempt in range(self.retry_config.max_retries + 1):
            try:
                response: Response = await session.get(
                    url,
                    headers=base_headers,
                    timeout=timeout,
                    impersonate=self._browser,
                    allow_redirects=allow_redirects,
                )

                # Rate limited - use Retry-After header if present
                if response.status_code == 429:
                    if attempt < self.retry_config.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        wait_time = delay
                        if retry_after:
                            try:
                                parsed_retry = float(retry_after)
                                if 0 < parsed_retry <= 300:
                                    wait_time = parsed_retry
                            except (ValueError, TypeError):
                                pass
                        await asyncio.sleep(wait_time)
                        delay = min(delay * self.retry_config.exponential_base, self.retry_config.max_delay)
                        continue
                    return self._make_result(response)

                # Server error - retry with backoff
                if response.status_code >= 500:
                    if attempt < self.retry_config.max_retries:
                        jitter = random.uniform(-self.retry_config.jitter, self.retry_config.jitter)
                        wait_time = delay * (1 + jitter)
                        await asyncio.sleep(wait_time)
                        delay = min(delay * self.retry_config.exponential_base, self.retry_config.max_delay)
                        continue
                    return self._make_result(response)

                # Possibly blocked - rotate fingerprint and retry
                if response.status_code == 403:
                    self.rotate_fingerprint()
                    if attempt < self.retry_config.max_retries:
                        await asyncio.sleep(delay)
                        delay = min(delay * self.retry_config.exponential_base, self.retry_config.max_delay)
                        continue
                    return self._make_result(response)

                # Check Content-Length header before accessing body
                try:
                    content_length = int(response.headers.get("content-length", 0))
                except (ValueError, TypeError):
                    content_length = 0
                if content_length > self.MAX_RESPONSE_SIZE:
                    raise ValueError(
                        f"Response too large ({content_length // (1024 * 1024)}MB). "
                        f"Max allowed: {self.MAX_RESPONSE_SIZE // (1024 * 1024)}MB."
                    )

                # Still check actual body (header could lie or be missing)
                if len(response.content) > self.MAX_RESPONSE_SIZE:
                    raise ValueError(
                        f"Response too large ({len(response.content) // (1024 * 1024)}MB). "
                        f"Max allowed: {self.MAX_RESPONSE_SIZE // (1024 * 1024)}MB."
                    )

                return self._make_result(response)

            except (TimeoutError, ConnectionError, OSError) as e:
                last_error = e
                if attempt == self.retry_config.max_retries:
                    raise
                jitter = random.uniform(-self.retry_config.jitter, self.retry_config.jitter)
                wait_time = delay * (1 + jitter)
                await asyncio.sleep(wait_time)
                delay = min(delay * self.retry_config.exponential_base, self.retry_config.max_delay)

        if last_error:
            raise last_error
        raise RuntimeError(f"Failed after {self.retry_config.max_retries} retries")

    def _make_result(self, response: Response) -> FetchResult:
        """Convert curl_cffi response to FetchResult, enforcing size limit."""
        content = response.content
        if len(content) > self.MAX_RESPONSE_SIZE:
            content = content[: self.MAX_RESPONSE_SIZE]
        return FetchResult(
            content=content,
            content_type=response.headers.get("content-type", ""),
            status_code=response.status_code,
            final_url=str(response.url),
            headers=dict(response.headers),
        )

    async def apply_cookies(self, cookies: list[dict]) -> None:
        """Set cookies on the session for botfighter replay."""
        session = await self._get_session()
        for c in cookies:
            session.cookies.set(
                c.get("name", ""),
                c.get("value", ""),
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )

    async def set_cookie(self, name: str, value: str, domain: str | None = None) -> None:
        """Set a single cookie on the session."""
        session = await self._get_session()
        session.cookies.set(name, value, domain=domain)

    @property
    def current_impersonate(self) -> str:
        """Current TLS fingerprint string (public API for botfighter)."""
        return self._browser

    def pin_identity(self, impersonate: str) -> None:
        """Pin TLS fingerprint (suppress rotation)."""
        self._browser = impersonate
        self._identity_pinned = True

    def unpin_identity(self) -> None:
        """Allow normal fingerprint rotation again."""
        self._identity_pinned = False

    async def clear_cookies(self) -> None:
        """Clear all cookies from the session (call after botfighter handling)."""
        if self._session:
            self._session.cookies.clear()

    async def close(self) -> None:
        """Close the session."""
        if self._session:
            await self._session.close()
            self._session = None
