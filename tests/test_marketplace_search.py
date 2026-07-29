"""Tests for unified marketplace search."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from fetchaller.craigslist.locations import (
    is_canadian_location,
    resolve_hostname,
)
from fetchaller.kijiji.locations import resolve_location
from fetchaller.marketplace.aliases import (
    CATEGORY_MAP,
    CONDITION_MAP,
    SORT_MAP,
    resolve_alias,
)
from fetchaller.marketplace.search import search_marketplace

# ---------------------------------------------------------------------------
# Craigslist location resolution
# ---------------------------------------------------------------------------


class TestCraigslistLocations:
    def test_exact_match_canadian(self):
        assert resolve_hostname("toronto") == "toronto.craigslist.org"
        assert resolve_hostname("vancouver") == "vancouver.craigslist.org"
        assert resolve_hostname("montreal") == "montreal.craigslist.org"

    def test_exact_match_us(self):
        assert resolve_hostname("chicago") == "chicago.craigslist.org"
        assert resolve_hostname("los angeles") == "losangeles.craigslist.org"
        assert resolve_hostname("san francisco") == "sfbay.craigslist.org"

    def test_alias_mapping(self):
        # St. Catharines maps to Hamilton CL area
        assert resolve_hostname("st catharines") == "hamilton.craigslist.org"
        assert resolve_hostname("niagara") == "hamilton.craigslist.org"
        assert resolve_hostname("niagara falls") == "hamilton.craigslist.org"
        # SF aliases
        assert resolve_hostname("SF") == "sfbay.craigslist.org"
        assert resolve_hostname("bay area") == "sfbay.craigslist.org"

    def test_with_province_state(self):
        assert resolve_hostname("vancouver, BC") == "vancouver.craigslist.org"
        assert resolve_hostname("toronto, ON") == "toronto.craigslist.org"

    def test_normalization(self):
        # Dots, hyphens, case
        assert resolve_hostname("St. Catharines") == "hamilton.craigslist.org"
        assert resolve_hostname("TORONTO") == "toronto.craigslist.org"
        assert resolve_hostname("  vancouver  ") == "vancouver.craigslist.org"

    def test_fuzzy_match(self):
        # Minor typo
        result = resolve_hostname("toronot")
        assert result == "toronto.craigslist.org"

    def test_unknown_city(self):
        assert resolve_hostname("xyznonexistent") is None

    def test_is_canadian(self):
        assert is_canadian_location("toronto") is True
        assert is_canadian_location("vancouver") is True
        assert is_canadian_location("st catharines") is True
        assert is_canadian_location("chicago") is False
        assert is_canadian_location("los angeles") is False
        assert is_canadian_location("seattle") is False


# ---------------------------------------------------------------------------
# Alias dicts
# ---------------------------------------------------------------------------


class TestAliases:
    def test_sort_map_completeness(self):
        """All sort aliases have entries for all 3 platforms."""
        for alias, mapping in SORT_MAP.items():
            assert "craigslist" in mapping, f"Missing craigslist in sort '{alias}'"
            assert "kijiji" in mapping, f"Missing kijiji in sort '{alias}'"
            assert "facebook" in mapping, f"Missing facebook in sort '{alias}'"

    def test_category_map_completeness(self):
        for alias, mapping in CATEGORY_MAP.items():
            assert "craigslist" in mapping, f"Missing craigslist in category '{alias}'"
            assert "kijiji" in mapping, f"Missing kijiji in category '{alias}'"
            assert "facebook" in mapping, f"Missing facebook in category '{alias}'"

    def test_condition_map_completeness(self):
        for alias, mapping in CONDITION_MAP.items():
            assert "craigslist" in mapping, f"Missing craigslist in condition '{alias}'"
            assert "kijiji" in mapping, f"Missing kijiji in condition '{alias}'"
            assert "facebook" in mapping, f"Missing facebook in condition '{alias}'"

    def test_resolve_alias_sort(self):
        assert resolve_alias("date", SORT_MAP, "craigslist") == "date"
        assert resolve_alias("date", SORT_MAP, "kijiji") == "dateDesc"
        assert resolve_alias("date", SORT_MAP, "facebook") == "CREATION_TIME_DESCEND"
        assert resolve_alias("price_asc", SORT_MAP, "kijiji") == "priceAsc"

    def test_resolve_alias_category(self):
        assert resolve_alias("cars", CATEGORY_MAP, "craigslist") == "cta"
        assert resolve_alias("cars", CATEGORY_MAP, "kijiji") == "c174"
        assert resolve_alias("cars", CATEGORY_MAP, "facebook") == "vehicles"
        assert resolve_alias("all", CATEGORY_MAP, "craigslist") == "sss"
        assert resolve_alias("all", CATEGORY_MAP, "kijiji") == "c10"

    def test_resolve_alias_condition(self):
        assert resolve_alias("new", CONDITION_MAP, "craigslist") == "10"
        assert resolve_alias("new", CONDITION_MAP, "kijiji") == "new"
        assert resolve_alias("like_new", CONDITION_MAP, "facebook") == "used_like_new"

    def test_resolve_alias_none(self):
        assert resolve_alias(None, SORT_MAP, "craigslist") is None
        assert resolve_alias("nonexistent", SORT_MAP, "craigslist") is None

    def test_resolve_alias_platform_none(self):
        # Some categories have None for some platforms
        assert resolve_alias("all", CATEGORY_MAP, "facebook") is None
        assert resolve_alias("relevance", SORT_MAP, "kijiji") is None


# ---------------------------------------------------------------------------
# Kijiji location cross-check
# ---------------------------------------------------------------------------


class TestKijijiLocationCrossCheck:
    """Verify that Kijiji locations resolve for typical Canadian cities."""

    def test_major_cities(self):
        assert resolve_location("toronto") is not None
        assert resolve_location("vancouver") is not None
        assert resolve_location("montreal") is not None
        assert resolve_location("st catharines") is not None

    def test_us_city_returns_none(self):
        # Kijiji is Canada-only — US cities should not resolve
        assert resolve_location("chicago") is None
        assert resolve_location("los angeles") is None


# ---------------------------------------------------------------------------
# Orchestrator (mocked platform searches)
# ---------------------------------------------------------------------------


class TestSearchMarketplace:
    @pytest.mark.asyncio
    async def test_deadline_does_not_wait_for_noncooperative_child(
        self,
        monkeypatch,
    ):
        release = asyncio.Event()

        async def noncooperative(*_args, **_kwargs):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return {"platform": "kijiji", "content": "too late"}

        with (
            patch(
                "fetchaller.marketplace.search._search_kijiji",
                side_effect=noncooperative,
            ),
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch(
                "fetchaller.craigslist.locations.is_canadian_location",
                return_value=True,
            ),
        ):
            mock_cl.return_value = {
                "platform": "craigslist",
                "content": "Verified Craigslist results",
            }
            mock_fb.return_value = {
                "platform": "facebook",
                "error": "blocked",
            }
            monkeypatch.setattr(
                "fetchaller.marketplace.search._MARKETPLACE_TIMEOUT",
                0.05,
            )
            started = time.monotonic()
            result = await search_marketplace(
                query="bike",
                location="toronto",
            )
            elapsed = time.monotonic() - started

        assert elapsed < 0.5
        assert "Verified Craigslist results" in result["content"]
        assert "Kijiji: Platform search timed out" in result["content"]
        release.set()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_all_platforms_succeed(self):
        """When all 3 platforms return content, output has all 3 sections."""
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=True),
        ):
            mock_kj.return_value = {"platform": "kijiji", "content": "Kijiji results here"}
            mock_cl.return_value = {"platform": "craigslist", "content": "Craigslist results here"}
            mock_fb.return_value = {"platform": "facebook", "content": "Facebook results here"}

            result = await search_marketplace(
                query="golf r",
                location="toronto",
            )

            assert "content" in result
            assert "Kijiji" in result["content"]
            assert "Craigslist" in result["content"]
            assert "Facebook Marketplace" in result["content"]

    @pytest.mark.asyncio
    async def test_one_platform_fails(self):
        """If one platform errors, the others still return."""
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=True),
        ):
            mock_kj.return_value = {"platform": "kijiji", "error": "Timeout"}
            mock_cl.return_value = {"platform": "craigslist", "content": "CL results"}
            mock_fb.return_value = {"platform": "facebook", "content": "FB results"}

            result = await search_marketplace(
                query="couch",
                location="toronto",
            )

            assert "content" in result
            assert "Craigslist" in result["content"]
            assert "Facebook Marketplace" in result["content"]
            assert "Kijiji" in result["content"]  # appears in errors section
            assert "Timeout" in result["content"]

    @pytest.mark.asyncio
    async def test_kijiji_skipped_for_us(self):
        """Kijiji is auto-skipped for US locations."""
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=False),
        ):
            mock_cl.return_value = {"platform": "craigslist", "content": "CL results"}
            mock_fb.return_value = {"platform": "facebook", "content": "FB results"}

            result = await search_marketplace(
                query="couch",
                location="chicago",
            )

            assert "content" in result
            mock_kj.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_platforms_fail(self):
        """If all platforms fail, return error."""
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=True),
        ):
            mock_kj.return_value = {"platform": "kijiji", "error": "fail1"}
            mock_cl.return_value = {"platform": "craigslist", "error": "fail2"}
            mock_fb.return_value = {"platform": "facebook", "error": "fail3"}

            result = await search_marketplace(
                query="thing",
                location="toronto",
            )

            assert "error" in result
            assert "All platforms failed" in result["error"]

    @pytest.mark.asyncio
    async def test_platform_exception_handled(self):
        """If a platform raises an exception, it's caught and others continue."""
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=True),
        ):
            mock_kj.side_effect = RuntimeError("boom")
            mock_cl.return_value = {"platform": "craigslist", "content": "CL results"}
            mock_fb.return_value = {"platform": "facebook", "content": "FB results"}

            result = await search_marketplace(
                query="bike",
                location="toronto",
            )

            assert "content" in result
            assert "Craigslist" in result["content"]

    @pytest.mark.asyncio
    async def test_child_cancellation_is_reported_without_crashing_or_hiding_success(self):
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch(
                "fetchaller.craigslist.locations.is_canadian_location",
                return_value=True,
            ),
        ):
            mock_kj.side_effect = asyncio.CancelledError
            mock_cl.return_value = {
                "platform": "craigslist",
                "content": "Verified Craigslist results",
            }
            mock_fb.return_value = {"platform": "facebook", "error": "blocked"}

            result = await search_marketplace(query="bike", location="toronto")

        assert "Verified Craigslist results" in result["content"]
        assert "Kijiji: search cancelled" in result["content"]

    @pytest.mark.asyncio
    async def test_specific_platforms(self):
        """Requesting specific platforms only searches those."""
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=True),
        ):
            mock_kj.return_value = {"platform": "kijiji", "content": "KJ results"}

            result = await search_marketplace(
                query="desk",
                location="toronto",
                platforms=["kijiji"],
            )

            assert "content" in result
            mock_cl.assert_not_called()
            mock_fb.assert_not_called()

    @pytest.mark.asyncio
    async def test_price_in_header(self):
        """Price range appears in the header."""
        with (
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=False),
        ):
            mock_cl.return_value = {"platform": "craigslist", "content": "results"}
            mock_fb.return_value = {"platform": "facebook", "content": "results"}

            result = await search_marketplace(
                query="car",
                location="chicago",
                min_price=5000,
                max_price=50000,
            )

            assert "content" in result
            assert "$5,000" in result["content"]
            assert "$50,000" in result["content"]

    @pytest.mark.asyncio
    async def test_fb_location_disambiguated_for_canada(self):
        """Facebook gets ', Canada' appended for Canadian locations to avoid geocoding ambiguity."""
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=True),
        ):
            mock_kj.return_value = {"platform": "kijiji", "content": "KJ"}
            mock_cl.return_value = {"platform": "craigslist", "content": "CL"}
            mock_fb.return_value = {"platform": "facebook", "content": "FB"}

            await search_marketplace(query="couch", location="vancouver")

            # Facebook should receive "vancouver, Canada", not bare "vancouver"
            fb_call_args = mock_fb.call_args[0]
            assert fb_call_args[1] == "vancouver, Canada"

            # Kijiji and CL should still get the original location
            kj_call_args = mock_kj.call_args[0]
            assert kj_call_args[1] == "vancouver"
            cl_call_args = mock_cl.call_args[0]
            assert cl_call_args[1] == "vancouver"

    @pytest.mark.asyncio
    async def test_fb_location_not_disambiguated_for_us(self):
        """US locations are not modified for Facebook geocoding."""
        with (
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=False),
        ):
            mock_cl.return_value = {"platform": "craigslist", "content": "CL"}
            mock_fb.return_value = {"platform": "facebook", "content": "FB"}

            await search_marketplace(query="desk", location="seattle")

            fb_call_args = mock_fb.call_args[0]
            assert fb_call_args[1] == "seattle"

    @pytest.mark.asyncio
    async def test_fb_location_with_comma_not_doubled(self):
        """Location with comma (e.g. 'toronto, ON') should not get ', Canada' appended."""
        with (
            patch("fetchaller.marketplace.search._search_kijiji") as mock_kj,
            patch("fetchaller.marketplace.search._search_craigslist") as mock_cl,
            patch("fetchaller.marketplace.search._search_facebook") as mock_fb,
            patch("fetchaller.craigslist.locations.is_canadian_location", return_value=True),
        ):
            mock_kj.return_value = {"platform": "kijiji", "content": "KJ"}
            mock_cl.return_value = {"platform": "craigslist", "content": "CL"}
            mock_fb.return_value = {"platform": "facebook", "content": "FB"}

            await search_marketplace(query="couch", location="toronto, ON")

            fb_call_args = mock_fb.call_args[0]
            assert fb_call_args[1] == "toronto, ON"

    @pytest.mark.asyncio
    async def test_invalid_platforms(self):
        """Invalid platform names are filtered out."""
        result = await search_marketplace(
            query="test",
            location="toronto",
            platforms=["invalid_platform"],
        )
        assert "error" in result
        assert "No valid platforms" in result["error"]
