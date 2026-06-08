"""Entry points: ``search_realtor`` (MCP tool) + ``get_realtor`` (fetch dispatch)."""

from __future__ import annotations

import wafer

from . import api
from .render import render_listing_detail, render_search_results


def _filters_desc(
    transaction: str, property_type: str, building_type: str | None,
    min_price: int | None, max_price: int | None,
    min_beds: int | None, min_baths: int | None, ownership: str | None,
) -> str:
    parts: list[str] = []
    if property_type and property_type != "any":
        parts.append(property_type.replace("-", " "))
    if building_type:
        parts.append(building_type)
    if ownership:
        parts.append(ownership)
    if min_beds:
        parts.append(f"{min_beds}+ bed")
    if min_baths:
        parts.append(f"{min_baths}+ bath")
    unit = "/mo" if transaction == "rent" else ""
    if min_price and max_price:
        parts.append(f"${min_price:,}–${max_price:,}{unit}")
    elif min_price:
        parts.append(f"from ${min_price:,}{unit}")
    elif max_price:
        parts.append(f"up to ${max_price:,}{unit}")
    return ", ".join(parts)


def _wafer_error(e: Exception) -> dict:
    msg = str(e)
    if "imperva" in msg.lower() or "challenge" in msg.lower():
        return {"error": "realtor.ca blocked the request (bot challenge). Try again shortly."}
    if "timeout" in msg.lower():
        return {"error": "realtor.ca request timed out."}
    return {"error": f"realtor.ca error: {e}"}


async def search_realtor(
    *,
    location: str,
    transaction: str = "sale",
    property_type: str = "any",
    building_type: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_beds: int | None = None,
    min_baths: int | None = None,
    ownership: str | None = None,
    sort: str = "newest",
    page: int = 1,
    browser_solver=None,
) -> dict:
    """Structured home search over realtor.ca's api2. Returns ``{"content"}``."""
    try:
        geo = await api.geocode(location, browser_solver=browser_solver)
        if not geo or (not geo.get("geoid") and not geo.get("bbox")):
            return {"error": f"Could not find location on realtor.ca: {location!r}"}

        data = await api.property_search(
            bbox=geo.get("bbox"), geoids=geo.get("geoid", ""),
            transaction=transaction, property_type=property_type, building_type=building_type,
            min_price=min_price, max_price=max_price, min_beds=min_beds, min_baths=min_baths,
            ownership=ownership, sort=sort, page=page, browser_solver=browser_solver,
        )
        desc = _filters_desc(transaction, property_type, building_type, min_price,
                             max_price, min_beds, min_baths, ownership)
        content = render_search_results(
            data, location=geo.get("location", location),
            transaction=transaction, filters_desc=desc, page=page,
        )
        return {"content": content}
    except wafer.WaferError as e:
        return _wafer_error(e)
    except Exception as e:  # noqa: BLE001 — surface a clean message to the LLM
        return {"error": f"realtor.ca search failed: {type(e).__name__}: {e}"}


async def get_realtor(url: str, browser_solver=None) -> dict:
    """Dispatch a realtor.ca URL (listing detail or search/SEO/map page)."""
    try:
        if api.is_realtor_listing(url):
            parsed = await api.get_listing_detail(url, browser_solver=browser_solver)
            if not parsed.get("price") and not parsed.get("description"):
                return {"error": "realtor.ca listing page had no extractable content."}
            similar = await api.get_similar_listings(parsed, browser_solver=browser_solver)
            return {"content": render_listing_detail(parsed, similar)}

        if api.is_realtor_search(url):
            res = await api.search_from_url(url, browser_solver=browser_solver)
            content = render_search_results(
                res["data"], location=res.get("location", "search area"),
                transaction=res.get("transaction", "sale"), page=res.get("page", 1),
            )
            return {"content": content}

        return {"error": f"Unrecognized realtor.ca URL: {url}"}
    except wafer.WaferError as e:
        return _wafer_error(e)
    except Exception as e:  # noqa: BLE001
        return {"error": f"realtor.ca fetch failed: {type(e).__name__}: {e}"}
