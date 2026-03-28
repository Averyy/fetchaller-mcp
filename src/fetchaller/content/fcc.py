"""FCC filing page cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_fcc,
fix_fcc_nav, extract_fcc_data, strip_fcc_junk, postprocess_fcc).

Handles fcc.report FCC ID pages. The site has a broken Bootstrap navbar
(unclosed navbar-collapse div) that causes lxml to nest all body content
inside <nav>, which the generic "nav" selector then removes. fix_fcc_nav()
repairs this before selector removal.

Data lives in three tables inside div.jumbotron:
  - Table 0: Frequency authorizations (app ID, freq range, date, status)
  - Table 1: Exhibits/documents (file name, type, date, PDF link)
  - Table 2: Application details (key-value pairs)
The app IDs are opaque base64 strings; frequency columns use float divs.
extract_fcc_data() rebuilds these as clean structured markers.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_FCC_REPORT_HOST_RE = re.compile(r"^(?:www\.)?fcc\.report$")
_FCC_GOV_HOST_RE = re.compile(r"^apps\.fcc\.gov$")


def is_fcc(url: str) -> bool:
    """Check if URL is an FCC filing page (fcc.report or fcc.gov EAS)."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if _FCC_REPORT_HOST_RE.match(hostname):
        return "/fcc-id/" in path or "/els/" in path or "/ibfs/" in path
    if _FCC_GOV_HOST_RE.match(hostname):
        return "/oetcf/eas/" in path
    return False


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST: list[str] = [
    # fcc.report: top navbar is handled by fix_fcc_nav
    # fcc.gov: legacy navigation chrome
    ".skip-link",
    "#skippagenav",
    "#skiptopnav",
    "#skipfilingoptions",
    "#skipreportingoptions",
    "#skipmiscsection",
    "#skipfooter",
]


# ---------------------------------------------------------------------------
# Nav fix (runs BEFORE CSS selectors fire)
# ---------------------------------------------------------------------------


def fix_fcc_nav(soup: BeautifulSoup) -> None:
    """Fix broken nav nesting in fcc.report HTML.

    fcc.report's Bootstrap navbar has an unclosed <div class="navbar-collapse">
    which causes lxml to absorb all body content into the <nav> element.
    This moves the content container back to body as a sibling of nav.
    """
    nav = soup.find("nav", class_="navbar")
    if not nav:
        return
    body = soup.find("body")
    if not body:
        return
    # In lxml's broken parse, div.container ends up inside the nav
    container = nav.find("div", class_="container")
    if container and container.find(class_="jumbotron"):
        # Move the container out of nav, placing it right after
        nav.insert_after(container)


# ---------------------------------------------------------------------------
# Data extraction (runs BEFORE CSS selectors fire)
# ---------------------------------------------------------------------------

_DATA_MARKER = "__FCC_DATA__"


