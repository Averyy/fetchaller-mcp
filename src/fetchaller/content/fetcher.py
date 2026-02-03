"""Content fetcher with curl_cffi TLS fingerprint impersonation."""

import asyncio
import random
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from curl_cffi.requests import AsyncSession, Response

from ..config import BROWSER_FINGERPRINTS, Config

if TYPE_CHECKING:
    from ..cache.response_cache import ResponseCache


def _log(msg: str) -> None:
    """Log with timestamp."""
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)


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

    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
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
    - Optional response caching
    """

    # Chrome 131 on macOS headers
    DEFAULT_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    # JSON API headers
    JSON_HEADERS = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
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
        cache: "ResponseCache | None" = None,
    ):
        self.retry_config = retry_config or RetryConfig()
        self.cache = cache
        self._browser = random.choice(BROWSER_FINGERPRINTS)
        self._session: AsyncSession | None = None

    async def _get_session(self) -> AsyncSession:
        """Get or create async session."""
        if self._session is None:
            start = time.time()
            _log("FETCH: Creating new AsyncSession...")
            self._session = AsyncSession()
            elapsed = (time.time() - start) * 1000
            _log(f"FETCH: AsyncSession created in {elapsed:.1f}ms")
        return self._session

    def rotate_fingerprint(self) -> None:
        """Rotate to a different browser fingerprint (call after 403/block)."""
        old = self._browser
        # Pick a different fingerprint
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
        fetch_start = time.time()
        _log(f"FETCH: Starting fetch url={url} timeout={timeout}s browser={self._browser}")

        session = await self._get_session()
        delay = self.retry_config.initial_delay

        # Build headers
        base_headers = self.JSON_HEADERS.copy() if use_json_headers else self.DEFAULT_HEADERS.copy()
        if headers:
            base_headers.update(headers)

        last_error: Exception | None = None

        for attempt in range(self.retry_config.max_retries + 1):
            attempt_start = time.time()
            _log(f"FETCH: Attempt {attempt + 1}/{self.retry_config.max_retries + 1} for {url}")

            try:
                response: Response = await session.get(
                    url,
                    headers=base_headers,
                    timeout=timeout,
                    impersonate=self._browser,
                    allow_redirects=allow_redirects,
                )
                attempt_elapsed = (time.time() - attempt_start) * 1000
                _log(f"FETCH: Got response status={response.status_code} size={len(response.content)} in {attempt_elapsed:.1f}ms url={url}")

                # Rate limited - use Retry-After header if present
                if response.status_code == 429:
                    if attempt < self.retry_config.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        wait_time = delay
                        if retry_after:
                            try:
                                # Validate and clamp Retry-After to reasonable range (1-300 seconds)
                                parsed_retry = float(retry_after)
                                if 0 < parsed_retry <= 300:
                                    wait_time = parsed_retry
                            except (ValueError, TypeError):
                                pass  # Use default delay
                        _log(f"FETCH: Rate limited (429), waiting {wait_time:.1f}s before retry")
                        await asyncio.sleep(wait_time)
                        delay = min(delay * self.retry_config.exponential_base, self.retry_config.max_delay)
                        continue
                    # Return the 429 response on last attempt
                    total_elapsed = (time.time() - fetch_start) * 1000
                    _log(f"FETCH: Returning 429 after all retries, total time={total_elapsed:.1f}ms")
                    return self._make_result(response)

                # Server error - retry with backoff
                if response.status_code >= 500:
                    if attempt < self.retry_config.max_retries:
                        jitter = random.uniform(-self.retry_config.jitter, self.retry_config.jitter)
                        wait_time = delay * (1 + jitter)
                        _log(f"FETCH: Server error ({response.status_code}), waiting {wait_time:.1f}s before retry")
                        await asyncio.sleep(wait_time)
                        delay = min(delay * self.retry_config.exponential_base, self.retry_config.max_delay)
                        continue
                    total_elapsed = (time.time() - fetch_start) * 1000
                    _log(f"FETCH: Returning {response.status_code} after all retries, total time={total_elapsed:.1f}ms")
                    return self._make_result(response)

                # Possibly blocked - rotate fingerprint and retry
                if response.status_code == 403:
                    old_browser = self._browser
                    self.rotate_fingerprint()
                    _log(f"FETCH: Got 403, rotating fingerprint {old_browser} -> {self._browser}")
                    if attempt < self.retry_config.max_retries:
                        _log(f"FETCH: Waiting {delay:.1f}s before retry")
                        await asyncio.sleep(delay)
                        delay = min(delay * self.retry_config.exponential_base, self.retry_config.max_delay)
                        continue
                    total_elapsed = (time.time() - fetch_start) * 1000
                    _log(f"FETCH: Returning 403 after all retries, total time={total_elapsed:.1f}ms")
                    return self._make_result(response)

                total_elapsed = (time.time() - fetch_start) * 1000
                _log(f"FETCH: Success status={response.status_code} total time={total_elapsed:.1f}ms url={url}")
                return self._make_result(response)

            except (TimeoutError, ConnectionError, OSError) as e:
                attempt_elapsed = (time.time() - attempt_start) * 1000
                _log(f"FETCH: Exception on attempt {attempt + 1}: {type(e).__name__}: {e} after {attempt_elapsed:.1f}ms")
                last_error = e
                if attempt == self.retry_config.max_retries:
                    total_elapsed = (time.time() - fetch_start) * 1000
                    _log(f"FETCH: Raising exception after all retries, total time={total_elapsed:.1f}ms")
                    raise
                jitter = random.uniform(-self.retry_config.jitter, self.retry_config.jitter)
                wait_time = delay * (1 + jitter)
                _log(f"FETCH: Waiting {wait_time:.1f}s before retry")
                await asyncio.sleep(wait_time)
                delay = min(delay * self.retry_config.exponential_base, self.retry_config.max_delay)

        # Should not reach here, but just in case
        if last_error:
            raise last_error
        raise RuntimeError(f"Failed after {self.retry_config.max_retries} retries")

    def _make_result(self, response: Response) -> FetchResult:
        """Convert curl_cffi response to FetchResult."""
        return FetchResult(
            content=response.content,
            content_type=response.headers.get("content-type", ""),
            status_code=response.status_code,
            final_url=str(response.url),
            headers=dict(response.headers),
        )

    async def close(self) -> None:
        """Close the session."""
        if self._session:
            await self._session.close()
            self._session = None
