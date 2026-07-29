"""URL detection, filter mapping, and rendering unit tests for realtor.ca."""

from unittest.mock import AsyncMock, patch

import pytest
from bs4 import BeautifulSoup

from fetchaller.realtor import api, render

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_listing(self):
        u = "https://www.realtor.ca/real-estate/29867248/2110-ricardo-street-ottawa"
        assert api.is_realtor(u)
        assert api.is_realtor_listing(u)
        assert not api.is_realtor_search(u)
        assert api.extract_listing_id(u) == "29867248"

    def test_listing_french(self):
        u = "https://www.realtor.ca/immobilier/29867248/2110-ricardo-street-ottawa"
        assert api.is_realtor_listing(u)
        assert api.extract_listing_id(u) == "29867248"

    def test_seo_search(self):
        for u in [
            "https://www.realtor.ca/on/ottawa/real-estate",
            "https://www.realtor.ca/on/ottawa/orleans/real-estate",
            "https://www.realtor.ca/map",
        ]:
            assert api.is_realtor_search(u)
            assert not api.is_realtor_listing(u)

    def test_rejects_non_realtor(self):
        for u in ["https://example.com/real-estate/123/x", "https://realtor.com/foo"]:
            assert not api.is_realtor(u)

    def test_extract_id_none(self):
        assert api.extract_listing_id("https://www.realtor.ca/map") is None


# ---------------------------------------------------------------------------
# Filter encodings
# ---------------------------------------------------------------------------


class TestFilters:
    def test_range(self):
        assert api._range(3) == "3-0"
        assert api._range(0) is None
        assert api._range(None) is None

    def test_range_min(self):
        assert api._range_min("3-0") == 3
        assert api._range_min("0-0") is None
        assert api._range_min(None) is None
        assert api._range_min("garbage") is None

    def test_invert(self):
        assert api._invert(api.SORT, "6-D", "newest") == "newest"
        assert api._invert(api.PROPERTY_TYPE, "3", "any") == "condo"
        assert api._invert(api.BUILDING_TYPE, "999", None) is None

    def test_vocab_present(self):
        assert api.TRANSACTION["rent"] == "3"
        assert api.PROPERTY_TYPE["vacant-land"] == "6"
        assert api.BUILDING_TYPE["townhouse"] == "16"
        assert api.OWNERSHIP["freehold"] == "1"

    def test_place_from_slug(self):
        assert api._place_from_slug("/on/ottawa/orleans/real-estate") == "orleans, ottawa"
        assert api._place_from_slug("/on/ottawa/real-estate") == "ottawa"


class TestMapKwargs:
    def test_parses_hash_and_query(self):
        url = (
            "https://www.realtor.ca/map#LatitudeMax=45.5&LongitudeMax=-75.2"
            "&LatitudeMin=44.9&LongitudeMin=-76.3&TransactionTypeId=2"
            "&PropertySearchTypeId=3&PriceMin=500000&PriceMax=800000"
            "&BedRange=3-0&Sort=1-A&GeoIds=g30_abc&CurrentPage=2"
        )
        out = api._map_kwargs(url)
        kw = out["kwargs"]
        assert kw["bbox"] == {
            "LatitudeMax": "45.5", "LongitudeMax": "-75.2",
            "LatitudeMin": "44.9", "LongitudeMin": "-76.3",
        }
        assert kw["transaction"] == "sale"
        assert kw["property_type"] == "condo"
        assert kw["min_price"] == 500000
        assert kw["max_price"] == 800000
        assert kw["min_beds"] == 3
        assert kw["sort"] == "price-asc"
        assert kw["geoids"] == "g30_abc"
        assert kw["page"] == 2

    def test_rent_uses_rent_params(self):
        url = "https://www.realtor.ca/map#TransactionTypeId=3&RentMin=1500&RentMax=2500"
        kw = api._map_kwargs(url)["kwargs"]
        assert kw["transaction"] == "rent"
        assert kw["min_price"] == 1500
        assert kw["max_price"] == 2500


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_SAMPLE_RESULT = {
    "Id": "123",
    "MlsNumber": "X111",
    "Property": {
        "Price": "$649,900",
        "Type": "Single Family",
        "Address": {"AddressText": "1 MAIN ST|Ottawa, Ontario K1A0A1"},
        "ParkingType": "Attached Garage",
    },
    "Building": {"Bedrooms": "3", "BathroomTotal": "2", "Type": "House", "SizeInterior": "120 m2"},
    "Land": {"SizeTotal": "40 x 100 FT"},
    "Individual": [{"Name": "Jane Agent", "Organization": {"Name": "ACME REALTY"}}],
    "RelativeDetailsURL": "/real-estate/123/1-main-st-ottawa",
    "TimeOnRealtor": "2 hours ago",
}

