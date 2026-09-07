"""Turn a transport failure into something a caller can act on.

wafer surfaces connection failures as the underlying Rust error's ``Debug``
output. For a TLS problem that is a ~700-character dump in which the one word
that matters — ``CERTIFICATE_VERIFY_FAILED`` — sits 300 characters in, wrapped
in nested struct literals, numeric error codes and the absolute path of the
machine the wheel was built on:

    Connection error: is_connect error: wreq::Error { kind: Request, uri: ...,
    source: Error { kind: Connect, source: Some(Error { code: SSL (1), cause:
    Some(Ssl(ErrorStack([Error { code: 268435581, library: "SSL routines", ...
    file: "/Users/runner/work/wreq-python/wreq-python/target/..."

Two things are wrong with that. It leaks build-machine paths into a
user-facing string, and it does not answer the only question the caller has:
is this site broken, or did our client fail to build a chain that a browser
builds fine? Answering it required dropping to ``curl`` to compare.

Nothing here retries or fixes the connection — that is transport, and belongs to
wafer. This only names the failure.
"""

from __future__ import annotations

import re

from ..security.xss import redact_secrets_for_log

# Ordered: the first match wins, so specific causes precede general ones.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"HostnameMismatch|NotValidForName|certificate.*(?:name|hostname)", re.I),
        "TLS certificate does not cover this hostname. The certificate the "
        "server presented is issued for a different name, so this is the "
        "site's own misconfiguration rather than a fetch problem.",
    ),
    (
        re.compile(r"CertExpired|certificate has expired|NotValidYet", re.I),
        "TLS certificate is expired or not yet valid. This is the site's own "
        "misconfiguration.",
    ),
    (
        re.compile(
            r"unable to get local issuer|unable to verify the first certificate|"
            r"UNABLE_TO_GET_ISSUER_CERT(?:_LOCALLY)?",
            re.I,
        ),
        "TLS certificate chain could not be verified because its issuer could "
        "not be found. The server may be serving its leaf certificate without "
        "the intermediate. Verify with: "
        "`openssl s_client -connect <host>:443 -servername <host>` and inspect "
        "the certificates and verification errors returned.",
    ),
    (
        re.compile(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed", re.I),
        "TLS certificate verification failed. The reason supplied does not "
        "distinguish an expired, mismatched, untrusted, self-signed, or incomplete "
        "chain, so the site configuration must be inspected before assigning a "
        "cause.",
    ),
    (
        re.compile(r"dns error|failed to lookup|NameResolution|nodename nor servname", re.I),
        "DNS lookup failed — the hostname does not resolve. Check the spelling, "
        "and whether the host is internal-only.",
    ),
    (
        re.compile(r"connection refused|ECONNREFUSED", re.I),
        "Connection refused — nothing is listening on that port.",
    ),
    (
        re.compile(r"connection reset|ECONNRESET|reset by peer", re.I),
        "Connection reset by the server mid-handshake. Often an edge appliance "
        "rejecting the client rather than an application error.",
    ),
    (
        re.compile(r"timed out|ETIMEDOUT", re.I),
        "Connection timed out before the server answered.",
    ),
)

# Fallback trimming: keep the first clause, drop the struct dump behind it.
_NOISE_RE = re.compile(r"\s*(?:source|cause|file|code|library|reason_code)\s*[:=]", re.I)
_LOCAL_PATH_RE = re.compile(
    r"(?<![:/])/(?:[^/\s,:{}\[\]()\"']+/)+[^/\s,:{}\[\]()\"']+"
    r"|\b[A-Za-z]:\\(?:[^\\\s,:{}\[\]()\"']+\\)+[^\\\s,:{}\[\]()\"']+"
)
_MAX_RAW_CHARS = 200


def describe_connection_failure(reason: str) -> str:
    """A caller-facing sentence for a transport failure.

    Falls back to the raw text, trimmed at the first struct field and bounded,
    so an unrecognised failure still says something without pasting a debug
    dump — or a build path — into the answer.
    """
    text = " ".join((reason or "").split())
    if not text:
        return "Connection error: the request failed before a response arrived."

    for pattern, explanation in _PATTERNS:
        if pattern.search(text):
            return f"Connection error: {explanation}"

    head = _NOISE_RE.split(text, maxsplit=1)[0].strip(" {,")
    if not head:
        head = text
    head = redact_secrets_for_log(head)
    head = _LOCAL_PATH_RE.sub("[redacted-path]", head)
    if len(head) > _MAX_RAW_CHARS:
        head = head[: _MAX_RAW_CHARS - 1].rstrip() + "…"
    return f"Connection error: {head}"
