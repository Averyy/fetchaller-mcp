"""Live verification for the Vacuum Wars content module.

Deliberately standalone and NOT wired into CI. The branch that added the
Vacuum Wars module was built in an environment whose egress policy blocked
vacuumwars.com, so every gate below was exercised against captured pages and
none of it has ever run against the live site. Run this from a machine that
can reach the site before trusting the module:

    uv run python scripts/verify_vacuumwars.py

It also answers the two questions that environment could not:

  * the dataset's true size -- every count taken from inside that environment
    was a floor, because the payload exceeds the fetch tool's ceiling and both
    hosts ignore ``Range``; here the whole page is in hand, so the real total
    is printed and should be compared against what the docs claim;
  * whether the reverse-soft-404 routes (the main site's ``-vs-`` URLs and
    ``compare.vacuumwars.com/embed/``) actually carry the dataset in their
    404 bodies. If they do, they are worth handling; if not, the current
    decision to leave them erroring is correct and should be written down.

Exit status is non-zero if any gate fails.
"""

from __future__ import annotations

import asyncio
import re
import sys

import wafer

from fetchaller.content.html import html_to_markdown
from fetchaller.content.vacuumwars import _parse_vwproducts

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

    print("\n== dataset: true size (the figure the docs could only floor) ==")
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
            f"TRUE TOTALS: {len(products)} listings, {len(scored)} scored, "
            f"{fields} fields/record. Compare against docs/site-apis.md, which "
            f"says 'over 420 listings / 350 models'."
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

    print("\n== page/2 re-serves the same payload (never fetch it for more) ==")
    status2, html2 = get(session, COMPARE_PAGE2)
    p2 = None
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html2, re.S):
        if "vwProducts" in m.group(1):
            p2 = _parse_vwproducts(m.group(1))
            if p2:
                break
    check(
        "page/2 payload identical to page 1",
        bool(products) and bool(p2) and len(p2) == len(products),
        f"HTTP {status2}, {len(p2 or [])} vs {len(products or [])} listings",
    )

    print("\n== the lean host serves the same data ==")
    statusl, lean_html = get(session, LEAN)
    lp = None
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", lean_html, re.S):
        if "vwProducts" in m.group(1):
            lp = _parse_vwproducts(m.group(1))
            if lp:
                break
    check("lean host fetched", statusl == 200, f"HTTP {statusl}, {len(lean_html):,} chars")
    check("same listing count", bool(lp) and bool(products) and len(lp) == len(products))
    if lp and products:
        by_wp = {p["slug"]: p for p in products}
        drift = [
            s for s, p in ((x["slug"], x) for x in lp)
            if s in by_wp and by_wp[s].get("vacuum_wars_score_stars")
            != p.get("vacuum_wars_score_stars")
        ]
        check("no score drift between hosts", not drift, f"{len(drift)} differ")

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

    print("\n== open question: do the 404 routes carry the dataset? ==")
    for label, url in (("main site -vs- URL", VS), ("lean host /embed/", EMBED)):
        status5, body = get(session, url)
        has = "vwProducts" in body
        parsed = None
        if has:
            for m in re.finditer(r"<script[^>]*>(.*?)</script>", body, re.S):
                if "vwProducts" in m.group(1):
                    parsed = _parse_vwproducts(m.group(1))
                    if parsed:
                        break
        print(f"  INFO  {label}: HTTP {status5}, vwProducts present={has}, "
              f"parsed={len(parsed) if parsed else 0}")
        notes.append(
            f"{label}: HTTP {status5}, dataset {'PRESENT' if parsed else 'absent'}"
            f"{' (' + str(len(parsed)) + ' listings)' if parsed else ''}. "
            f"{'Worth handling the 404 body.' if parsed else 'Leaving it erroring is correct.'}"
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
