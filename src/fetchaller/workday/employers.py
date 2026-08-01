"""Employer aliases for Workday boards.

Only boards verified to answer are listed. A tenant absent here still works —
callers pass the board URL directly — so this is a convenience table, not a
gate. Each value is the public board URL, from which
``content.workday.extract_workday_board_params`` derives the tenant and site.
"""

from __future__ import annotations

KNOWN_EMPLOYERS: dict[str, str] = {
    "adobe": "https://adobe.wd5.myworkdayjobs.com/external_experienced",
    "autodesk": "https://autodesk.wd1.myworkdayjobs.com/Ext",
    "crowdstrike": "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers",
    "motorolasolutions": "https://motorolasolutions.wd5.myworkdayjobs.com/Careers",
    "nvidia": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "salesforce": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
    "servicetitan": "https://servicetitan.wd1.myworkdayjobs.com/ServiceTitan",
}


def resolve_employer(employer: str) -> str | None:
    """Map an alias or a board URL to a board URL, or None if it is neither."""
    value = (employer or "").strip()
    if not value:
        return None
    known = KNOWN_EMPLOYERS.get(value.casefold().replace(" ", "").replace("-", ""))
    if known:
        return known
    if "myworkdayjobs.com" in value:
        return value if "://" in value else f"https://{value}"
    return None
