"""Hugging Face-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_huggingface,
strip_huggingface_junk, postprocess_huggingface).
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


def is_huggingface(url: str) -> bool:
    """Check if URL is a Hugging Face page."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in ("huggingface.co", "www.huggingface.co")


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Main site header (Models/Datasets/Spaces/Community/Docs/Enterprise/Pricing/Log In/Sign Up)
    '[data-target="MainHeader"]',
    # SSO banner
    '[data-target="SSOBanner"]',
    # Unsafe content banner
    '[data-target="UnsafeBanner"]',
    # Theme switcher
    '[data-target="ThemeSwitcher"]',
    # Device provider (empty)
    '[data-target="DeviceProvider"]',
    # System theme monitor (empty)
    '[data-target="SystemThemeMonitor"]',
    # Dataset viewer / Data Studio (massive — 192k+ chars on fineweb)
    '[data-target="DatasetViewer"]',
    # Inference widget ("This model isn't deployed by any Inference Provider")
    '[data-target="InferenceWidget"]',
    # Linked spaces list (emoji-heavy app links)
    '[data-target="LinkedSpacesList"]',
    # Model evaluation results (empty or auto-generated tables)
    '[data-target="ModelEvalResults"]',
    # Model tensor/params info ("Safetensors Model size 3B params...")
    '[data-target="ModelTensorsParams"]',
    # Dataset actions dropdown
    '[data-target="DatasetAndModelActionsDropdown"]',
    # Dataset library install snippet
    '[data-target="DatasetLibrary"]',
    # Repo code copy button
    '[data-target="RepoCodeCopy"]',
    # Org header actions (Follow button area for org pages)
    '[data-target="OrgHeaderActions"]',
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (runs before markdownify)
# ---------------------------------------------------------------------------


def strip_huggingface_junk(soup: BeautifulSoup) -> None:
    """Remove HF-specific junk that CSS selectors can't easily catch."""
    # Remove filter tag links (/models?pipeline_tag=..., /models?library=..., etc.)
    for a in list(soup.find_all("a", href=True)):
        href = a["href"]
        if href.startswith("/models?") or href.startswith("/datasets?"):
            a.decompose()

    # Remove "Deploy" and "Use this model" buttons
    for btn in list(soup.find_all("button")):
        text = btn.get_text(strip=True).lower()
        if text in ("deploy", "use this model"):
            btn.decompose()

    # Remove like/follow buttons (contain "like" or "Follow" text)
    for btn in list(soup.find_all("button")):
        text = btn.get_text(strip=True)
        if text == "like" or text.startswith("Follow"):
            btn.decompose()

    # Remove follower/like count buttons (e.g., "3.42k", "18.3k")
    for btn in list(soup.find_all("button")):
        text = btn.get_text(strip=True)
        if re.match(r"^[\d,.]+[kKmM]?$", text):
            btn.decompose()

    # Remove avatar images (cdn-avatars.huggingface.co)
    for img in list(soup.find_all("img", src=True)):
        src = img["src"]
        if "cdn-avatars.huggingface.co" in src:
            parent = img.parent
            if parent and parent.name == "a" and len(list(parent.children)) == 1:
                parent.decompose()
            else:
                img.decompose()

    # Remove the HF logo image
    for img in list(soup.find_all("img", alt=True)):
        if img.get("alt", "").strip() == "Hugging Face's logo":
            parent = img.parent
            if parent and parent.name == "a":
                parent.decompose()
            else:
                img.decompose()

    # Remove "N models" derivative links (e.g., "953 models", "401 models")
    for a in list(soup.find_all("a", href=True)):
        href = a["href"]
        text = a.get_text(strip=True)
        if href.startswith("/models?other=base_model:") and re.match(r"^\d+\s+models?$", text):
            a.decompose()

    # Remove gated model login/signup gate blocks
    # These contain "Log in or Sign Up to review the conditions"
    for a in list(soup.find_all("a", href=True)):
        href = a["href"]
        text = a.get_text(strip=True)
        if href.startswith("/login") and text in ("Log in", "Sign Up"):
            a.decompose()
        elif href.startswith("/join") and text == "Sign Up":
            a.decompose()


