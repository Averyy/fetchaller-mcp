"""Sensitive diagnostics must remain bounded and credential-free."""

from fetchaller.marketplace.search import _log as marketplace_log
from fetchaller.security.xss import safe_log_text


def test_safe_log_text_redacts_url_credentials_paths_and_queries():
    value = (
        "failed https://user:password@example.com/private/listing"
        "?api_key=secret-value&query=home-address"
    )

    safe = safe_log_text(value)

    assert "user" not in safe
    assert "password" not in safe
    assert "private" not in safe
    assert "secret-value" not in safe
    assert "home-address" not in safe
    assert safe == "failed https://example.com/…"


def test_downstream_logger_is_single_line_bounded_and_redacted(capsys):
    marketplace_log(
        "failure\nhttps://example.com/private?"
        "token=secret-value&location=home-address"
    )

    logged = capsys.readouterr().err

    assert logged.count("\n") == 1
    assert "private" not in logged
    assert "secret-value" not in logged
    assert "home-address" not in logged