def extract_fcc_data(soup: BeautifulSoup) -> None:
    """Extract structured FCC filing data and inject as a marker.

    Parses the three data tables in the jumbotron and rebuilds them as
    clean markdown text inside a marker div that survives markdownify.
    """
    jumbotron = soup.find(class_="jumbotron")
    if not jumbotron:
        return

    lines: list[str] = []

    # --- Header info ---
    h1 = jumbotron.find("h1")
    fcc_id = h1.get_text(strip=True) if h1 else ""
    if fcc_id:
        lines.append(f"# FCC ID {fcc_id}")
        lines.append("")

    # Applicant name from h2 tags (second h2 has "Company -ProductCode")
    h2s = jumbotron.find_all("h2")
    applicant = ""
    for h2 in h2s:
        text = h2.get_text(strip=True)
        if text and text != "FCC ID":
            # Format: "Apple Inc. -E3548A" → "Apple Inc."
            applicant = re.sub(r"\s*-\w+$", "", text).strip()
            break

    # Address from h4
    h4 = jumbotron.find("h4")
    address = h4.get_text(strip=True) if h4 else ""

    tables = jumbotron.find_all("table", class_="table")

    # --- Application details (Table 2: key-value pairs) ---
    details: dict[str, str] = {}
    if len(tables) >= 3:
        for row in tables[2].find_all("tr"):
            cells = row.find_all("td")
            if len(cells) == 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key and val:
                    details[key] = val

    equipment = details.get("Equipment", "")
    if applicant:
        lines.append(f"**Applicant:** {applicant}")
    if equipment:
        lines.append(f"**Equipment:** {equipment}")
    if address:
        lines.append(f"**Address:** {address}")
    lines.append("")

    # --- Application details table ---
    # Show key fields (skip ones already shown in header)
    header_keys = {"Equipment", "Applicant Business"}
    detail_lines = []
    for key, val in details.items():
        if key in header_keys:
            continue
        # Truncate very long values
        if len(val) > 120:
            val = val[:117] + "..."
        detail_lines.append(f"| {key} | {val} |")

    if detail_lines:
        lines.append("## Application Details")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        lines.extend(detail_lines)
        lines.append("")

    # --- Frequency authorizations (Table 0) ---
    if tables:
        freq_lines = []
        for row in tables[0].find_all("tr")[1:]:  # skip header
            divs = row.find_all("div")
            # Structure: [app_id, freq_range, date, status]
            if len(divs) >= 4:
                freq_range = divs[1].get_text(strip=True)
                date = divs[2].get_text(strip=True)
                status = divs[3].get_text(strip=True)
                freq_lines.append(f"| {freq_range} | {date} | {status} |")

        if freq_lines:
            lines.append("## Frequency Authorizations")
            lines.append("")
            lines.append("| Frequency Range (MHz) | Action Date | Status |")
            lines.append("|---|---|---|")
            lines.extend(freq_lines)
            lines.append("")

    # --- Exhibits / Documents (Table 1) ---
    if len(tables) >= 2:
        exhibit_lines = []
        for row in tables[1].find_all("tr")[1:]:  # skip header
            cells = row.find_all("td")
            if len(cells) >= 3:
                name = cells[0].get_text(strip=True)
                doc_type = cells[1].get_text(strip=True)
                date = cells[2].get_text(strip=True)
                # Strip time portion from date
                date = re.sub(r"\s+\d{2}:\d{2}:\d{2}$", "", date)
                # Find PDF link
                pdf_link = ""
                for a in cells[-1].find_all("a"):
                    href = a.get("href", "")
                    if href.endswith(".pdf"):
                        pdf_link = href
                        if not pdf_link.startswith("http"):
                            pdf_link = f"https://fcc.report{pdf_link}"
                        break
                if pdf_link:
                    exhibit_lines.append(
                        f"| [{name}]({pdf_link}) | {doc_type} | {date} |"
                    )
                else:
                    exhibit_lines.append(f"| {name} | {doc_type} | {date} |")

        if exhibit_lines:
            lines.append("## Exhibits & Documents")
            lines.append("")
            lines.append("| File Name | Document Type | Date |")
            lines.append("|---|---|---|")
            lines.extend(exhibit_lines)
            lines.append("")

    if len(lines) <= 2:
        return  # Nothing useful extracted

    body = soup.find("body")
    if body is not None:
        marker = soup.new_tag("div", id="fcc-data-marker")
        marker.string = _DATA_MARKER + "\n".join(lines) + _DATA_MARKER
        body.insert(0, marker)


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_fcc_junk(soup: BeautifulSoup) -> None:
    """Remove FCC-specific junk that CSS selectors can't easily target."""
    # Remove the jumbotron (data already extracted into marker)
    for el in soup.find_all(class_="jumbotron"):
        el.decompose()

    # Remove all <input> elements
    for el in list(soup.find_all("input")):
        el.decompose()

    # Remove tracking pixels
    for img in list(soup.find_all("img")):
        width = img.get("width", "")
        height = img.get("height", "")
        if width in ("0", "1") or height in ("0", "1"):
            img.decompose()

    # Remove the fcc.report copyright footer
    for el in list(soup.find_all("small")):
        if "not affiliated" in el.get_text():
            # Also remove the sibling text and <hr> around it
            parent = el.parent
            if isinstance(parent, Tag):
                for sibling in list(parent.children):
                    if isinstance(sibling, Tag) and sibling.name in ("hr", "br"):
                        sibling.decompose()
            el.decompose()

    # fcc.gov: remove the left sidebar navigation column
    for el in list(soup.find_all("td")):
        text = el.get_text(strip=True)
        if "OET Home Page" in text and "Filing Options" in text:
            el.decompose()


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_DATA_MARKER_RE = re.compile(
    r"__FCC_DATA__([\s\S]*?)__FCC_DATA__\n*"
)

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # fcc.report boilerplate
    (re.compile(r"(?:^|\n)©\s*\d{4}\s*FCC\.report(?=\n|$)"), "\n"),
    (re.compile(
        r"(?:^|\n)This site is not affiliated with or endorsed by the FCC(?=\n|$)"
    ), "\n"),

    # fcc.gov navigation chrome
    (re.compile(r"(?:^|\n)\[?Skip (?:Primary )?FCC[^\]]*\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[?Skip the EAS[^\]]*\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Office of Engineering and Technology(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)FCC > FCC E-filing > EAS > Search(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[?FCC Site Map\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # fcc.gov sidebar links that survive selector removal
    (re.compile(r"(?:^|\n)-?\s*\[?Filing Options\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Grantee Registration\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Modify Grantee Information\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Authorization Search\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Grantee Search\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Pending Application Status\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Equipment Class/Rule Part List\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Knowledge Database\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Get FRN\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Measurement Procedures\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # fcc.gov footer
    (re.compile(
        r"(?:^|\n)Please use the Submit Inquiry link at[^\n]*(?=\n|$)"
    ), "\n"),
    (re.compile(r"(?:^|\n)\[?Skip FCC Footer[^\]]*\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Federal Communications Commission(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\d+ L Street NE(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Washington, DC \d+(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[?More FCC Contact Information[^\]]*\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Phone: \d+-\w+-FCC[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)TTY: \d+-\w+-FCC[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Fax: [\d-]+(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)E-mail: \[?fccinfo@fcc\.gov\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Privacy Policy\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Web Policies & Notices\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Customer Service Standards\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Freedom of Information Act\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # "Perform Search Again" link
    (re.compile(r"(?:^|\n)\[?Perform Search Again\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # Bare horizontal rules
    (re.compile(r"(?:^|\n)---(?=\n|$)"), "\n"),
]

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_fcc(markdown: str) -> str:
    """Clean up FCC-specific markdown noise."""
    # Extract and use the structured data marker
    m = _DATA_MARKER_RE.search(markdown)
    if m:
        data_content = m.group(1).strip()
        # Replace entire markdown with just the structured data
        # (the jumbotron was decomposed, so there's nothing else useful)
        markdown = data_content
    else:
        # No marker — apply pattern cleanup on raw markdown (fcc.gov fallback)
        for pattern, replacement in _POSTPROCESS_PATTERNS:
            markdown = pattern.sub(replacement, markdown)

    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
