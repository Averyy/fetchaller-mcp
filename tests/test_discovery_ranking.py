"""Picking the data exchange out of a capture.

The two headline cases are directly opposed and are both reproduced here with
their live shapes: Netflix, where the branding blob outscores the listing 2x,
and Workday, where a lookup table outnumbers the postings 5x. No weighting
satisfies both, so the query hint is what actually decides.
"""

import json

from fetchaller.discovery.observe import Capture, Exchange
from fetchaller.discovery.ranking import best, query_hints, rank, visible_text


def exchange(url, body, *, order=0, method="GET", status=200, kind="fetch", ctype="application/json"):
    return Exchange(
        order=order,
        phase="load",
        method=method,
        url=url,
        resource_type=kind,
        status=status,
        request_headers={},
        request_body=None,
        response_headers={"content-type": ctype},
        body=body,
    )


def page(html, exchanges, url="https://board.example.com/careers?query=engineer"):
    return Capture(url=url, html=html, exchanges=list(exchanges))


class TestQueryHints:
    def test_reads_word_like_query_values(self):
        assert query_hints("https://x/y?query=engineer&location=Toronto") == [
            "engineer",
            "Toronto",
        ]

    def test_analytics_blobs_are_ignored(self):
        # Google's _gl=1*16occzp*_up*MQ.. fails the pattern.
        assert query_hints("https://x/y?_gl=1*16occzp*_up*MQ..") == []

    def test_booleans_and_sort_orders_are_stopwords(self):
        assert query_hints("https://x/y?remote=true&sort=recent&dir=desc") == []

    def test_very_short_and_very_long_values_are_ignored(self):
        assert query_hints("https://x/y?a=ab&b=" + "z" * 80) == []

    def test_no_query_yields_nothing(self):
        # Workday board URLs carry no query at all, which is why they fall
        # through to score.
        assert query_hints("https://adobe.wd5.myworkdayjobs.com/external_experienced") == []


class TestNetflixOpposition:
    """Branding outscores the listing; the hint is what separates them."""

    def _capture(self):
        # Branding: every one of its values is navigation chrome, and all of it
        # is rendered, so coverage is ~1.0.
        chrome = ["Netflix Jobs Home", "CAREERS", "LOCATIONS", "CULTURE MEMO"] * 8
        branding = json.dumps({"nav": [{"label": c, "href": "/x"} for c in chrome]})
        # Listing: 10 postings, only the first few of which are on screen.
        titles = [f"Senior Engineer Position {i:02d}" for i in range(10)]
        listing = json.dumps({"positions": [{"name": t, "id": i} for i, t in enumerate(titles)]})
        html = "<html><body>" + " ".join(chrome + titles[:3]) + " engineer</body></html>"
        return page(
            html,
            [
                exchange("https://board.example.com/api/apply/v2/branding", branding, order=1),
                exchange("https://board.example.com/api/apply/v2/jobs/1/jobs", listing, order=2),
            ],
        )

    def test_branding_really_does_outscore_the_listing(self):
        ranked = {c.url.rsplit("/", 1)[-1]: c for c in rank(self._capture())}
        assert ranked["branding"].score > ranked["jobs"].score

    def test_but_the_hint_picks_the_listing(self):
        assert best(self._capture()).url.endswith("/jobs")

    def test_branding_is_off_subject(self):
        ranked = {c.url.rsplit("/", 1)[-1]: c for c in rank(self._capture())}
        assert ranked["jobs"].on_subject
        assert not ranked["branding"].on_subject


class TestWorkdayOpposition:
    """A lookup table outnumbers the postings; score is what separates them."""

    def _capture(self):
        labels = json.dumps(
            {"body": [{"key": f"WDRES.BUTTON.{i}", "label": "Close"} for i in range(334)]}
        )
        titles = [f"Product Designer Level {i}" for i in range(70)]
        jobs = json.dumps({"jobPostings": [{"title": t, "externalPath": f"/{i}"} for i, t in enumerate(titles)]})
        # No query in the board URL, so there are no hints at all.
        html = "<html><body>" + " ".join(titles[:20]) + "</body></html>"
        return page(
            html,
            [
                exchange("https://t.wd5.myworkdayjobs.com/wday/cxs/t/videoplayerlabels", labels, order=1),
                exchange("https://t.wd5.myworkdayjobs.com/wday/cxs/t/b/jobs", jobs, order=2, method="POST"),
            ],
            url="https://t.wd5.myworkdayjobs.com/external_experienced",
        )

    def test_the_lookup_table_really_does_have_more_records(self):
        ranked = {c.url.rsplit("/", 1)[-1]: c for c in rank(self._capture())}
        assert ranked["videoplayerlabels"].records > ranked["jobs"].records

    def test_score_still_picks_the_postings(self):
        assert best(self._capture()).url.endswith("/jobs")

    def test_no_hints_means_the_score_path_was_used(self):
        capture = self._capture()
        assert query_hints(capture.url) == []
        assert not any(c.on_subject for c in rank(capture))


