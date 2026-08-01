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
    "buildertrend": "https://buildertrend.wd108.myworkdayjobs.com/External_Careers",
    "cae": "https://cae.wd3.myworkdayjobs.com/career",
    # careers.cisco.com is a Phenom front end, but the board underneath is
    # public Workday. Note the cloud: every slug on wd1/wd3/wd12/wd103 answers
    # 422, and only wd5 + Cisco_Careers answers 200.
    "cisco": "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers",
    "crowdstrike": "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers",
    "gaf": "https://gafsgi.wd5.myworkdayjobs.com/GAF_Careers",
    "motorolasolutions": "https://motorolasolutions.wd5.myworkdayjobs.com/Careers",
    "nvidia": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    # Clearpath Robotics is part of Rockwell and its own careers page sends
    # candidates here, so both names resolve to the Rockwell-wide board. It is
    # NOT prefiltered to Clearpath roles.
    "clearpath": "https://rockwellautomation.wd1.myworkdayjobs.com/External_Rockwell_Automation",
    "rockwellautomation": (
        "https://rockwellautomation.wd1.myworkdayjobs.com/External_Rockwell_Automation"
    ),
    "salesforce": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
    "servicetitan": "https://servicetitan.wd1.myworkdayjobs.com/ServiceTitan",
    "teledyneflir": "https://flir.wd1.myworkdayjobs.com/flircareers",
    # snyk.io's careers page is a Contentful shell that proxies Workday through
    # its own /api/next/jobs route; the board underneath is public Workday.
    "snyk": "https://snyk.wd103.myworkdayjobs.com/External",
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