# ---------------------------------------------------------------------------
# Markdown-level post-processing (runs after markdownify)
# ---------------------------------------------------------------------------

# Pre-compiled regex for whitespace cleanup
_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# HF logo markdown: [![Hugging Face's logo](...) Hugging Face](/)
_HF_LOGO_RE = re.compile(
    r"\n?\[?!\[Hugging Face['\u2019]s logo\]\([^\)]+\)\s*\n?"
    r"(?:Hugging Face\]\(/\))?\n?"
)

# Standalone "Hugging Face" link to root
_HF_HOME_LINK_RE = re.compile(r"(?:^|\n)\[?Hugging Face\]\(/\)\n?")

# Tab navigation: "Model card", "Files and versions", "Community N"
_TAB_MODEL_CARD_RE = re.compile(r"\n?\[Model card\]\([^\)]+\)\n?")
_TAB_FILES_RE = re.compile(r"\n?\[Files\s*(?:Files and versions)?\s*(?:xet)?\]\([^\)]+\)\n?")
_TAB_COMMUNITY_RE = re.compile(r"\n?\[Community\s*\d*\]\([^\)]+\)\n?")
_TAB_DATASET_CARD_RE = re.compile(r"\n?\[Dataset card\]\([^\)]+\)\n?")
_TAB_DATA_STUDIO_RE = re.compile(r"\n?\[Data Studio\]\([^\)]+\)\n?")

# "like N" / "Follow OrgName N" standalone lines
_LIKE_RE = re.compile(r"(?:^|\n)like[\s\xa0]+[\d,.]+[kKmM]?(?:\n|$)")
_FOLLOW_RE = re.compile(r"(?:^|\n)Follow[\s\xa0]+.+?[\d,.]+[kKmM]?(?:\n|$)")

# Deploy / Use this model buttons
_DEPLOY_RE = re.compile(r"(?:^|\n)Deploy(?:\n|$)")
_USE_MODEL_RE = re.compile(r"(?:^|\n)Use this model(?:\n|$)")

# "Log in or Sign Up" gate block
_LOGIN_GATE_RE = re.compile(
    r"(?:^|\n)\[Log in\]\([^\)]+\)\s*\n\s*or\s*\n\s*\[Sign Up\]\([^\)]+\)\s*\n"
    r"(?:to review the conditions and access this model content\.\s*\n?)?"
)

# Orphaned gate text left after soup-level link removal
_REVIEW_CONDITIONS_RE = re.compile(
    r"(?:^|\n)to review the conditions and access this model content\.(?:\n|$)"
)

# Standalone "or" between login/signup
_STANDALONE_OR_RE = re.compile(r"\nor\n")

# "Inference Providers" / "NEW" label
_INFERENCE_PROVIDERS_RE = re.compile(
    r"(?:^|\n)Inference Providers\s*\n?\s*NEW\s*\n?", re.IGNORECASE
)

# "This model isn't deployed by any Inference Provider"
_NOT_DEPLOYED_RE = re.compile(
    r"(?:^|\n).*?(?:isn['\u2019]t deployed|is not deployed) by any Inference Provider.*?(?:\n|$)"
)

# Base model derivative counts: "953 models" / "401 models" links
_DERIVATIVE_MODELS_RE = re.compile(r"\n?\[?\d+\s+models?\]?\(?[^\)]*\)?\n?")

# "Files info" line
_FILES_INFO_RE = re.compile(r"(?:^|\n)Files info(?:\n|$)")

