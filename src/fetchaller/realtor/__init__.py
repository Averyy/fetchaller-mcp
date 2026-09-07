"""realtor.ca support: structured home search + individual listing pages.

- ``api.py`` — api2.realtor.ca client (geocode + PropertySearch_Post) and the
  SSR listing-detail fetch/parse. URL detection lives here too.
- ``render.py`` — markdown renderers for search results and listing detail.

realtor.ca is behind Cloudflare (since 2026-09; Imperva before that). The front
page serves a managed challenge that wafer's ``browser_solver`` clears; api2
serves a WAF block page to any request that does not carry the clearance the
front page minted. fetchaller does NO challenge handling — it passes the shared
``browser_solver`` and a per-host ``rate_limit`` — but it does own the request
*order*: ``api._api()`` loads the front page on the shared session before the
first api2 call, the way a browser does, and re-clears once on a mid-session
block.
"""
