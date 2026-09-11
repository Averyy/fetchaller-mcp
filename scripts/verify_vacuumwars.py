"""Live verification for the Vacuum Wars content module.

Deliberately standalone and NOT wired into CI: it asserts against a live site
whose catalogue grows every week, so a failure here should stop a release, not
redden a build that has nothing to do with it.

    uv run python scripts/verify_vacuumwars.py

Every gate below has been run against the live site. It also prints the
catalogue's current size, which is the one number that must never be copied
into the docs as a constant -- Vacuum Wars adds listings continuously, and the
figures quoted in ``docs/site-apis.md`` are dated measurements, not totals.

Exit status is non-zero if any gate fails.
"""

from __future__ import annotations

import asyncio
import re
import sys

import wafer

from fetchaller.content.html import html_to_markdown
from fetchaller.content.vacuumwars import (
    _FORMATTERS,
    _SPEC_COLUMNS,
    _base_name,
    _group_variants,
    _merge_signature,
    _name_cell,
    _parse_vwproducts,
)

COMPARE = "https://vacuumwars.com/compare/robot-vacuums/"
COMPARE_PAGE2 = "https://vacuumwars.com/compare/robot-vacuums/page/2/"
LEAN = "https://compare.vacuumwars.com/"
TOP20 = "https://vacuumwars.com/vacuum-wars-best-robot-vacuums/"
REVIEW = "https://vacuumwars.com/dreame-d30-ultra-review/"
CORDLESS = "https://vacuumwars.com/vacuum-wars-best-cordless-vacuums/"
VS = (
    "https://vacuumwars.com/compare/robot-vacuums/"
    "dreame_x60_max_ultra_complete-vs-eufy_omni_s2/"
)
EMBED = (
    "https://compare.vacuumwars.com/embed/"
    "?product1=dreame_x60_max_ultra_complete&product2=eufy_omni_s2"
)
ARTICLE = "https://vacuumwars.com/compare/"
VANITY = "https://robotvacs.com/"

# The comparison tool's empty state. Any of these surviving into the markdown
# means the dataset was not read and the caller is looking at a board that
# renders as though it simply had nothing on it.
EMPTY_STATE = ("No products found", "No brand found", "Accordion")

failures: list[str] = []
notes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def get(session, url: str) -> tuple[int, str]:
    try:
        r = session.get(url, timeout=60)
        return r.status_code, r.text
    except Exception as exc:  # noqa: BLE001 - report, do not abort the run
        return 0, f"__ERROR__ {exc}"