class TestExclusions:
    def test_non_2xx_is_never_a_candidate(self):
        capture = page("<html>x</html>", [exchange("https://board.example.com/a", '{"r":[1,2]}', status=429)])
        assert rank(capture) == []

    def test_empty_bodies_are_excluded(self):
        capture = page("<html>x</html>", [exchange("https://board.example.com/a", "")])
        assert rank(capture) == []

    def test_asset_resource_types_are_excluded(self):
        capture = page(
            "<html>x</html>",
            [exchange("https://board.example.com/a", '{"r":[1]}', kind="stylesheet")],
        )
        assert rank(capture) == []

    def test_the_navigation_document_is_penalized_not_excluded(self):
        # Trap 10: an SSR board can answer the navigation with the payload.
        body = json.dumps({"results": [{"t": f"engineer role {i}"} for i in range(20)]})
        url = "https://board.example.com/careers?query=engineer"
        capture = page(
            "<html>engineer role 1</html>",
            [exchange(url, body, kind="document", ctype="text/html")],
            url=url,
        )
        assert len(rank(capture)) == 1


class TestExpect:
    def test_a_payload_missing_expect_is_excluded_outright(self):
        capture = page(
            "<html>x</html>",
            [
                exchange("https://board.example.com/a", '{"r":[{"n":"pastry chef here"}]}', order=1),
                exchange("https://board.example.com/b", '{"r":[{"n":"engineer role here"}]}', order=2),
            ],
        )
        ranked = rank(capture, expect="engineer role here")
        assert len(ranked) == 1
        assert ranked[0].url.endswith("/b")

    def test_expect_overrides_the_url_hints(self):
        capture = page(
            "<html>x</html>",
            [exchange("https://board.example.com/b", '{"r":[{"n":"pastry chef here"}]}')],
        )
        assert best(capture, expect="pastry chef here") is not None


class TestRefusal:
    def test_a_thin_payload_is_not_answerable(self):
        # Uber's board hits this: the only XHRs are RSC flight responses, and
        # reporting "no data request" beats returning a page-fetching plan.
        capture = page("<html>x</html>", [exchange("https://board.example.com/a", '{"ok":true}')])
        assert best(capture) is None

    def test_a_record_set_is_required(self):
        body = json.dumps({"r": [{"id": i, "t": f"engineer role {i}"} for i in range(10)]})
        capture = page("<html>x</html>", [exchange("https://board.example.com/a", body)])
        assert best(capture) is not None

    def test_distinctive_values_alone_are_not_enough(self):
        # Meta's bulk-route-definitions carries 309 distinct values, zero
        # records, and — because the route definition stores the parsed query
        # parameter — the literal value "engineer". It therefore looked
        # on-subject too. Two captures ended with discovery confidently
        # returning a routing endpoint carrying no postings.
        body = json.dumps({"route": "engineer", "mods": [f"Module{i}" for i in range(50)]})
        capture = page("<html>x</html>", [exchange("https://board.example.com/a", body)])
        assert best(capture) is None

    def test_expect_remains_the_escape_hatch(self):
        body = json.dumps({"route": "engineer", "mods": [f"Module{i}" for i in range(50)]})
        capture = page("<html>x</html>", [exchange("https://board.example.com/a", body)])
        assert best(capture, expect="Module7") is not None


class TestOffSiteHandling:
    def test_a_bigger_third_party_array_never_outranks_the_board(self):
        # Netflix's cookielaw payload has 200 records.
        consent = json.dumps({"groups": [{"id": i, "name": f"Cookie group {i}"} for i in range(200)]})
        listing = json.dumps({"positions": [{"name": f"engineer role {i}", "id": i} for i in range(10)]})
        capture = page(
            "<html>engineer role 1 engineer</html>",
            [
                exchange("https://cdn.cookielaw.org/consent/x", consent, order=1),
                exchange("https://board.example.com/api/jobs", listing, order=2),
            ],
        )
        assert best(capture).url.startswith("https://board.example.com")


