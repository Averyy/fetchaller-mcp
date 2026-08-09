"""Entry points for ui.com pages and for downloading a guide.

``get_ubiquiti`` returns ``{"content": ...}``, ``{"error": ...}``, or None when
the URL belongs on the ordinary HTML path. Returning None rather than an error
matters: ui.com serves plenty of pages this module knows nothing about (the
blog, the help centre, the downloads portal), and those render fine as HTML.
"""

from __future__ import annotations

import wafer

from . import api, guide, manual, render


async def get_ubiquiti(url: str, *, timeout: float = 30.0) -> dict | None:
    """Fetch and render one ui.com page."""
    try:
        if api.is_ui_guide(url):
            data = await manual.fetch_guide(url, timeout=timeout)
            return {"content": render.render_guide(data)}

        found = await api.fetch_page(url, timeout=timeout)
        if found is None:
            return None
        route, next_data, props = found

        if route in (
            api.ROUTE_STORE_PRODUCT,
            api.ROUTE_STORE_COLLECTION_PRODUCT,
            api.ROUTE_STORE_COLLECTION,
        ):
            product = api.store_product(next_data)
            if product is None:
                return {"error": "Ubiquiti store page carried no product data"}
            return {"content": render.render_product(product, props, url)}

        if route == api.ROUTE_STORE_CATEGORY:
            groups = api.store_category_products(next_data)
            if not groups:
                return {"error": "Ubiquiti category page listed no products"}
            return {"content": render.render_category(groups, props, url)}

        if route == api.ROUTE_SPECS_PRODUCT:
            product = api.techspecs_product(next_data)
            if product is None:
                return {"error": "Ubiquiti tech specs page carried no product data"}
            return {"content": render.render_techspecs_product(product, url)}

        if route == api.ROUTE_SPECS_CATEGORY:
            groups = api.techspecs_groups(next_data)
            if not groups:
                return {"error": "Ubiquiti tech specs page listed no products"}
            return {"content": render.render_techspecs_category(groups, url)}

        return None
    except guide.GuideNotFoundError as exc:
        return {"error": f"{exc}"}
    except LookupError as exc:
        return {"error": f"Ubiquiti page could not be read: {exc}"}
    except (TimeoutError, OSError, ValueError, wafer.WaferError) as exc:
        # A transport failure has to be reported, not raised: letting it escape
        # fails the whole tool call instead of naming the URL that broke.
        return {"error": f"Ubiquiti request failed: {type(exc).__name__}: {exc}"}
