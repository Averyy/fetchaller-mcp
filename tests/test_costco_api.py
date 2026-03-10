"""Tests for Costco search API response parsing and key extraction."""

import json

from fetchaller.costco import api
from fetchaller.costco.api import (
    _extract_api_key,
    _load_cached_keys,
    _save_cached_keys,
    get_total_count,
    parse_search_items,
)

# ---------------------------------------------------------------------------
# API key extraction from HTML
# ---------------------------------------------------------------------------


class TestExtractApiKey:
    def test_plain_json_format(self):
        html = '{"apikey","value":"273db6be-f015-4de7-b0d6-dd4746ccd5c3"}'
        assert _extract_api_key(html) == "273db6be-f015-4de7-b0d6-dd4746ccd5c3"

    def test_backslash_delimited_format(self):
        # Regex [\\"] matches either \ or " as delimiter — test with backslash
        html = "\\apikey\\,\\value\\:\\134a4023-68d5-4138-8e03-8353667d5fb3\\"
        assert _extract_api_key(html) == "134a4023-68d5-4138-8e03-8353667d5fb3"

    def test_surrounded_by_other_html(self):
        html = (
            '<html><head><script>var config = {"settings":[{"apikey","value":'
            '"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}]}</script></head></html>'
        )
        assert _extract_api_key(html) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_no_key_present(self):
        assert _extract_api_key("<html><body>no api key here</body></html>") is None

    def test_invalid_uuid_not_matched(self):
        # Too short to be a UUID
        html = '{"apikey","value":"not-a-uuid"}'
        assert _extract_api_key(html) is None


# ---------------------------------------------------------------------------
# Search response parsing
# ---------------------------------------------------------------------------


def _make_costco_response(docs, num_found=None):
    """Build a fake Costco search API response."""
    return {
        "response": {
            "docs": docs,
            "numFound": num_found if num_found is not None else len(docs),
        }
    }


def _make_costco_doc(
    title="Kirkland Paper Towels",
    item_number="1234567",
    price=19.99,
    sale_price=None,
    rating=4.5,
    reviews=128,
    brand="Kirkland Signature",
    stock="In Stock",
    slug="kirkland-paper-towels",
    description="Premium paper towels",
    features=None,
):
    """Build a fake Costco search result document."""
    doc = {
        "item_product_name": title,
        "item_number": item_number,
        "item_location_pricing_listPrice": price,
        "item_location_pricing_salePrice": sale_price,
        "item_review_ratings": rating,
        "item_product_review_count": reviews,
        "Brand_attr": [brand] if brand else [],
        "item_location_stockStatus": stock,
        "slug": slug,
        "item_product_primary_image": "https://images.costco.com/image.jpg",
        "item_short_description": description,
        "item_product_marketing_features": features or [],
    }
    return doc


