"""jobs.uber.com URL detection, count unwrapping, and rendering."""

from fetchaller.uber_jobs import api, search, url

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_global_careers_list(self):
        u = "https://www.uber.com/global/en/careers/list/302805/"
        assert url.is_uber_job_url(u)
        assert url.extract_uber_job_id(u) == "302805"

    def test_regional_careers_list(self):
        assert url.extract_uber_job_id("https://www.uber.com/us/en/careers/list/302805") == (
            "302805"
        )

    def test_jobs_subdomain(self):
        assert url.extract_uber_job_id("https://jobs.uber.com/en/jobs/302805") == "302805"

    def test_list_index(self):
        assert url.is_uber_jobs_list_url("https://www.uber.com/us/en/careers/list/")
        assert not url.is_uber_job_url("https://www.uber.com/us/en/careers/list/")

    def test_non_careers_uber_page_rejected(self):
        # uber.com is mostly a consumer site; only the careers paths qualify.
        assert not url.is_uber_job_url("https://www.uber.com/ca/en/ride/")
        assert not url.is_uber_jobs_list_url("https://www.uber.com/ca/en/ride/")

    def test_other_host_rejected(self):
        assert not url.is_uber_job_url("https://example.com/global/en/careers/list/302805/")


class TestTotalUnwrapping:
    def test_long_shape(self):
        # Uber returns counts as a Long triple, not a number.
        assert api._total({"totalResults": {"low": 11, "high": 0, "unsigned": False}}) == 11

    def test_plain_number(self):
        assert api._total({"totalResults": 638}) == 638

    def test_missing(self):
        assert api._total({}) == 0


class TestBuildLocation:
    def test_country_only(self):
        assert api.build_location(country="CAN") == [{"country": "CAN"}]

    def test_country_and_city(self):
        assert api.build_location(country="CAN", city="Toronto") == [
            {"country": "CAN", "city": "Toronto"}
        ]

    def test_empty_means_anywhere(self):
        assert api.build_location() == []


class TestSplitLocation:
    def test_city_and_country(self):
        assert search._split_location("Toronto, Canada") == ("CAN", "Toronto")

    def test_country_only(self):
        assert search._split_location("Canada") == ("CAN", "")

    def test_city_only(self):
        assert search._split_location("Toronto") == ("", "Toronto")

    def test_empty(self):
        assert search._split_location("") == ("", "")


class TestLocationText:
    def test_formats_city_region_country(self):
        job = {"allLocations": [{"city": "Toronto", "region": "Ontario", "countryName": "Canada"}]}
        assert search._location_text(job) == "Toronto, Ontario, Canada"

    def test_multiple_locations_joined(self):
        job = {
            "allLocations": [
                {"city": "New York", "region": "New York", "countryName": "United States"},
                {"city": "San Francisco", "region": "San Francisco", "countryName": "United States"},
            ]
        }
        assert " · " in search._location_text(job)

    def test_all_null_fields_are_labelled_not_dropped(self):
        # Uber ships null locations for remote/unspecified reqs; an empty
        # string would read as a parsing failure.
        job = {"allLocations": [{"city": None, "region": None, "countryName": None}]}
        assert search._location_text(job) == "Not specified"

    def test_no_locations_at_all(self):
        assert search._location_text({}) == ""


class TestRender:
    def test_search_results(self):
        job = {
            "id": 302805,
            "title": "Senior Product Designer",
            "department": "Product - Product Design",
            "level": "5",
            "allLocations": [{"city": "Toronto", "region": "Ontario", "countryName": "Canada"}],
        }
        out = search._render_results(
            [job], title="designer", location="Toronto", total=3,
            title_filtered=0, location_filtered=0,
        )
        assert "# Uber jobs" in out
        assert "https://www.uber.com/global/en/careers/list/302805/" in out
        assert "**Department**: Product - Product Design" in out

    def test_drop_counts_reported(self):
        out = search._render_results(
            [], title="designer", location="Toronto", total=10,
            title_filtered=7, location_filtered=3,
        )
        assert "dropped 7 by title and 3 by location" in out