_SAMPLE_DATA = {
    "Paging": {"TotalRecords": 4655, "CurrentPage": 1, "TotalPages": 50, "RecordsShowing": 600},
    "Pins": [{"count": 5}, {"count": 1}],
    "Results": [_SAMPLE_RESULT],
}


class TestRenderSearch:
    def test_basic(self):
        out = render.render_search_results(
            _SAMPLE_DATA, location="Ottawa, ON", transaction="sale",
            filters_desc="3+ bed", page=1,
        )
        assert "4,655 listings for sale" in out
        assert "Ottawa, ON" in out
        assert "3+ bed" in out
        assert "$649,900" in out
        assert "1 MAIN ST, Ottawa, Ontario K1A0A1" in out
        assert "3 bed · 2 bath" in out
        assert "ACME REALTY" in out
        assert "https://www.realtor.ca/real-estate/123/1-main-st-ottawa" in out
        # 600-record cap note appears when TotalRecords exceeds it
        assert "600" in out

    def test_rent_verb(self):
        out = render.render_search_results(_SAMPLE_DATA, location="Toronto", transaction="rent")
        assert "for rent" in out

    def test_empty(self):
        out = render.render_search_results(
            {"Paging": {"TotalRecords": 0}, "Results": []}, location="Nowhere",
        )
        assert "No listings" in out


class TestRenderListing:
    def test_detail(self):
        parsed = {
            "url": "https://www.realtor.ca/real-estate/123/1-main-st",
            "id": "123",
            "price": "$649,900",
            "address": "1 MAIN ST Ottawa",
            "beds": "3 Beds",
            "baths": "2 Baths",
            "description": "Lovely home.",
            "location_description": "Cross Streets: Main & 1st.",
            "details": [("Property Type", "Single Family"), ("Annual Property Taxes", "$5,000")],
            "rooms": [("Main level Living room", "4.6 m x 4.3 m"), ("Kitchen", "3.7 m x 2.9 m")],
            "mls": "X111",
            "agent": "Jane Agent",
            "brokerage": "ACME REALTY Brokerage",
            "coords": (45.4, -75.7),
        }
        out = render.render_listing_detail(parsed, similar=[_SAMPLE_RESULT])
        assert out.startswith("# 1 MAIN ST Ottawa")
        assert "$649,900" in out
        assert "MLS X111" in out
        assert "Listed by Jane Agent / ACME REALTY Brokerage" in out
        assert "## Description" in out
        assert "Lovely home." in out
        assert "## Property Details" in out
        assert "**Property Type:** Single Family" in out
        assert "## Rooms" in out
        assert "**Main level Living room** — 4.6 m x 4.3 m" in out
        assert "## Location" in out
        assert "Cross Streets: Main & 1st." in out
        assert "## Similar Listings Nearby" in out
        assert "ACME REALTY" in out


_LISTING_HTML = """
<html><body>
<div id="listingPriceValue">$500,000</div>
<div id="listingAddress">1 Main St, Ottawa</div>
<div id="galleryBeds">3 Beds</div>
<div id="galleryBaths">2 Baths</div>
<div id="propertyDescriptionCon">Nice home.</div>
<div class="propertyDetailsSectionContentLabel">Property Type</div>
<div class="propertyDetailsSectionContentValue">House</div>
<div class="listingDetailsRoomDetailsCon RoomLevel9">Main level Living room
  <div class="listingDetailsRoomDetails_DimensionsCon">
    <div class="listingDetailsRoomDetails_Dimensions Metric">4.6 m x 4.3 m</div>
    <div class="listingDetailsRoomDetails_Dimensions Imperial">15 ft x 14 ft</div>
  </div>
</div>
<div class="listingDetailsRoomDetailsCon RoomLevel9">Kitchen
  <div class="listingDetailsRoomDetails_DimensionsCon">
    <div class="listingDetailsRoomDetails_Dimensions Metric">3.7 m x 2.9 m</div>
  </div>
</div>
<div id="LocationDescription">Location Description Cross Streets: Main.</div>
<div class="realtorCardName">Jane Agent</div>
<div id="OfficeCard-123">ACME REALTY Brokerage 1 Office St, Ottawa</div>
<div id="listingMLSNumberCon">MLS ® Number: X999999</div>
<a href="https://www.google.com/maps/dir/?api=1&destination=45.42%2c-75.70">dir</a>
</body></html>
"""


