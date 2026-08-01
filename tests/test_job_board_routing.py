"""fetch() routing for the big-tech career boards.

Each of these boards is an SPA whose HTML carries no postings, so a URL that
fails to route here silently degrades to an empty page rather than erroring.
The negative cases matter as much as the positive ones: amazon.jobs must not
be captured by the retail post-processor, and uber.com is mostly a consumer
site where only the careers paths qualify.
"""

import pytest

from fetchaller.tools.fetch import _match_job_board_url

ROUTED = [
    ("https://apply.careers.microsoft.com/careers/job/1970393556750546", "eightfold_job"),
    ("https://apply.careers.microsoft.com/careers?query=engineer", "eightfold_board"),
    ("https://explore.jobs.netflix.net/careers/job/790316470001", "eightfold_job"),
    ("https://paypal.eightfold.ai/careers", "eightfold_board"),
    ("https://jobs.apple.com/en-ca/details/200674861/staff-ml-engineer", "apple_job"),
    ("https://jobs.apple.com/en-ca/search?search=engineer", "apple_search"),
    ("https://www.metacareers.com/jobs/733224206480023/", "meta_job"),
    ("https://www.metacareers.com/jobs", "meta_search"),
    ("https://www.amazon.jobs/en/jobs/10471950/art-director", "amazon_job"),
    ("https://www.amazon.jobs/en/search", "amazon_search"),
    ("https://www.uber.com/global/en/careers/list/302805/", "uber_job"),
    (
        "https://www.google.com/about/careers/applications/jobs/results/92025237427626694",
        "google_job",
    ),
    ("https://www.google.com/about/careers/applications/jobs/results?q=x", "google_search"),
    ("https://careers.oracle.com/en/sites/jobsearch/job/338925", "oracle_job"),
    ("https://careers.oracle.com/en/sites/jobsearch/jobs", "oracle_search"),
    ("https://www.uber.com/us/en/careers/list/", "uber_search"),
]

NOT_ROUTED = [
    # Retail Amazon — belongs to the shopping post-processor.
    "https://www.amazon.ca/dp/B01234567",
    "https://www.amazon.com/s?k=laptop",
    # Consumer Uber.
    "https://www.uber.com/ca/en/ride/",
    # google.com is mostly not a job board.
    "https://www.google.com/",
    "https://www.google.com/search?q=jobs",
    "https://www.google.com/about/",
    # Unrelated career pages with no client.
    "https://example.com/careers",
    "https://boards.greenhouse.io/acme",
    # Workday keeps its own existing routing.
    "https://adobe.wd5.myworkdayjobs.com/external_experienced",
    # Incomplete paths.
    "https://jobs.apple.com/en-ca/details/",
    "https://www.metacareers.com/",
]


@pytest.mark.parametrize(("url", "kind"), ROUTED)
def test_routes_board_urls(url, kind):
    match = _match_job_board_url(url)
    assert match is not None, url
    assert match[1] == kind


@pytest.mark.parametrize("url", NOT_ROUTED)
def test_leaves_other_urls_alone(url):
    assert _match_job_board_url(url) is None, url


def test_every_route_carries_a_human_label():
    for url, _kind in ROUTED:
        label = _match_job_board_url(url)[0]
        assert label and not label.startswith("_")


def test_posting_wins_over_board_for_the_same_host():
    # A selected posting on an Eightfold board is a posting, not the board.
    board = _match_job_board_url("https://apply.careers.microsoft.com/careers")
    posting = _match_job_board_url("https://apply.careers.microsoft.com/careers?pid=123456789")
    assert board[1] == "eightfold_board"
    assert posting[1] == "eightfold_job"
    assert posting[2]["position_id"] == "123456789"
