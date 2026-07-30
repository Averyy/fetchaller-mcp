"""Least-privilege user-context OAuth for exact account-gated Reddit reads."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

import wafer

from ..config import Config
from ..content.reddit import normalize_moderator_roster_children
from ..queue.reddit_queue import RedditRequestQueue, parse_retry_after
from ..ratelimit import reddit_limiter

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_OAUTH_ORIGIN = "https://oauth.reddit.com"
_SUBREDDIT_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_]{0,20}\Z")
_MAX_TOKEN_RESPONSE = 64 * 1024
_MAX_JSON_RESPONSE = 50 * 1024 * 1024
_MAX_ROSTER_RESPONSE = 2 * 1024 * 1024
_MAX_WIKI_PAGES_RESPONSE = 2 * 1024 * 1024
_MAX_BEARER_LENGTH = 8192
_TOKEN_EXPIRY_CAP_SECONDS = 24 * 60 * 60
_MAX_ROSTER_PAGES = 20
_PAGINATION_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class _OAuthDeadlineExceededError(Exception):
    """Internal control flow for one exhausted overall request deadline."""


@dataclass(frozen=True, slots=True)
class _RequestFailure:
    """Sanitized network failure returned through the queue without logging secrets."""

    timed_out: bool = False


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _OAuthDeadlineExceededError
    return remaining


async def _within_reddit_budget[T](
    callback: Callable[[], Awaitable[T]],
    *,
    queue: RedditRequestQueue | None,
    deadline: float,
) -> T:
    """Run one Reddit request inside the shared rate and total-time budget."""
    remaining = _remaining(deadline)
    if queue is not None:
        try:
            return await queue.enqueue(callback, _queue_timeout=remaining)
        except TimeoutError:
            raise _OAuthDeadlineExceededError from None
    try:
        await asyncio.wait_for(reddit_limiter.wait(), timeout=remaining)
    except TimeoutError:
        raise _OAuthDeadlineExceededError from None
    return await callback()


def _retry_after(headers: object) -> float | None:
    if not hasattr(headers, "get"):
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    return parse_retry_after(value)


def _apply_rate_limit(
    *,
    queue: RedditRequestQueue | None,
    retry_after: float | None,
) -> float:
    applied_delay = 60.0 if retry_after is None else retry_after
    if queue is not None:
        queue.set_backoff(429, retry_after=retry_after)
    else:
        reddit_limiter.defer(applied_delay)
    return applied_delay


def _valid_bearer_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_BEARER_LENGTH
        and value.isascii()
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _response_matches_exact_url(response: object, expected_url: str) -> bool:
    """Reject every redirect before trusting an OAuth response."""

    history = getattr(response, "history", ())
    if history:
        return False
    return str(getattr(response, "url", expected_url)) == expected_url


def _valid_oauth_read_url(url: str) -> bool:
    """Accept only a bounded exact URL on Reddit's OAuth API origin."""

    if (
        not isinstance(url, str)
        or len(url) > 8192
        or not url.isascii()
        or any(
            ord(character) <= 0x20
            or ord(character) == 0x7F
            or character == "\\"
            for character in url
        )
    ):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").rstrip(".").lower() == "oauth.reddit.com"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.params
        and not parsed.fragment
        and parsed.path.startswith("/")
    )


def valid_moderator_roster(value: object) -> bool:
    """Require the UserList shape before handing it to the renderer."""
    if (
        not isinstance(value, dict)
        or value.get("error")
        or value.get("kind") != "UserList"
    ):
        return False
    return normalize_moderator_roster_children(value, allow_wrapped=False) is not None