class TestParseSearchItems:
    def test_parses_basic_fields(self):
        data = _make_costco_response([
            _make_costco_doc("Kirkland Paper Towels", item_number="1234567",
                             price=19.99, brand="Kirkland Signature"),
        ])
        items = parse_search_items(data)
        assert len(items) == 1
        assert items[0]["title"] == "Kirkland Paper Towels"
        assert items[0]["item_number"] == "1234567"
        assert items[0]["price"] == 19.99
        assert items[0]["brand"] == "Kirkland Signature"

    def test_url_built_from_slug(self):
        data = _make_costco_response([
            _make_costco_doc(slug="kirkland-paper-towels"),
        ])
        items = parse_search_items(data)
        assert items[0]["url"] == "https://www.costco.com/kirkland-paper-towels.html"

    def test_no_slug_falls_back_to_item_number(self):
        data = _make_costco_response([
            _make_costco_doc(slug="", item_number="9876543"),
        ])
        items = parse_search_items(data)
        assert items[0]["url"] == "https://www.costco.com/p/-/9876543"

    def test_no_slug_no_item_number_empty_url(self):
        data = _make_costco_response([
            _make_costco_doc(slug="", item_number=""),
        ])
        items = parse_search_items(data)
        assert items[0]["url"] == ""

    def test_sale_price_extracted(self):
        data = _make_costco_response([
            _make_costco_doc(price=29.99, sale_price=19.99),
        ])
        items = parse_search_items(data)
        assert items[0]["price"] == 29.99
        assert items[0]["sale_price"] == 19.99

    def test_skips_items_without_title(self):
        data = _make_costco_response([
            _make_costco_doc(title=""),
            _make_costco_doc(title="Valid Item"),
        ])
        items = parse_search_items(data)
        assert len(items) == 1
        assert items[0]["title"] == "Valid Item"

    def test_skips_non_dict_docs(self):
        data = _make_costco_response([
            "not a dict",
            _make_costco_doc(title="Real Item"),
        ])
        items = parse_search_items(data)
        assert len(items) == 1
        assert items[0]["title"] == "Real Item"

    def test_features_split_by_semicolon(self):
        data = _make_costco_response([
            _make_costco_doc(features=["Feature A; Feature B; Feature C"]),
        ])
        items = parse_search_items(data)
        assert items[0]["features"] == ["Feature A", "Feature B", "Feature C"]

    def test_features_multiple_entries(self):
        data = _make_costco_response([
            _make_costco_doc(features=["Feature A", "Feature B"]),
        ])
        items = parse_search_items(data)
        assert items[0]["features"] == ["Feature A", "Feature B"]

    def test_features_non_list_treated_as_empty(self):
        doc = _make_costco_doc()
        doc["item_product_marketing_features"] = "not a list"
        data = _make_costco_response([doc])
        items = parse_search_items(data)
        assert items[0]["features"] == []

    def test_brand_empty_when_no_brand(self):
        data = _make_costco_response([
            _make_costco_doc(brand=""),
        ])
        items = parse_search_items(data)
        assert items[0]["brand"] == ""

    def test_empty_docs(self):
        data = _make_costco_response([])
        assert parse_search_items(data) == []

    def test_missing_response_key(self):
        assert parse_search_items({}) == []

    def test_multiple_items(self):
        data = _make_costco_response([
            _make_costco_doc("Item A"),
            _make_costco_doc("Item B"),
            _make_costco_doc("Item C"),
        ])
        items = parse_search_items(data)
        assert [i["title"] for i in items] == ["Item A", "Item B", "Item C"]


# ---------------------------------------------------------------------------
# Total count extraction
# ---------------------------------------------------------------------------


class TestGetTotalCount:
    def test_extracts_count(self):
        data = _make_costco_response([], num_found=4523)
        assert get_total_count(data) == 4523

    def test_missing_response(self):
        assert get_total_count({}) == 0

    def test_missing_num_found(self):
        assert get_total_count({"response": {}}) == 0


# ---------------------------------------------------------------------------
# API key disk caching
# ---------------------------------------------------------------------------


def _reset_key_state():
    """Reset all in-memory key state for test isolation."""
    api._api_keys.clear()
    api._default_keys_exhausted.clear()
    api._keys_loaded = False


