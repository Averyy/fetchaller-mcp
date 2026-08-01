"""Markdown rendering for Eightfold positions."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from markdownify import markdownify

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")
_MAX_FIELD_CHARS = 300

# Tenant-defined extras. Eightfold namespaces them `efcustomText*`; the suffix
# is the employer's own label, so it is un-camel-cased rather than mapped.
_CUSTOM_PREFIX = "efcustomText"
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _clean(value, limit: int = _MAX_FIELD_CHARS) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_clean(v, limit) for v in value]
        return " · ".join(p for p in parts if p)
    text = " ".join(str(value).split())
    if not text:
        return ""
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _posted(position: dict) -> str:
    for key in ("postedTs", "creationTs"):
        ts = position.get(key)
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                continue
    return ""


def _location(position: dict) -> str:
    # `standardizedLocations` is the normalised "Vancouver, BC, CA" form;
    # `locations` is the employer's own free text. Prefer the former.
    for key in ("standardizedLocations", "locations"):
        value = position.get(key)
        if isinstance(value, list) and value:
            return _clean(value)
    return _clean(position.get("location"))


def posting_url(position: dict, board_root: str) -> str:
    """Absolute link to a posting, preferring the tenant's own public URL."""
    public = position.get("publicUrl")
    if isinstance(public, str) and public.startswith("http"):
        return public
    path = position.get("positionUrl")
    if isinstance(path, str) and path:
        if path.startswith("http"):
            return path
        return f"{board_root.rstrip('/')}/{path.lstrip('/')}"
    position_id = position.get("id")
    if position_id:
        return f"{board_root.rstrip('/')}/careers/job/{position_id}"
    return ""


def _description_markdown(html: str) -> str:
    if not html:
        return ""
    md = markdownify(
        html,
        heading_style="ATX",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
    )
    return _BLANK_LINE_COLLAPSE_RE.sub("\n\n", md).strip()


def render_search_results(
    positions: list[dict],
    *,
    employer: str,
    board_root: str,
    query: str = "",
    location: str = "",
    total: int = 0,
    title_filtered: int = 0,
) -> str:
    scope = " · ".join(
        part
        for part in (
            f"“{_clean(query)}”" if query else "",
            _clean(location),
        )
        if part
    )
    lines = [f"# {_clean(employer)} jobs{': ' + scope if scope else ''}", ""]

    plural = "" if len(positions) == 1 else "s"
    counts = f"_{len(positions)} job{plural} shown"
    if total and total > len(positions):
        counts += f" of {total} matching"
    if title_filtered:
        counts += f"; {title_filtered} dropped by the title filter"
    lines.append(counts + "_")
    lines.append("")

    if not positions:
        lines.append("No postings matched.")
        return "\n".join(lines) + "\n"

    for index, position in enumerate(positions, start=1):
        name = _clean(position.get("name")) or "Untitled posting"
        url = posting_url(position, board_root)
        lines.append(f"## {index}. {name}" if not url else f"## {index}. [{name}]({url})")

        meta: list[str] = []
        where = _location(position)
        if where:
            meta.append(f"- **Location**: {where}")
        for label, key in (
            ("Department", "department"),
            ("Work type", "workLocationOption"),
            ("Flexibility", "locationFlexibility"),
        ):
            value = _clean(position.get(key))
            if value:
                meta.append(f"- **{label}**: {value}")
        posted = _posted(position)
        if posted:
            meta.append(f"- **Posted**: {posted}")
        req = _clean(position.get("displayJobId") or position.get("atsJobId"))
        if req:
            meta.append(f"- **Req ID**: {req}")
        lines.extend(meta)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_position(
    position: dict,
    *,
    employer: str,
    board_root: str,
    source_url: str | None = None,
) -> str:
    name = _clean(position.get("name")) or "Job Posting"
    lines = [f"# {name}", "", f"- **Employer**: {_clean(employer)}"]

    where = _location(position)
    if where:
        lines.append(f"- **Location**: {where}")
    for label, key in (
        ("Department", "department"),
        ("Work type", "workLocationOption"),
        ("Flexibility", "locationFlexibility"),
    ):
        value = _clean(position.get(key))
        if value:
            lines.append(f"- **{label}**: {value}")
    posted = _posted(position)
    if posted:
        lines.append(f"- **Posted**: {posted}")
    req = _clean(position.get("displayJobId") or position.get("atsJobId"))
    if req:
        lines.append(f"- **Req ID**: {req}")

    for key, value in position.items():
        if not key.startswith(_CUSTOM_PREFIX):
            continue
        text = _clean(value)
        if not text:
            continue
        label = _CAMEL_SPLIT_RE.sub(" ", key[len(_CUSTOM_PREFIX) :]).strip()
        if label:
            lines.append(f"- **{label}**: {text}")

    lines.append("")
    description = _description_markdown(position.get("jobDescription") or "")
    if description:
        lines.append("## Description")
        lines.append("")
        lines.append(description)
        lines.append("")

    link = source_url or posting_url(position, board_root)
    if link:
        lines.append(f"**Source**: {link}")

    return "\n".join(lines).rstrip() + "\n"