@dataclass(slots=True)
class RedditModeratorOAuth:
    """In-memory access-token lifecycle for exact account-gated parity routes."""

    client_id: str | None = field(default=None, repr=False)
    client_secret: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    user_agent: str = "fetchaller-mcp/3 exact-reddit-reads"
    _expires_at: float = field(default=math.inf, init=False, repr=False)
    _refresh_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _session: wafer.AsyncSession | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _session_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Keep direct construction behind the same credential validation."""
        Config(
            reddit_client_id=self.client_id,
            reddit_client_secret=self.client_secret,
            reddit_refresh_token=self.refresh_token,
            reddit_access_token=self.access_token,
            reddit_user_agent=self.user_agent,
        )

    @property
    def can_refresh(self) -> bool:
        return (
            self.client_id is not None
            and self.client_secret is not None
            and self.refresh_token is not None
        )

    def _usable_token(self) -> str | None:
        if self.access_token is not None and time.monotonic() < self._expires_at:
            return self.access_token
        return None

    async def get_session(self) -> wafer.AsyncSession:
        """Return the cookie-isolated application API session."""

        if self._session is None:
            async with self._session_lock:
                if self._session is None:
                    self._session = wafer.AsyncSession(
                        profile=wafer.Profile.DART,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": self.user_agent,
                        },
                        max_rotations=0,
                        follow_redirects=False,
                        max_response_size=_MAX_JSON_RESPONSE,
                    )
        return self._session

    async def fetch_json(
        self,
        url: str,
        queue: RedditRequestQueue | None,
        timeout: float,
    ) -> dict[str, Any]:
        """Fetch one prevalidated public read through Reddit's OAuth origin."""

        if not _valid_oauth_read_url(url):
            return {"error": "Invalid Reddit OAuth read URL."}

        deadline = time.monotonic() + max(0.0, timeout)
        session = await self.get_session()
        token_result = await self._get_access_token(session, queue, deadline)
        if "error" in token_result:
            return token_result

        token = token_result["token"]
        for attempt in range(2):

            async def request_json():
                try:
                    return await session.get(
                        url,
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {token}",
                            "User-Agent": self.user_agent,
                        },
                        timeout=_remaining(deadline),
                        max_response_size=_MAX_JSON_RESPONSE,
                    )
                except (
                    wafer.WaferTimeout,
                    TimeoutError,
                    _OAuthDeadlineExceededError,
                ):
                    return _RequestFailure(timed_out=True)
                except Exception:
                    return _RequestFailure()

            try:
                response = await _within_reddit_budget(
                    request_json,
                    queue=queue,
                    deadline=deadline,
                )
            except _OAuthDeadlineExceededError:
                return {"error": "Reddit API request timed out."}
            except (wafer.WaferTimeout, TimeoutError):
                return {"error": "Reddit API request timed out."}
            except Exception:
                return {"error": "Reddit API request failed."}

            if isinstance(response, _RequestFailure):
                return {
                    "error": (
                        "Reddit API request timed out."
                        if response.timed_out
                        else "Reddit API request failed."
                    )
                }
            if not _response_matches_exact_url(response, url):
                return {
                    "error": "Reddit API request left its exact endpoint."
                }
            if (
                response.status_code == 401
                and attempt == 0
                and self.can_refresh
            ):
                refreshed = await self._get_access_token(
                    session,
                    queue,
                    deadline,
                    rejected_token=token,
                )
                if "error" in refreshed:
                    return refreshed
                token = refreshed["token"]
                continue
            return {"response": response}

        return {"error": "Reddit API access token was rejected or expired."}

    async def _refresh_access_token(
        self,
        session: wafer.AsyncSession,
        queue: RedditRequestQueue | None,
        deadline: float,
    ) -> dict[str, str]:
        if not self.can_refresh:
            if self.access_token is not None:
                return {
                    "error": (
                        "Reddit OAuth access token was rejected or expired."
                    )
                }
            return {"error": "Reddit user-context OAuth is not configured."}

        assert self.client_id is not None
        assert self.client_secret is not None
        assert self.refresh_token is not None
        basic_value = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("ascii")
        ).decode("ascii")

        async def request_token():
            try:
                return await session.post(
                    _TOKEN_URL,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Basic {basic_value}",
                        "User-Agent": self.user_agent,
                    },
                    form={
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                    },
                    timeout=_remaining(deadline),
                    max_response_size=_MAX_TOKEN_RESPONSE,
                )
            except (wafer.WaferTimeout, TimeoutError, _OAuthDeadlineExceededError):
                return _RequestFailure(timed_out=True)
            except Exception:
                return _RequestFailure()

        try:
            response = await _within_reddit_budget(
                request_token,
                queue=queue,
                deadline=deadline,
            )
        except _OAuthDeadlineExceededError:
            return {"error": "Reddit OAuth authentication timed out."}
        except (wafer.WaferTimeout, TimeoutError):
            return {"error": "Reddit OAuth authentication timed out."}
        except Exception:
            return {"error": "Reddit OAuth authentication failed."}

        if isinstance(response, _RequestFailure):
            return {
                "error": (
                    "Reddit OAuth authentication timed out."
                    if response.timed_out
                    else "Reddit OAuth authentication failed."
                )
            }
        if not _response_matches_exact_url(response, _TOKEN_URL):
            return {
                "error": (
                    "Reddit OAuth authentication left its exact endpoint."
                )
            }
        if response.status_code == 429:
            delay = _apply_rate_limit(
                queue=queue,
                retry_after=_retry_after(response.headers),
            )
            return {
                "error": (
                    "Reddit OAuth authentication was rate limited. "
                    f"Retry after {delay:g}s."
                )
            }
        if response.status_code in {400, 401, 403}:
            return {"error": "Reddit OAuth refresh credentials were rejected."}
        if response.status_code != 200:
            return {
                "error": (
                    "Reddit OAuth authentication is temporarily unavailable "
                    f"(HTTP {response.status_code})."
                )
            }

        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            return {"error": "Reddit OAuth refresh credentials were rejected."}
        if not isinstance(payload, dict):
            return {"error": "Reddit OAuth authentication returned an invalid response."}

        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if (
            not _valid_bearer_token(token)
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not math.isfinite(float(expires_in))
            or float(expires_in) <= 0
        ):
            return {"error": "Reddit OAuth authentication returned an invalid response."}

        lifetime = min(float(expires_in), _TOKEN_EXPIRY_CAP_SECONDS)
        refresh_margin = min(60.0, lifetime / 10)
        self.access_token = token
        self._expires_at = time.monotonic() + max(
            0.0,
            lifetime - refresh_margin,
        )
        return {"token": token}

    async def _get_access_token(
        self,
        session: wafer.AsyncSession,
        queue: RedditRequestQueue | None,
        deadline: float,
        *,
        rejected_token: str | None = None,
    ) -> dict[str, str]:
        token = self._usable_token()
        if token is not None and (rejected_token is None or token != rejected_token):
            return {"token": token}

        acquired = False
        try:
            await asyncio.wait_for(
                self._refresh_lock.acquire(),
                timeout=_remaining(deadline),
            )
            acquired = True
        except (TimeoutError, _OAuthDeadlineExceededError):
            return {"error": "Reddit OAuth authentication timed out."}
        try:
            token = self._usable_token()
            if token is not None and (rejected_token is None or token != rejected_token):
                return {"token": token}
            if rejected_token is not None and self.access_token == rejected_token:
                self._expires_at = 0.0
            return await self._refresh_access_token(session, queue, deadline)
        finally:
            if acquired:
                self._refresh_lock.release()

    async def fetch_moderators(
        self,
        subreddit: str,
        session: wafer.AsyncSession | None,
        queue: RedditRequestQueue | None,
        timeout: float,
    ) -> dict[str, Any]:
        """Fetch the exact moderator roster from Reddit's fixed OAuth origin."""
        if not _SUBREDDIT_PATTERN.fullmatch(subreddit):
            return {"error": "Invalid subreddit name"}

        session = session or await self.get_session()
        deadline = time.monotonic() + max(0.0, timeout)
        token_result = await self._get_access_token(session, queue, deadline)
        if "error" in token_result:
            return token_result

        token = token_result["token"]
        auth_refresh_used = False
        after: str | None = None
        seen_cursors: set[str] = set()
        all_children: list[object] = []
        first_payload: dict[str, Any] | None = None

        for _page_number in range(_MAX_ROSTER_PAGES):
            params = {"limit": "500", "raw_json": "1"}
            if after is not None:
                params["after"] = after
            roster_url = (
                f"{_OAUTH_ORIGIN}/r/{subreddit}/about/moderators?"
                + urlencode(params)
            )

            for _attempt in range(2):
                async def request_roster():
                    try:
                        return await session.get(
                            roster_url,
                            headers={
                                "Accept": "application/json",
                                "Authorization": f"Bearer {token}",
                                "User-Agent": self.user_agent,
                            },
                            timeout=_remaining(deadline),
                            max_response_size=_MAX_ROSTER_RESPONSE,
                        )
                    except (
                        wafer.WaferTimeout,
                        TimeoutError,
                        _OAuthDeadlineExceededError,
                    ):
                        return _RequestFailure(timed_out=True)
                    except Exception:
                        return _RequestFailure()

                try:
                    response = await _within_reddit_budget(
                        request_roster,
                        queue=queue,
                        deadline=deadline,
                    )
                except _OAuthDeadlineExceededError:
                    return {"error": "Reddit moderator roster request timed out."}
                except (wafer.WaferTimeout, TimeoutError):
                    return {"error": "Reddit moderator roster request timed out."}
                except Exception:
                    return {"error": "Reddit moderator roster request failed."}

                if isinstance(response, _RequestFailure):
                    return {
                        "error": (
                            "Reddit moderator roster request timed out."
                            if response.timed_out
                            else "Reddit moderator roster request failed."
                        )
                    }
                if not _response_matches_exact_url(response, roster_url):
                    return {
                        "error": (
                            "Reddit moderator roster request left its exact "
                            "endpoint."
                        )
                    }
                if (
                    response.status_code == 401
                    and not auth_refresh_used
                    and self.can_refresh
                ):
                    auth_refresh_used = True
                    refreshed = await self._get_access_token(
                        session,
                        queue,
                        deadline,
                        rejected_token=token,
                    )
                    if "error" in refreshed:
                        return refreshed
                    token = refreshed["token"]
                    continue
                break

            if response.status_code == 401:
                if self.access_token == token:
                    self._expires_at = 0.0
                return {
                    "error": (
                        "Reddit moderator access token was rejected or expired."
                    )
                }
            if response.status_code == 403:
                return {
                    "error": (
                        "Reddit moderator access was forbidden; verify the "
                        "account and OAuth read scope."
                    )
                }
            if response.status_code == 404:
                return {"error": "Reddit moderator roster was not found."}
            if response.status_code == 429:
                delay = _apply_rate_limit(
                    queue=queue,
                    retry_after=_retry_after(response.headers),
                )
                return {
                    "error": (
                        "Reddit moderator roster was rate limited. "
                        f"Retry after {delay:g}s."
                    )
                }
            if response.status_code != 200:
                return {
                    "error": (
                        "Reddit moderator roster is temporarily unavailable "
                        f"(HTTP {response.status_code})."
                    )
                }

            try:
                payload = response.json()
            except Exception:
                payload = None
            if not valid_moderator_roster(payload):
                return {"error": "Reddit moderator roster returned an invalid response."}

            assert isinstance(payload, dict)
            data = payload["data"]
            assert isinstance(data, dict)
            children = data["children"]
            assert isinstance(children, list)
            if first_payload is None:
                first_payload = payload
            all_children.extend(children)

            next_cursor = data.get("after")
            if next_cursor is None:
                if not seen_cursors:
                    return {"data": payload}
                merged_payload = dict(first_payload)
                merged_data = dict(first_payload["data"])
                merged_data["children"] = all_children
                merged_data["after"] = None
                merged_payload["data"] = merged_data
                return {"data": merged_payload}
            if (
                not isinstance(next_cursor, str)
                or not _PAGINATION_CURSOR.fullmatch(next_cursor)
                or next_cursor in seen_cursors
            ):
                return {
                    "error": (
                        "Reddit moderator roster returned an invalid pagination cursor."
                    )
                }
            seen_cursors.add(next_cursor)
            after = next_cursor

        return {
            "error": (
                "Reddit moderator roster exceeded the bounded pagination limit."
            )
        }

    async def fetch_wiki_pages(
        self,
        subreddit: str,
        session: wafer.AsyncSession | None,
        queue: RedditRequestQueue | None,
        timeout: float,
    ) -> dict[str, Any]:
        """Fetch the exact wiki page index from Reddit's fixed OAuth origin."""

        if not _SUBREDDIT_PATTERN.fullmatch(subreddit):
            return {"error": "Invalid subreddit name"}

        session = session or await self.get_session()
        deadline = time.monotonic() + max(0.0, timeout)
        token_result = await self._get_access_token(session, queue, deadline)
        if "error" in token_result:
            return token_result

        token = token_result["token"]
        auth_refresh_used = False
        wiki_url = (
            f"{_OAUTH_ORIGIN}/r/{subreddit}/wiki/pages/?"
            + urlencode({"raw_json": "1"})
        )
        for _attempt in range(2):

            async def request_pages():
                try:
                    return await session.get(
                        wiki_url,
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {token}",
                            "User-Agent": self.user_agent,
                        },
                        timeout=_remaining(deadline),
                        max_response_size=_MAX_WIKI_PAGES_RESPONSE,
                    )
                except (
                    wafer.WaferTimeout,
                    TimeoutError,
                    _OAuthDeadlineExceededError,
                ):
                    return _RequestFailure(timed_out=True)
                except Exception:
                    return _RequestFailure()

            try:
                response = await _within_reddit_budget(
                    request_pages,
                    queue=queue,
                    deadline=deadline,
                )
            except _OAuthDeadlineExceededError:
                return {"error": "Reddit wiki page index request timed out."}
            except (wafer.WaferTimeout, TimeoutError):
                return {"error": "Reddit wiki page index request timed out."}
            except Exception:
                return {"error": "Reddit wiki page index request failed."}

            if isinstance(response, _RequestFailure):
                return {
                    "error": (
                        "Reddit wiki page index request timed out."
                        if response.timed_out
                        else "Reddit wiki page index request failed."
                    )
                }
            if not _response_matches_exact_url(response, wiki_url):
                return {
                    "error": (
                        "Reddit wiki page index request left its exact "
                        "endpoint."
                    )
                }
            if (
                response.status_code == 401
                and not auth_refresh_used
                and self.can_refresh
            ):
                auth_refresh_used = True
                refreshed = await self._get_access_token(
                    session,
                    queue,
                    deadline,
                    rejected_token=token,
                )
                if "error" in refreshed:
                    return refreshed
                token = refreshed["token"]
                continue
            break

        if response.status_code == 401:
            if self.access_token == token:
                self._expires_at = 0.0
            return {
                "error": (
                    "Reddit wiki page index access token was rejected or "
                    "expired."
                )
            }
        if response.status_code == 403:
            return {
                "error": (
                    "Reddit wiki page index access was forbidden; verify the "
                    "account and OAuth wikiread scope."
                )
            }
        if response.status_code == 404:
            return {"error": "Reddit wiki page index was not found."}
        if response.status_code == 429:
            delay = _apply_rate_limit(
                queue=queue,
                retry_after=_retry_after(response.headers),
            )
            return {
                "error": (
                    "Reddit wiki page index was rate limited. "
                    f"Retry after {delay:g}s."
                )
            }
        if response.status_code != 200:
            return {
                "error": (
                    "Reddit wiki page index is temporarily unavailable "
                    f"(HTTP {response.status_code})."
                )
            }

        try:
            payload = response.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return {
                "error": (
                    "Reddit wiki page index returned an invalid response."
                )
            }
        return {"data": payload}