class TestExtractAgent:
    """Brokerage parsing must work for EN and FR (Quebec /immobilier/) pages."""

    def _agent(self, office_html: str):
        soup = BeautifulSoup(
            f"<div class='realtorCardName'>Jane Agent</div>{office_html}", "lxml"
        )
        return api._extract_agent(soup)

    def test_english_brokerage(self):
        a, b = self._agent(
            "<div id='OfficeCard-1'>ACME REALTY Brokerage #5-1 Main St Ottawa 613-555-1234</div>"
        )
        assert a == "Jane Agent"
        assert b == "ACME REALTY Brokerage"

    def test_french_brokerage(self):
        # The bug: splitting only on EN 'Brokerage' left FR brokerages full of address junk.
        _, b = self._agent(
            "<div id='OfficeCard-1'>ROYAL LEPAGE Bureau de courtage #107-250 CENTRUM BLVD OTTAWA 613-830-3350</div>"
        )
        assert b == "ROYAL LEPAGE Bureau de courtage"

    def test_fallback_trims_address_when_no_keyword(self):
        _, b = self._agent(
            "<div id='OfficeCard-1'>SOME REALTY INC 123 Main St Ottawa 613-555-1234</div>"
        )
        assert b == "SOME REALTY INC"


class TestParseListing:
    def test_parse_all_fields(self):
        p = api.parse_listing_html(_LISTING_HTML, "https://www.realtor.ca/real-estate/123/1-main-st")
        assert p["price"] == "$500,000"
        assert p["price_value"] == 500000
        assert p["address"] == "1 Main St, Ottawa"
        assert p["transaction"] == "sale"
        assert p["beds"] == "3 Beds"
        assert p["description"] == "Nice home."
        assert ("Property Type", "House") in p["details"]
        assert p["rooms"] == [
            ("Main level Living room", "4.6 m x 4.3 m"),
            ("Kitchen", "3.7 m x 2.9 m"),
        ]
        assert p["location_description"] == "Cross Streets: Main."
        assert p["agent"] == "Jane Agent"
        assert p["brokerage"] == "ACME REALTY Brokerage"
        assert p["mls"] == "X999999"
        assert p["coords"] == (45.42, -75.70)

    @pytest.mark.parametrize(
        "signal",
        [
            '<script>window.listing={"TransactionTypeId":"3"}</script>',
            "<div>For Rent</div>",
            '<div id="listingPriceValue">$2,450 / month</div>',
        ],
    )
    def test_rental_transaction_is_inferred_from_ssr(self, signal):
        html = _LISTING_HTML.replace(
            '<div id="listingPriceValue">$500,000</div>',
            signal
            if "listingPriceValue" in signal
            else '<div id="listingPriceValue">$2,450</div>' + signal,
        )

        parsed = api.parse_listing_html(
            html,
            "https://www.realtor.ca/real-estate/123/1-main-st",
        )

        assert parsed["transaction"] == "rent"

    @pytest.mark.asyncio
    async def test_rental_similar_listings_use_rent_price_filters(self):
        parsed = {
            "id": "123",
            "coords": (45.42, -75.70),
            "price_value": 2000,
            "transaction": "rent",
        }
        with patch(
            "fetchaller.realtor.api.property_search",
            new_callable=AsyncMock,
            return_value={"Results": [_SAMPLE_RESULT]},
        ) as search:
            await api.get_similar_listings(parsed)

        kwargs = search.await_args.kwargs
        assert kwargs["transaction"] == "rent"
        assert kwargs["min_price"] == 1200
        assert kwargs["max_price"] == 3000


class TestWaferErrorTyped:
    """_wafer_error dispatches on exception type, not a str(e) substring scan."""

    def test_challenge_uses_challenge_type(self):
        import wafer

        from fetchaller.realtor.search import _wafer_error
        out = _wafer_error(wafer.ChallengeDetected("imperva", "https://www.realtor.ca/", 403))
        assert "imperva" in out["error"].lower()

    def test_timeout_message(self):
        import wafer

        from fetchaller.realtor.search import _wafer_error
        out = _wafer_error(wafer.WaferTimeout("https://www.realtor.ca/", 60))
        assert "timed out" in out["error"].lower()
