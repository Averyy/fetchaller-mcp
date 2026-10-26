Captured from the real Google Docs research-search endpoint on 2026-10-25 with
wafer's ordinary AsyncSession, no login or cookies, redirects disabled. JSON
bodies retain Google's original XSSI guard and response envelope.

- `page1.json`, `page2.json`: `Python asyncio documentation`, English, ten
  requested results, cursors `CAA=` / `CAo=`. Ten organic results each, no overlap.
- `empty.json`: `site:example.invalid wafer-search`, English, cursor `CAA=`.
  The response message omits records and the next cursor.
- `banff.json`: `Banff National Park`, English, cursor `CAA=`. General web
  results headed by Parks Canada; the document does not scope the search.
- `french.json`: `musée du Louvre Paris`, French, cursor `CAo=`. Unicode
  titles/snippets and French destinations on a real second page. The public
  tool currently fixes language to English; this fixture checks the decoder.

The first three captures used the verified SIG Autoscaling document; the last
two used the verified Code Organization document. Both IDs and provenance are
recorded in `docs/google-search-document-pool.json`.