_manager: RedditModeratorOAuth | None = None
_manager_fingerprint: bytes | None = None


def _credential_fingerprint(config: Config) -> bytes:
    digest = hashlib.sha256()
    for value in (
        config.reddit_client_id,
        config.reddit_client_secret,
        config.reddit_refresh_token,
        config.reddit_access_token,
        config.reddit_user_agent,
    ):
        encoded = (value or "").encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.digest()


def get_reddit_moderator_oauth(config: Config) -> RedditModeratorOAuth | None:
    """Return a process-local manager without retaining raw credentials as its key."""
    global _manager, _manager_fingerprint
    if not config.reddit_moderator_oauth_configured:
        return None
    fingerprint = _credential_fingerprint(config)
    if _manager is None or _manager_fingerprint != fingerprint:
        _manager = RedditModeratorOAuth(
            client_id=config.reddit_client_id,
            client_secret=config.reddit_client_secret,
            refresh_token=config.reddit_refresh_token,
            access_token=config.reddit_access_token,
            user_agent=config.reddit_user_agent,
        )
        _manager_fingerprint = fingerprint
    return _manager


def reset_reddit_moderator_oauth() -> None:
    """Forget cached OAuth state (tests and deliberate credential rotation)."""
    global _manager, _manager_fingerprint
    _manager = None
    _manager_fingerprint = None
