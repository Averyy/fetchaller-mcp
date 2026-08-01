"""Workday facet discovery and location resolution.

Tenants agree on almost nothing about their filters, so these fixtures are
trimmed from real responses across seven boards. The point of every test here
is that nothing is keyed by facet *name*: Adobe calls the country facet
``locationCountry``, NVIDIA ``locationHierarchy1``, and Salesforce a 90-character
custom field, and all three must resolve "Canada" the same way.
"""

from fetchaller.content import workday

# Trimmed from the live /jobs responses.
ADOBE_FACETS = [
    {"facetParameter": "jobFamilyGroup", "descriptor": "Job Category", "values": [
        {"descriptor": "Engineering", "id": "eng", "count": 100},
    ]},
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
            {"descriptor": "Canada", "id": "ca-id", "count": 6},
            {"descriptor": "United States of America", "id": "us-id", "count": 557},
        ]},
        {"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "Toronto", "id": "tor-id", "count": 2},
            {"descriptor": "Ottawa", "id": "ott-id", "count": 1},
            {"descriptor": "Austin", "id": "aus-id", "count": 21},
        ]},
    ]},
]

NVIDIA_FACETS = [
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationHierarchy1", "descriptor": "Locations", "values": [
            {"descriptor": "Canada", "id": "nv-ca", "count": 4},
            {"descriptor": "China", "id": "nv-cn", "count": 90},
        ]},
        {"facetParameter": "locations", "descriptor": "Sites", "values": [
            {"descriptor": "Canada, Toronto", "id": "nv-tor", "count": 2},
            {"descriptor": "Canada, Remote", "id": "nv-rem", "count": 2},
        ]},
    ]},
]

SALESFORCE_FACETS = [
    {
        "facetParameter": (
            "CF_-_REC_-_LRV_-_Job_Posting_Anchor_-_Country_from_Job_Posting_Location_Extended"
        ),
        "descriptor": "Country",
        "values": [{"descriptor": "Canada", "id": "sf-ca", "count": 37}],
    },
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "Canada - Toronto", "id": "sf-tor", "count": 20},
            {"descriptor": "Canada - Vancouver", "id": "sf-van", "count": 5},
            {"descriptor": "Arizona - Phoenix", "id": "sf-phx", "count": 9},
        ]},
    ]},
]

SERVICETITAN_FACETS = [
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "Canada British Columbia Remote", "id": "st-bc", "count": 1},
            {"descriptor": "Canada Ontario Remote", "id": "st-on", "count": 2},
            {"descriptor": "Chicago, IL", "id": "st-chi", "count": 4},
        ]},
    ]},
]


class TestFlattenFacets:
    def test_recurses_into_location_group(self):
        flat = workday.flatten_facets(ADOBE_FACETS)
        names = {f["facetParameter"] for f in flat}
        assert names == {"jobFamilyGroup", "locationCountry", "locations"}

    def test_marks_group_children_as_locations(self):
        flat = {f["facetParameter"]: f for f in workday.flatten_facets(ADOBE_FACETS)}
        assert flat["locationCountry"]["isLocation"]
        assert flat["locations"]["isLocation"]
        assert not flat["jobFamilyGroup"]["isLocation"]

    def test_top_level_custom_field_detected_by_descriptor(self):
        # Salesforce's country facet has an opaque name but a "Country" label.
        flat = {f["descriptor"]: f for f in workday.flatten_facets(SALESFORCE_FACETS)}
        assert flat["Country"]["isLocation"]

    def test_empty_input(self):
        assert workday.flatten_facets(None) == []
        assert workday.flatten_facets([]) == []


class TestResolveLocationFacet:
    def test_country_prefers_the_country_facet_over_many_cities(self):
        # "Canada" matches one country value and several city values; the
        # single precise value must win.
        assert workday.resolve_location_facet(ADOBE_FACETS, "Canada") == (
            "locationCountry",
            ["ca-id"],
        )

    def test_city_falls_through_to_the_locations_facet(self):
        assert workday.resolve_location_facet(ADOBE_FACETS, "Toronto") == (
            "locations",
            ["tor-id"],
        )

    def test_nvidia_hierarchy_naming(self):
        assert workday.resolve_location_facet(NVIDIA_FACETS, "Canada") == (
            "locationHierarchy1",
            ["nv-ca"],
        )

    def test_nvidia_comma_separated_city_value(self):
        assert workday.resolve_location_facet(NVIDIA_FACETS, "Toronto") == (
            "locations",
            ["nv-tor"],
        )

    def test_salesforce_custom_country_field(self):
        parameter, ids = workday.resolve_location_facet(SALESFORCE_FACETS, "Canada")
        assert parameter.startswith("CF_-_REC_-_LRV_")
        assert ids == ["sf-ca"]

    def test_salesforce_dash_separated_city_value(self):
        assert workday.resolve_location_facet(SALESFORCE_FACETS, "Toronto") == (
            "locations",
            ["sf-tor"],
        )

    def test_tenant_without_country_facet_uses_every_matching_city(self):
        # ServiceTitan has no country facet, so "Canada" must OR the city values.
        parameter, ids = workday.resolve_location_facet(SERVICETITAN_FACETS, "Canada")
        assert parameter == "locations"
        assert sorted(ids) == ["st-bc", "st-on"]

    def test_unmatched_location_returns_none(self):
        assert workday.resolve_location_facet(ADOBE_FACETS, "Narnia") is None

    def test_empty_location_returns_none(self):
        assert workday.resolve_location_facet(ADOBE_FACETS, "") is None

    def test_no_facets_returns_none(self):
        assert workday.resolve_location_facet([], "Canada") is None