# "Data Studio" / "API" / "Embed" / "Duplicate" action buttons
_DATA_STUDIO_RE = re.compile(r"(?:^|\n)Data Studio(?:\n|$)")
_API_BUTTON_RE = re.compile(r"(?:^|\n)API(?:\n|$)")
_EMBED_BUTTON_RE = re.compile(r"(?:^|\n)Embed(?:\n|$)")
_DUPLICATE_RE = re.compile(r"\n?\[Duplicate\]\([^\)]+\)\n?")

# Subset/split selector lines from dataset viewer
_SUBSET_RE = re.compile(r"(?:^|\n)Subset \(\d+\)\n")
_SPLIT_RE = re.compile(r"(?:^|\n)Split \(\d+\)\n")
_SEARCH_NOT_AVAILABLE_RE = re.compile(r"(?:^|\n)Search is not available for this dataset\n?")
_SQL_CONSOLE_RE = re.compile(r"(?:^|\n)SQL\s*\n\s*Console\n?")

# "Auto-converted to Parquet" notice
_AUTOCONVERTED_RE = re.compile(r"\n?\[Auto-converted to Parquet\]\([^\)]+\)\n?")

# Org page: "AI & ML interests\nNone defined yet."
_AI_ML_INTERESTS_RE = re.compile(r"(?:^|\n)### AI & ML interests\n+None defined yet\.\n?")

# "Recent Activity" section on org pages
_RECENT_ACTIVITY_RE = re.compile(
    r"(?:^|\n)### Recent Activity\n.+?(?=\n### |\n## |\Z)",
    re.DOTALL,
)

# "View all activity/Papers/models/datasets/collections" links
_VIEW_ALL_RE = re.compile(r"\n?\[View (?:all |)\d*\s*\w+\]\([^\)]+\)\n?")

# "Sort: Recently updated" control
_SORT_RE = re.compile(r"(?:^|\n)Sort:\s*Recently updated\n?")

# Org "Team members N"
_TEAM_MEMBERS_RE = re.compile(r"(?:^|\n)### Team members \d+\n?")

# "Organization Card" / "Community" / "About org cards" links
_ORG_CARD_RE = re.compile(r"(?:^|\n)Organization Card\n?")
_ABOUT_ORG_CARDS_RE = re.compile(r"\n?\[About org cards\]\([^\)]+\)\n?")

# Org page metadata: "Enterprise", "company", "Verified" standalone lines
_ENTERPRISE_LINK_RE = re.compile(r"\n?\[Enterprise\]\([^\)]+\)\n?")
_COMPANY_RE = re.compile(r"(?:^|\n)company(?:\n|$)")
_VERIFIED_RE = re.compile(r"(?:^|\n)Verified(?:\n|$)")

# "[Activity Feed](/organizations/.../activity/all)" link
_ACTIVITY_FEED_RE = re.compile(r"\n?\[Activity Feed\]\([^\)]+\)\n?")

# Empty metadata labels left after filter links are stripped
# Matches label lines with no value (followed by blank line then another label or heading)
_EMPTY_META_LABEL_RE = re.compile(
    r"(?:^|\n)(?:Tasks|Modalities|Languages|Size|ArXiv):?\s*\n(?=\n)"
)

# Gated model gate block: "## You need to agree to share your contact information..."
# Contains full license agreement + acceptable use policy (2000+ tokens).
# Matches from the gate heading through to the next ## heading.
_GATED_MODEL_BLOCK_RE = re.compile(
    r"(?:^|\n)## You need to agree to .+?"
    r"(?=\n## (?!You need to))",
    re.DOTALL,
)

# Also catch standalone license/AUP headings that appear outside the gate block
_LICENSE_AGREEMENT_RE = re.compile(
    r"(?:^|\n)### [A-Z ]+(?:COMMUNITY )?LICENSE AGREEMENT\n.+?"
    r"(?=\n## |\n### [A-Za-z]+ Acceptable Use|\Z)",
    re.DOTALL,
)
_ACCEPTABLE_USE_RE = re.compile(
    r"(?:^|\n)### [A-Za-z 0-9.]+ Acceptable Use Policy\n.+?"
    r"(?=\n## |\Z)",
    re.DOTALL,
)


