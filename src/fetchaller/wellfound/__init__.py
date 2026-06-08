"""wellfound.com support: startup job search, job detail, and company pages.

Every wellfound page is server-rendered. Search/company pages embed a Next.js
``__NEXT_DATA__`` blob with a normalized Apollo cache; job-detail pages embed a
``schema.org/JobPosting`` JSON-LD block. wellfound is behind DataDome (+ an
XHR-only Cloudflare Turnstile that does not gate navigation) — wafer 0.2.4
returns the real SSR document, so this module only parses it.

- ``api.py``    — URL detection, session, ``__NEXT_DATA__`` / Apollo helpers.
- ``render.py`` — markdown renderers for search, company, and job pages.
- ``page.py``   — ``get_wellfound(url, ...)`` dispatch for the fetch tool.
"""
