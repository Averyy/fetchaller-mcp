"""Timeouts are reported as the one that fired.

``wafer.WaferTimeout`` subclasses ``TimeoutError``, so a single request that ran
past its session's limit used to be reported as the tool's whole budget running
out ("timed out after 90s" at 62.6s).
"""

import asyncio

import wafer

from fetchaller.oracle_recruiting import api as oracle_api
from fetchaller.oracle_recruiting import search as oracle_search
from fetchaller.timeouts import timeout_error


def test_budget_timeout_names_the_budget():
    assert timeout_error("Oracle Recruiting search", 90, TimeoutError()) == (
        "Oracle Recruiting search timed out after 90s."
    )


def test_request_timeout_names_the_request_not_the_budget():
    exc = wafer.WaferTimeout("https://eeho.fa.us2.oraclecloud.com/x", 60.0)
    message = timeout_error("Oracle Recruiting search", 90, exc)
    assert message == (
        "Oracle Recruiting search failed: one request got no answer within 60s "
        "(the 90s budget had not run out). Try again shortly."
    )


async def test_a_hung_request_inside_a_search_is_reported_as_one_request(monkeypatch):
    async def _session(browser_solver=None):
        return object()

    async def _host(employer, session):
        return "https://eeho.fa.us2.oraclecloud.com"

    async def _hung(*args, **kwargs):
        raise wafer.WaferTimeout("https://eeho.fa.us2.oraclecloud.com/search", 60.0)

    monkeypatch.setattr(oracle_api, "_get_session", _session)
    monkeypatch.setattr(oracle_search, "_resolve_host", _host)
    monkeypatch.setattr(oracle_api, "search_all_requisitions", _hung)

    result = await oracle_search.search_oracle_jobs("oracle", title="software engineer", timeout=90)

    assert "one request got no answer within 60s" in result["error"]
    assert "timed out after 90s" not in result["error"]


async def test_the_budget_itself_running_out_still_says_so(monkeypatch):
    async def _session(browser_solver=None):
        return object()

    async def _host(employer, session):
        return "https://eeho.fa.us2.oraclecloud.com"

    async def _slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(oracle_api, "_get_session", _session)
    monkeypatch.setattr(oracle_search, "_resolve_host", _host)
    monkeypatch.setattr(oracle_api, "search_all_requisitions", _slow)

    result = await oracle_search.search_oracle_jobs("oracle", title="software engineer", timeout=0.05)

    assert result["error"] == "Oracle Recruiting search timed out after 0s."