def postprocess_huggingface(markdown: str) -> str:
    """Strip remaining Hugging Face UI text from markdown."""
    # Header chrome
    markdown = _HF_LOGO_RE.sub("\n", markdown)
    markdown = _HF_HOME_LINK_RE.sub("\n", markdown)

    # Tab navigation
    markdown = _TAB_MODEL_CARD_RE.sub("\n", markdown)
    markdown = _TAB_FILES_RE.sub("\n", markdown)
    markdown = _TAB_COMMUNITY_RE.sub("\n", markdown)
    markdown = _TAB_DATASET_CARD_RE.sub("\n", markdown)
    markdown = _TAB_DATA_STUDIO_RE.sub("\n", markdown)

    # Like/Follow/Deploy/Use buttons
    markdown = _LIKE_RE.sub("\n", markdown)
    markdown = _FOLLOW_RE.sub("\n", markdown)
    markdown = _DEPLOY_RE.sub("\n", markdown)
    markdown = _USE_MODEL_RE.sub("\n", markdown)

    # Login gate
    markdown = _LOGIN_GATE_RE.sub("\n", markdown)
    markdown = _REVIEW_CONDITIONS_RE.sub("\n", markdown)

    # Inference providers
    markdown = _INFERENCE_PROVIDERS_RE.sub("\n", markdown)
    markdown = _NOT_DEPLOYED_RE.sub("\n", markdown)

    # Derivative model counts
    markdown = _DERIVATIVE_MODELS_RE.sub("\n", markdown)

    # Dataset viewer remnants
    markdown = _DATA_STUDIO_RE.sub("\n", markdown)
    markdown = _API_BUTTON_RE.sub("\n", markdown)
    markdown = _EMBED_BUTTON_RE.sub("\n", markdown)
    markdown = _DUPLICATE_RE.sub("\n", markdown)
    markdown = _SUBSET_RE.sub("\n", markdown)
    markdown = _SPLIT_RE.sub("\n", markdown)
    markdown = _SEARCH_NOT_AVAILABLE_RE.sub("\n", markdown)
    markdown = _SQL_CONSOLE_RE.sub("\n", markdown)
    markdown = _AUTOCONVERTED_RE.sub("\n", markdown)
    markdown = _FILES_INFO_RE.sub("\n", markdown)

    # Org page junk
    markdown = _AI_ML_INTERESTS_RE.sub("\n", markdown)
    markdown = _RECENT_ACTIVITY_RE.sub("\n", markdown)
    markdown = _VIEW_ALL_RE.sub("\n", markdown)
    markdown = _SORT_RE.sub("\n", markdown)
    markdown = _TEAM_MEMBERS_RE.sub("\n", markdown)
    markdown = _ORG_CARD_RE.sub("\n", markdown)
    markdown = _ABOUT_ORG_CARDS_RE.sub("\n", markdown)
    markdown = _ENTERPRISE_LINK_RE.sub("\n", markdown)
    markdown = _COMPANY_RE.sub("\n", markdown)
    markdown = _VERIFIED_RE.sub("\n", markdown)
    markdown = _ACTIVITY_FEED_RE.sub("\n", markdown)
    markdown = _EMPTY_META_LABEL_RE.sub("\n", markdown)

    # Gated model license blocks (huge, 2000+ tokens)
    markdown = _GATED_MODEL_BLOCK_RE.sub("\n", markdown)
    markdown = _LICENSE_AGREEMENT_RE.sub("\n", markdown)
    markdown = _ACCEPTABLE_USE_RE.sub("\n", markdown)

    # Standalone "or" left from login gate cleanup
    markdown = _STANDALONE_OR_RE.sub("\n", markdown)

    # Collapse excessive newlines
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
