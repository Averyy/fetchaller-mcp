"""Entry point for Home Depot URLs.

``get_homedepot`` returns ``{"content": ...}``, ``{"error": ...}``, or None when
the URL belongs on the ordinary HTML path. Returning None matters: both sites
serve plenty of pages this package does not read (department landing pages,
how-to articles, the home page), and on homedepot.ca those render fine as HTML.
On homedepot.com they still meet Akamai's challenge without a browser — that is
active blocking and stays wafer's — but none of them is a product, listing,
review page or store.
"""

from __future__ import annotations

from urllib.parse import urljoin

import wafer

from . import ca, com, render
from .urls import COM_ORIGIN, CaTarget, ComTarget, ca_target, com_target, with_param


async def get_homedepot(url: str, *, timeout: float = 30.0) -> dict | None:
    """Fetch and render one Home Depot page."""
    try:
        target = com_target(url)
        if target is not None:
            return {"content": await _com(target, url, timeout)}
        ca_tgt = ca_target(url)
        if ca_tgt is not None:
            content = await _ca(ca_tgt, url, timeout)
            return None if content is None else {"content": content}
        return None
    except LookupError as exc:
        return {"error": f"Home Depot: {exc}"}
    except (TimeoutError, OSError, ValueError, wafer.WaferError) as exc:
        # A transport failure has to be reported, not raised: letting it escape
        # fails the whole tool call instead of naming the URL that broke.
        return {"error": f"Home Depot request failed: {type(exc).__name__}: {exc}"}


async def _com(target: ComTarget, url: str, timeout: float) -> str:
    if target.kind == "product":
        product = await com.fetch_product(target.item_id or "", timeout=timeout)
        return render.render_com_product(product, url)
    if target.kind == "reviews":
        product, reviews = await com.fetch_reviews(target, timeout=timeout)
        return render.render_com_reviews(product, reviews, target, url)
    if target.kind == "store":
        store, note = await com.fetch_store(target.store_id or "", target.store_zip or "", timeout=timeout)
        return render.render_com_store(store, url, note=note)
    return await _com_listing(target, url, timeout)


async def _com_listing(target: ComTarget, url: str, timeout: float) -> str:
    model = await com.fetch_listing(target, timeout=timeout)
    redirect = (model.get("metadata") or {}).get("searchRedirect")
    if model.get("searchReport") is None and redirect:
        # The site answers some keywords ("dewalt") with a brand or category
        # page instead of results. The gateway reports where it would go and
        # returns nothing else, so the read follows it — keeping the caller's
        # paging, sort and price range — and says it did.
        destination = urljoin(COM_ORIGIN, str(redirect))
        follow = com_target(destination)
        if follow is None or follow.kind != "listing":
            raise com.GatewayError(f'homedepot.com sends this search to {destination}, which is not a product listing')
        model = await com.fetch_listing(
            target, timeout=timeout, nav_param=follow.nav_param, keyword=follow.keyword or ""
        )
        if model.get("searchReport") is None:
            raise com.GatewayError(f"homedepot.com's redirect target {destination} returned no listing")
        return render.render_com_listing(model, target, url, redirected_to=destination.split("?")[0])
    if model.get("searchReport") is None:
        # An unknown N- value is not an error at the gateway: it answers 200
        # with a null report and no products. Rendering that would read as an
        # empty category.
        what = f"category N-{target.nav_param}" if target.nav_param else f'search "{target.keyword}"'
        raise com.GatewayError(f"homedepot.com returned no listing for {what} — check the URL")
    return render.render_com_listing(model, target, url)


async def _ca(target: CaTarget, url: str, timeout: float) -> str | None:
    if target.kind == "product":
        final_url, state = await ca.fetch_page_state(url, timeout=timeout)
        product = ca.state_product(state, target.code)
        if product is None:
            raise ca.CaReadError(
                f"homedepot.ca has no product {target.code} — its page says the item "
                "may be discontinued or temporarily unavailable"
            )
        code = str(product.get("code") or target.code)
        localized = await ca.fetch_localized([code], lang=target.lang, page_url=final_url, timeout=timeout)
        promotions = await ca.fetch_promotions(code, lang=target.lang, page_url=final_url, timeout=timeout)
        return render.render_ca_product(
            product,
            final_url,
            localized=None if localized is None else localized.get(code, {}),
            promotions=promotions,
        )

    if target.kind == "category":
        final_url, state = await ca.fetch_page_state(url, timeout=timeout)
        found = ca.state_listing(state)
        if found is None:
            # Department landing pages (tools.html) carry tiles, not a listing;
            # the HTML path renders those correctly.
            return None
        plp, aem = found
        return render.render_ca_listing(plp, final_url, title=_ca_title(plp, aem), is_search=False)

    data = await ca.fetch_search(target, page_url=url, timeout=timeout)
    title = f'Home Depot Canada search: "{target.query}"'
    destination = ca.redirect_target(data)
    if destination and not target.filter:
        # The site sends some keywords to a category page ("drill" → Drills);
        # its search page itself carries no results to read instead. Follow
        # it with the caller's page and sort, and say so.
        if target.page > 1:
            destination = with_param(destination, "page", target.page)
        if target.sort:
            destination = with_param(destination, "sort", target.sort)
        final_url, state = await ca.fetch_page_state(destination, timeout=timeout)
        found = ca.state_listing(state)
        if found is not None:
            plp, aem = found
            note = (
                f'homedepot.ca sends the search "{target.query}" to its {_ca_title(plp, aem)} '
                f"category ({final_url.split('?')[0]}) — these are that page's results."
            )
            return render.render_ca_listing(plp, final_url, title=title, is_search=False, note=note)
        note = f'homedepot.ca would send this search to {destination}; showing the search results instead.'
        return render.render_ca_listing(data, url, title=title, is_search=True, note=note)
    return render.render_ca_listing(data, url, title=title, is_search=True)


def _ca_title(plp: dict, aem: dict) -> str:
    for key in ("navTitle", "pageTitle"):
        value = aem.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    crumbs = [c for c in plp.get("breadcrumbs") or [] if isinstance(c, dict) and c.get("title")]
    if crumbs:
        return str(crumbs[-1]["title"])
    return "Home Depot Canada listing"

