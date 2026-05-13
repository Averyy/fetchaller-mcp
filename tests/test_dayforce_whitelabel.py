"""Tests for Dayforce white-label detection via __NEXT_DATA__.

Some companies host the Dayforce Next.js candidate portal on their own
domain (e.g. www.synaptivemedical.com/job-openings). The SSR'd
__NEXT_DATA__ carries enough info to rewrite to the canonical
jobs.dayforcehcm.com board URL.
"""

import json

from fetchaller.content import dayforce


def _wrap(next_data: dict) -> str:
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'


def test_detects_white_label_dayforce():
    next_data = {
        "props": {"pageProps": {}},
        "page": "/[clientNamespace]/[careerSiteXRefCode]",
        "query": {"clientNamespace": "pp4h663", "careerSiteXRefCode": "CANDIDATEPORTAL"},
        "runtimeConfig": {"BASE_URL": "https://jobs.dayforcehcm.com/"},
        "locale": "en-US",
    }
    assert dayforce.extract_dayforce_canonical_board_url(_wrap(next_data)) == (
        "https://jobs.dayforcehcm.com/en-US/pp4h663/CANDIDATEPORTAL"
    )


def test_rejects_non_dayforce_next_data():
    next_data = {
        "props": {"pageProps": {}},
        "query": {"clientNamespace": "x", "careerSiteXRefCode": "Y"},
        "runtimeConfig": {"BASE_URL": "https://other.com/"},
        "locale": "en-US",
    }
    assert dayforce.extract_dayforce_canonical_board_url(_wrap(next_data)) is None


def test_rejects_missing_query_keys():
    next_data = {
        "props": {"pageProps": {}},
        "query": {"clientNamespace": "x"},  # no careerSiteXRefCode
        "runtimeConfig": {"BASE_URL": "https://jobs.dayforcehcm.com/"},
        "locale": "en-US",
    }
    assert dayforce.extract_dayforce_canonical_board_url(_wrap(next_data)) is None


def test_rejects_pages_with_no_next_data():
    assert dayforce.extract_dayforce_canonical_board_url("<html><body>nothing</body></html>") is None


def test_defaults_locale_when_missing():
    next_data = {
        "query": {"clientNamespace": "ns", "careerSiteXRefCode": "BOARD"},
        "runtimeConfig": {"BASE_URL": "https://jobs.dayforcehcm.com/"},
    }
    assert dayforce.extract_dayforce_canonical_board_url(_wrap(next_data)) == (
        "https://jobs.dayforcehcm.com/en-US/ns/BOARD"
    )
