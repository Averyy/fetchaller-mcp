"""Pytest fixtures for fetchaller tests."""

import pytest

from fetchaller.ratelimit import (
    alibaba_limiter,
    aliexpress_limiter,
    craigslist_limiter,
    digikey_limiter,
    facebook_limiter,
    kijiji_limiter,
    mouser_limiter,
    reddit_limiter,
    soylent_limiter,
)

# All domain rate limiters — zeroed out for tests (no real HTTP requests)
_ALL_LIMITERS = [
    alibaba_limiter,
    aliexpress_limiter,
    craigslist_limiter,
    digikey_limiter,
    facebook_limiter,
    kijiji_limiter,
    mouser_limiter,
    reddit_limiter,
    soylent_limiter,
]


@pytest.fixture(autouse=True)
def _zero_rate_limiters():
    """Zero out all domain rate limiters so mocked tests don't sleep."""
    saved = []
    for limiter in _ALL_LIMITERS:
        saved.append((limiter._min_interval, limiter._jitter_min, limiter._jitter_max, limiter._last_time))
        limiter._min_interval = 0.0
        limiter._jitter_min = 0.0
        limiter._jitter_max = 0.0
        limiter._last_time = 0.0
    yield
    for limiter, (interval, jmin, jmax, last) in zip(_ALL_LIMITERS, saved):
        limiter._min_interval = interval
        limiter._jitter_min = jmin
        limiter._jitter_max = jmax
        limiter._last_time = last


@pytest.fixture
def sample_html():
    """Sample HTML for testing."""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Test Page</title></head>
    <body>
        <nav>Navigation</nav>
        <main>
            <h1>Hello World</h1>
            <p>This is a test paragraph.</p>
        </main>
        <footer>Footer</footer>
        <script>alert('xss')</script>
    </body>
    </html>
    """