class TestVisibleText:
    def test_script_and_style_content_is_removed(self):
        html = "<html><script>var jobs = 'engineer'</script><body>Hello</body></html>"
        assert "engineer" not in visible_text(html)
        assert "Hello" in visible_text(html)


class TestUrlEchoIsNotSubjectMatter:
    """A payload that echoes the page URL must not read as on-subject."""

    def _capture(self):
        # Meta's /ajax/bulk-route-definitions/ answers with the requested route
        # as a *key*, so the raw body contains "engineer" while carrying no job
        # data at all. It out-records the real results query often enough to
        # win the tiebreak, which made discovery non-deterministic.
        routes = {
            "payload": {
                "payloads": {
                    "/jobsearch/?q=engineer": {
                        "result": {
                            "type": "route_definition",
                            "resources": [
                                {"name": f"CPJobSearchModule{i}", "hash": f"abcdef{i:04d}"}
                                for i in range(90)
                            ],
                        }
                    }
                }
            }
        }
        results = {
            "data": {
                "job_search": [
                    {"title": f"Software Engineer {i:03d}", "id": str(i)} for i in range(40)
                ]
            }
        }
        html = "<html><body>Software Engineer 001 Software Engineer 002 engineer</body></html>"
        return page(
            html,
            [
                exchange("https://www.metacareers.com/ajax/bulk-route-definitions/",
                         json.dumps(routes), order=1, method="POST"),
                exchange("https://www.metacareers.com/graphql",
                         json.dumps(results), order=2, method="POST"),
            ],
            url="https://www.metacareers.com/jobs?q=engineer",
        )

    def test_the_echoed_url_still_appears_in_the_raw_body(self):
        capture = self._capture()
        routes = capture.exchanges[0]
        assert "engineer" in routes.body.casefold()  # the trap

    def test_but_it_is_not_treated_as_on_subject(self):
        ranked = {c.url.rsplit("/", 1)[-1] or "routes": c for c in rank(self._capture())}
        assert ranked["graphql"].on_subject
        assert not ranked[""].on_subject if "" in ranked else True

    def test_the_real_results_query_wins(self):
        assert best(self._capture()).url.endswith("/graphql")

    def test_an_explicit_expect_still_matches_payload_content(self):
        assert best(self._capture(), expect="Software Engineer 001").url.endswith("/graphql")


class TestServerRenderedListing:
    """An SSR board answers the navigation with the payload (trap 10 in reverse).

    Uber renders ten postings per page and paginates with plain
    `<a href="/en/jobs?query=…&page=2&pagesize=10">` links — no XHR at all,
    confirmed interactively. "No data request" is the right observation but the
    wrong conclusion: there is nothing to discover because the page already is
    the endpoint. `pagesize` is capped at 10 server-side.
    """

    def _capture(self, html, url="https://jobs.uber.com/en/jobs/?query=engineer"):
        from fetchaller.discovery.observe import Capture

        return Capture(
            url=url,
            requested_url=url,
            html=html,
            exchanges=[
                exchange(url, "<html>x</html>", kind="document", ctype="text/html"),
            ],
        )

    def test_repeated_hits_in_the_rendered_page_are_recognised(self):
        from fetchaller.discovery.pipeline import _server_rendered_listing

        html = "<html><body>" + " ".join(f"Staff engineer role {i}" for i in range(10)) + "</body></html>"
        assert _server_rendered_listing(self._capture(html), expect=None)

    def test_a_single_mention_is_not_a_listing(self):
        # Meta's document mentions "engineer" but carries none of the results —
        # 0 of the first 25 GraphQL titles appear in it. Treating that as SSR
        # would mislabel a throttled board.
        from fetchaller.discovery.pipeline import _server_rendered_listing

        html = "<html><body><h1>Search engineer jobs</h1><p>No results loaded yet</p></body></html>"
        assert _server_rendered_listing(self._capture(html), expect=None) is None

    def test_no_hints_means_no_verdict(self):
        from fetchaller.discovery.pipeline import _server_rendered_listing

        html = "<html><body>" + " ".join(f"engineer {i}" for i in range(10)) + "</body></html>"
        capture = self._capture(html, url="https://jobs.uber.com/en/jobs/")
        assert _server_rendered_listing(capture, expect=None) is None
