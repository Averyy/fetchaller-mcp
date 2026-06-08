"""realtor.ca support: structured home search + individual listing pages.

- ``api.py`` — api2.realtor.ca client (geocode + PropertySearch_Post) and the
  SSR listing-detail fetch/parse. URL detection lives here too.
- ``render.py`` — markdown renderers for search results and listing detail.

api2.realtor.ca is Imperva-protected; wafer 0.2.4 passes it transparently via
its native-TLS fallback (light load, no browser) and, under escalation, a
``browser_solver`` that solves on the origin page (www.realtor.ca) with same-site
XHR passthrough. fetchaller does NO challenge handling — it just passes the shared
``browser_solver`` and a per-host ``rate_limit`` so wafer can mint/reuse the
token and avoid rate-based challenges.
"""