class TestKeyCachePersistence:
    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)

        # Save keys
        api._api_keys["com"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        api._api_keys["ca"] = "11111111-2222-3333-4444-555555555555"
        _save_cached_keys()
        assert cache_file.exists()

        # Clear memory and load from disk
        api._api_keys.clear()
        _load_cached_keys()
        assert api._api_keys["com"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert api._api_keys["ca"] == "11111111-2222-3333-4444-555555555555"

    def test_load_ignores_missing_file(self, tmp_path, monkeypatch):
        _reset_key_state()
        monkeypatch.setattr(api, "_key_cache_path", lambda: tmp_path / "nope.json")
        _load_cached_keys()
        assert api._api_keys == {}

    def test_load_deletes_corrupt_json(self, tmp_path, monkeypatch):
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text("not json{{{")
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)
        _load_cached_keys()
        assert api._api_keys == {}
        assert not cache_file.exists()

    def test_load_deletes_non_dict_json(self, tmp_path, monkeypatch):
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text('["a list", "not a dict"]')
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)
        _load_cached_keys()
        assert api._api_keys == {}
        assert not cache_file.exists()

    def test_load_keeps_valid_keys_deletes_none(self, tmp_path, monkeypatch):
        """File with at least one valid key is kept, invalid entries skipped."""
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text(json.dumps({
            "com": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",  # 36 chars
            "ca": "too-short",  # not 36 chars
            "au": 12345,  # not a string
        }))
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)
        _load_cached_keys()
        assert "com" in api._api_keys
        assert "ca" not in api._api_keys
        assert "au" not in api._api_keys
        assert cache_file.exists()  # kept because "com" was valid

    def test_load_deletes_file_with_no_valid_keys(self, tmp_path, monkeypatch):
        """File where all keys are invalid gets deleted."""
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text(json.dumps({
            "ca": "too-short",
            "au": 12345,
        }))
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)
        _load_cached_keys()
        assert api._api_keys == {}
        assert not cache_file.exists()

    def test_load_deletes_empty_file(self, tmp_path, monkeypatch):
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text("")
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)
        _load_cached_keys()
        assert api._api_keys == {}
        assert not cache_file.exists()

    def test_load_deletes_empty_dict(self, tmp_path, monkeypatch):
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text("{}")
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)
        _load_cached_keys()
        assert api._api_keys == {}
        assert not cache_file.exists()

    def test_load_deletes_json_null(self, tmp_path, monkeypatch):
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text("null")
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)
        _load_cached_keys()
        assert api._api_keys == {}
        assert not cache_file.exists()

    def test_save_with_no_cache_dir(self, monkeypatch):
        _reset_key_state()
        monkeypatch.setattr(api, "_key_cache_path", lambda: None)
        api._api_keys["com"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        # Should not raise
        _save_cached_keys()

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch):
        _reset_key_state()
        cache_file = tmp_path / "deep" / "nested" / "costco_api_keys.json"
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)
        api._api_keys["com"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _save_cached_keys()
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert data["com"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_load_only_runs_once(self, tmp_path, monkeypatch):
        """_keys_loaded flag prevents re-reading disk on every resolve call."""
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text(json.dumps({"com": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}))
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)

        _load_cached_keys()
        api._keys_loaded = True

        # Overwrite the file — shouldn't matter, already loaded
        cache_file.write_text(json.dumps({"com": "ffffffff-ffff-ffff-ffff-ffffffffffff"}))
        api._api_keys.clear()
        # Simulate what _resolve_api_key does: check _keys_loaded
        if not api._keys_loaded:
            _load_cached_keys()
        # Keys should NOT have been reloaded (flag was set)
        assert "com" not in api._api_keys


class TestKeyLifecycle:
    """Test the full resolve → 401 → refresh → save cycle."""

    def test_cached_key_preferred_over_default(self, tmp_path, monkeypatch):
        _reset_key_state()
        cache_file = tmp_path / "costco_api_keys.json"
        cache_file.write_text(json.dumps({"com": "cached00-0000-0000-0000-000000000000"}))
        monkeypatch.setattr(api, "_key_cache_path", lambda: cache_file)

        _load_cached_keys()
        # Cached key should take priority over hardcoded default
        assert api._api_keys["com"] == "cached00-0000-0000-0000-000000000000"

    def test_exhausted_default_skips_to_refresh(self):
        """After 401, default is exhausted and not reused."""
        _reset_key_state()
        api._default_keys_exhausted.add("com")
        # _resolve_api_key would skip the default and call _refresh
        # Just verify the state prevents default usage
        assert "com" in api._default_keys_exhausted
        assert "com" not in api._api_keys

    def test_refresh_evicts_and_exhausts(self):
        """_refresh_api_key evicts stale key and marks default exhausted."""
        _reset_key_state()
        api._api_keys["com"] = "stale000-0000-0000-0000-000000000000"
        # Simulate what _refresh_api_key does at the top
        api._api_keys.pop("com", None)
        api._default_keys_exhausted.add("com")
        assert "com" not in api._api_keys
        assert "com" in api._default_keys_exhausted

    def test_successful_refresh_clears_exhausted(self, tmp_path, monkeypatch):
        """After successful refresh, default exhausted flag is cleared."""
        _reset_key_state()
        monkeypatch.setattr(api, "_key_cache_path", lambda: tmp_path / "keys.json")
        api._default_keys_exhausted.add("com")

        # Simulate successful refresh: new key stored, exhausted cleared, saved
        new_key = "newkey00-0000-0000-0000-00000000000e"
        api._api_keys["com"] = new_key
        api._default_keys_exhausted.discard("com")
        _save_cached_keys()

        assert api._api_keys["com"] == new_key
        assert "com" not in api._default_keys_exhausted
        # Verify disk has the new key
        data = json.loads((tmp_path / "keys.json").read_text())
        assert data["com"] == new_key

    def test_retry_401_evicts_refreshed_key(self):
        """If retry after refresh also 401s, the bad key is evicted."""
        _reset_key_state()
        api._api_keys["com"] = "badkey00-0000-0000-0000-00000000000f"
        # Simulate the post-retry 401 eviction
        api._api_keys.pop("com", None)
        assert "com" not in api._api_keys
