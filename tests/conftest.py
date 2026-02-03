"""Pytest fixtures for fetchaller tests."""

import pytest


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