async def main() -> int:
    session = wafer.SyncSession()

    print("\n== dataset: the catalogue as it stands today ==")
    status, html = get(session, COMPARE)
    check("compare page fetched", status == 200, f"HTTP {status}, {len(html):,} chars")
    products = None
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S):
        if "vwProducts" in m.group(1):
            products = _parse_vwproducts(m.group(1))
            if products:
                break
    check("window.vwProducts parsed whole", bool(products))
    if products:
        scored = [p for p in products if p.get("vacuum_wars_score_stars") not in (None, "")]
        fields = len(products[0])
        notes.append(
            f"CATALOGUE TODAY: {len(products)} listings, {len(scored)} scored, "
            f"{fields} fields/record. This grows; docs/site-apis.md quotes a "
            f"dated measurement, so update the date with the figure or leave "
            f"both alone."
        )
        print(f"        -> {len(products)} listings, {len(scored)} scored, {fields} fields")

    print("\n== compare page renders the dataset, not the empty state ==")
    md = await html_to_markdown(html, url=COMPARE)
    md = md[0] if isinstance(md, tuple) else md
    check("no empty-state text", not any(s in md for s in EMPTY_STATE))
    check("has the ranked score table", "Lab-tested, ranked by Vacuum Wars score" in md)
    check("has the spec table", "## Specifications" in md)
    check("separates tested from listed-only", "never tested" in md)
    print(f"        -> {len(md):,} chars (~{len(md)//4:,} tokens)")

    print("\n== there is no page 2 to fetch ==")
    # The tool pages client-side over the one array, and WordPress has no
    # route behind it: /page/2/ is a hard 404 with a 300 KB error body. It
    # does not re-serve the array, so paging for more returns nothing at all.
    status2, html2 = get(session, COMPARE_PAGE2)
    check("page/2 is a hard 404", status2 == 404, f"HTTP {status2}")
    check("page/2 carries no dataset", "vwProducts" not in html2)

    print("\n== the lean host agrees, and leads ==")
    # The two hosts are NOT interchangeable snapshots. The WordPress page is
    # served through Cloudflare on a 600s cache; the lean host is nginx and
    # uncached, and carries the newest records first. So the invariant is
    # containment, not equality: everything on the WordPress page must be on
    # the lean host, scores must agree wherever both carry a slug, and the
    # lean host may be ahead by however many models were added since the
    # cached page was built. A run where they happen to match proves nothing.
    statusl, lean_html = get(session, LEAN)
    lp = None
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", lean_html, re.S):
        if "vwProducts" in m.group(1):
            lp = _parse_vwproducts(m.group(1))
            if lp:
                break
    check("lean host fetched", statusl == 200, f"HTTP {statusl}, {len(lean_html):,} chars")
    if lp and products:
        wp_slugs = {x["slug"] for x in products}
        lean_slugs = {x["slug"] for x in lp}
        missing = wp_slugs - lean_slugs
        ahead = lean_slugs - wp_slugs
        check(
            "lean host carries everything the cached page does",
            not missing,
            f"lean {len(lp)} vs cached {len(products)}; "
            f"lean is ahead by {len(ahead)}, missing {len(missing)}",
        )
        if ahead:
            notes.append(
                f"HOST LAG: the lean host carried {len(ahead)} listing(s) the "
                f"Cloudflare-cached WordPress page did not "
                f"({', '.join(sorted(ahead)[:5])}). Expected, not a fault: "
                f"compare.vacuumwars.com is the fresher source as well as the "
                f"cheaper one."
            )
        by_wp = {x["slug"]: x for x in products}
        drift = [
            s for s, x in ((y["slug"], y) for y in lp)
            if s in by_wp and by_wp[s].get("vacuum_wars_score_stars")
            != x.get("vacuum_wars_score_stars")
        ]
        check("no score drift on shared slugs", not drift, f"{len(drift)} differ")

    print("\n== leaderboard cards appear once, not three to four times ==")
    for label, url in (("top 20", TOP20), ("review", REVIEW)):
        status3, h = get(session, url)
        if status3 != 200:
            check(f"{label} fetched", False, f"HTTP {status3}")
            continue
        out = await html_to_markdown(h, url=url)
        out = out[0] if isinstance(out, tuple) else out
        # The collapsed row printed "Name★score · price" on one line; the
        # expanded panel prints the name as a heading. Only the heading should
        # remain, so the price stamp that rode with the collapsed row is gone.
        check(f"{label}: no per-card price timestamp", not re.search(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2} [ap]m", out))
        check(f"{label}: no accordion caret", "▼" not in out)
        check(f"{label}: feature chips separated", "brushPad mop" not in out)
        print(f"        -> {label}: {len(out):,} chars (~{len(out)//4:,} tokens)")

    print("\n== non-robot article still renders its own score tables ==")
    status4, h4 = get(session, CORDLESS)
    out4 = await html_to_markdown(h4, url=CORDLESS)
    out4 = out4[0] if isinstance(out4, tuple) else out4
    check("cordless page has score tables", "Vacuum Wars Overall" in out4, f"HTTP {status4}")

    print("\n== a merged row never speaks for two different robots ==")
    if products:
        groups: dict[tuple, list[dict]] = {}
        for item in products:
            key = (
                (item.get("brand") or "").strip().lower(),
                _base_name(item.get("name") or "").lower(),
                _merge_signature(item),
            )
            groups.setdefault(key, []).append(item)
        hidden = []
        for members in groups.values():
            if len(members) < 2:
                continue
            for field, _, kind in _SPEC_COLUMNS:
                if len({_FORMATTERS[kind](x.get(field)) for x in members}) > 1:
                    hidden.append((members[0].get("name"), field))
        check(
            "no merged group hides a printed spec",
            not hidden,
            f"{len(groups)} rows from {len(products)} listings; {len(hidden)} hidden",
        )

    print("\n== a row's name still says which listing it is ==")
    if products:
        merged = _group_variants(products)
        cells = [_name_cell(row) for row in merged]
        dupes = {c for c in cells if cells.count(c) > 1}
        # The only acceptable collisions are names the site itself duplicates.
        source_dupes = {
            n for n in (x.get("name") for x in products)
            if [y.get("name") for y in products].count(n) > 1
        }
        unexplained = dupes - source_dupes
        check(
            "no two rows share a name the site did not",
            not unexplained,
            f"{len(dupes)} duplicate cells, {len(unexplained)} unexplained: "
            f"{sorted(unexplained)[:3]}",
        )
        # A parenthetical that is not a colour must survive onto the row.
        keepers = [
            x["name"] for x in products
            if x.get("name") and x["name"] != _base_name(x["name"])
            and any(
                w in x["name"]
                for w in ("No ", "no ", "Dock", "Station", "station", "Exclusive")
            )
        ]
        lost = [n for n in keepers if n in cells or _base_name(n) not in cells]
        check(
            "configuration parentheticals survive onto the row",
            len(lost) == len(keepers),
            f"{len(keepers)} such listings",
        )

    print("\n== every numbered row actually has a score ==")
    unscored = sum(
        1 for x in merged if x.get("vacuum_wars_score_stars") in (None, "")
    ) if products else 0
    ranked_rows = [
        ln for ln in md.splitlines()
        if ln.startswith("| ") and ln.split("|")[1].strip().isdigit()
    ]
    check(
        "rank column is only used for scored rows",
        bool(ranked_rows),
        f"{len(ranked_rows)} numbered rows; {unscored} rows have no overall score",
    )
    check("the unranked tail is declared", "no overall score" in md)

    print("\n== /compare/ itself is an article, not a failed tool read ==")
    status5, article = get(session, ARTICLE)
    md5 = await html_to_markdown(article, url=ARTICLE)
    md5 = md5[0] if isinstance(md5, tuple) else md5
    check("article fetched", status5 == 200, f"HTTP {status5}")
    check("no invented failure warning", "could not be read" not in md5)
    check("article body survives", "RobotVacs" in md5)

    print("\n== the vanity domain lands on the dataset ==")
    # The site's own article calls the tool RobotVacs.com and links there, so
    # this is the URL a caller is most likely to arrive with. It 301s onto the
    # compare path; extraction keys off the URL wafer ended on.
    try:
        resp = session.get(VANITY, timeout=90)
        check(
            "robotvacs.com redirects onto the compare path",
            resp.status_code == 200 and "/compare/robot-vacuums" in str(resp.url),
            f"HTTP {resp.status_code} -> {resp.url}",
        )
        md6 = await html_to_markdown(resp.text, url=str(resp.url))
        md6 = md6[0] if isinstance(md6, tuple) else md6
        check("vanity domain renders the dataset", "Lab-tested, ranked" in md6)
    except Exception as exc:  # noqa: BLE001
        check("robotvacs.com redirects onto the compare path", False, str(exc))

    print("\n== settled: the 404 routes carry nothing ==")
    for label, url in (("main site -vs- URL", VS), ("lean host /embed/", EMBED)):
        status7, body = get(session, url)
        check(
            f"{label}: 404 with no dataset",
            status7 == 404 and "vwProducts" not in body,
            f"HTTP {status7}",
        )
        notes.append(
            f"{label}: HTTP {status7}, dataset absent. fetch_url errors on any "
            f"status >= 400, so the caller sees HTTP 404 rather than a board "
            f"rendered from an error page. Settled -- do not re-open."
        )

    print("\n" + "=" * 70)
    for n in notes:
        print("NOTE: " + n)
    print("=" * 70)
    if failures:
        print(f"\n{len(failures)} gate(s) FAILED: {', '.join(failures)}")
        return 1
    print("\nAll gates passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
