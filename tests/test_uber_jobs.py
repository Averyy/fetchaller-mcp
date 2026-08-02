"""jobs.uber.com URL detection and Oracle Recruiting delegation.

Uber's own endpoint was dropped in favour of Oracle Recruiting Cloud (it
returned an empty description for every posting), so the rendering tests now
live in test_oracle_recruiting.py. What remains Uber-specific is the URL
grammar the fetch tool routes on.
"""

import inspect

from fetchaller.uber_jobs import search, url


class TestUrlDetection:
    def test_global_careers_list(self):
        u = "https://www.uber.com/global/en/careers/list/302805/"
        assert url.is_uber_job_url(u)
        assert url.extract_uber_job_id(u) == "302805"

    def test_regional_careers_list(self):
        assert url.extract_uber_job_id("https://www.uber.com/us/en/careers/list/302805") == (
            "302805"
        )

    def test_current_jobs_subdomain(self):
        # uber.com/careers/list/{id} now redirects here.
        assert url.extract_uber_job_id("https://jobs.uber.com/en/jobs/301041") == "301041"

    def test_list_index(self):
        assert url.is_uber_jobs_list_url("https://www.uber.com/us/en/careers/list/")
        assert not url.is_uber_job_url("https://www.uber.com/us/en/careers/list/")

    def test_the_redirect_target_is_also_a_list_url(self):
        # uber.com/{region}/{lang}/careers/list/ redirects to jobs.uber.com/{lang}/jobs/.
        # Without this, fetch() silently declined the very URL a user lands on.
        assert url.is_uber_jobs_list_url("https://jobs.uber.com/en/jobs/")
        assert url.is_uber_jobs_list_url("https://jobs.uber.com/en/jobs")
        assert not url.is_uber_job_url("https://jobs.uber.com/en/jobs/")

    def test_sibling_paths_on_the_jobs_host_are_not_the_list(self):
        # The board also serves saved-jobs, sitemap and people-stories; none is
        # a search result page and none should route to the Oracle client.
        for path in ("/en/jobs/saved-jobs/", "/en/sitemap/", "/en/people-stories/", "/en/"):
            assert not url.is_uber_jobs_list_url(f"https://jobs.uber.com{path}"), path

    def test_non_careers_uber_page_rejected(self):
        # uber.com is mostly a consumer site; only the careers paths qualify.
        assert not url.is_uber_job_url("https://www.uber.com/ca/en/ride/")
        assert not url.is_uber_jobs_list_url("https://www.uber.com/ca/en/ride/")

    def test_other_host_rejected(self):
        assert not url.is_uber_job_url("https://example.com/global/en/careers/list/302805/")


class TestDelegation:
    def test_targets_the_uber_oracle_tenant(self):
        from fetchaller.oracle_recruiting.employers import KNOWN_EMPLOYERS

        assert search.EMPLOYER in KNOWN_EMPLOYERS

    def test_search_signature_is_preserved(self):
        # The MCP tool passes these by keyword; a rename would break dispatch.
        params = inspect.signature(search.search_uber_jobs).parameters
        for name in ("title", "location", "strict_title", "strict_location", "limit"):
            assert name in params

    def test_get_signature_is_preserved(self):
        params = inspect.signature(search.get_uber_job).parameters
        assert "job_id" in params
