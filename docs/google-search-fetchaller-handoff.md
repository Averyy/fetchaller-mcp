# Google web search for fetchaller: implementation handoff

Validation date: 2026-10-25.

## Decision

Implement the Google leg using the Google Docs research-search endpoint and
fetchaller's existing HTTP client. **No wafer change or upgrade is required.**
No browser, JavaScript engine, TLS impersonation, Google login, or authentication
cookies are required for the requests observed here. Stock macOS `/usr/bin/curl`
8.7.1, with its default User-Agent, returned real search results.

This is general web search through a Google application endpoint. It does not
search the selected document, Google Docs files, or Google Scholar. Verified
results include IKEA shopping pages, Parks Canada, National Geographic, Python
documentation, and Reddit threads.

This is an alternative to the ordinary `www.google.com/search` route. The
SearchGuard VM work has **not** produced a verified pass of that route. Do not
make the fetchaller change depend on QuickJS, `SG_SS`, Opera Mini, or a browser
solver.

## Endpoint and document selection

```text
POST https://docs.google.com/document/d/{document_id}/explore/search
```

Use an eligible, publicly accessible document ID as request context. Its contents
do not scope the search. Public visibility or a link from a reputable project
does not alone establish endpoint eligibility: several Google/Go examples
returned 401.

The original working document is **10 Ways to Use the Web for Verification**:

```text
1X8Gd2RPfsQdiYVcrUmgk_RpQ7iAl-UQd9vlvpZ8Itvs
```

Use the separately recorded [verified document pool](google-search-document-pool.json)
as configuration, rather than generating IDs or discovering documents during
each search. Only entries that passed an actual search should enter the active
pool. Do not include rejected candidates merely because their preview loads.

## Verified active pool

```json
[
  "1RvhQAEIrVLHbyNnuaT99-6u9ZUMp7BfkPupT2LAZK7w",
  "10QkXwiZfBL7wBKHHFslooXvGDKHA75jkkgiIXuKFryM",
  "1HtTI0rJEGP_MSf6eO87aCmx_tzpovPAAg7U2Zxwm8FE"
]
```

| Document | ID | Verified results |
|---|---|---|
| Kubernetes SIG Autoscaling Agenda | `1RvhQAEIrVLHbyNnuaT99-6u9ZUMp7BfkPupT2LAZK7w` | 10 + 10, zero overlap |
| SIG Arch - Production Readiness Agenda and Notes | `10QkXwiZfBL7wBKHHFslooXvGDKHA75jkkgiIXuKFryM` | 10 + 10, zero overlap |
| Kube SIG Arch: Code Organization | `1HtTI0rJEGP_MSf6eO87aCmx_tzpovPAAg7U2Zxwm8FE` | 10 + 10, zero overlap |

All three are publicly linked by the official Kubernetes community repository.
Their preview pages returned 200, and each search endpoint returned pages 1 and
2 through stock curl with no cookies or impersonation. Their account ownership
was not established; do not describe them as Google-owned.

## Request format

The body is **URL-encoded form data**, with one field named `request`. That
field's value is a JSON array:

```json
["documentsuggest.search.search_request","site:reddit.com mechanical keyboard recommendations",[10,"CAA="],["en","en"],1]
```

| Position | Value |
|---|---|
| 0 | Literal `documentsuggest.search.search_request` |
| 1 | User's search query, unchanged |
| 2 | `[requested_count, cursor]` |
| 3 | `[interface_language, result_language]`; `["en","en"]` and `["fr","fr"]` were tested |
| 4 | Integer `1` |

Use the HTTP client's form encoder after serializing the array with a JSON
serializer. Do not send the array as the raw POST body or JSON-encode it twice.
Unicode queries work; a French Louvre query returned French web results.

`Content-Type: application/x-www-form-urlencoded` is sufficient; adding
`;charset=UTF-8` also worked. `Accept: application/json` is optional. Stock curl's
default `Accept: */*` worked. No custom User-Agent, Referer, Origin, client hints,
or Cookie header was needed in the plain-client test.

Minimal reproduction, using the verified Kubernetes SIG Autoscaling document:

```sh
curl -q --silent --show-error --max-time 30 --max-redirs 0 \
  --data-urlencode 'request=["documentsuggest.search.search_request","site:reddit.com mechanical keyboard recommendations",[10,"CAA="],["en","en"],1]' \
  'https://docs.google.com/document/d/1RvhQAEIrVLHbyNnuaT99-6u9ZUMp7BfkPupT2LAZK7w/explore/search'
```

Do not add `--location`: redirects must not be followed. `-q` prevents curl from
loading a user's configuration file; it is not an impersonation option.

## Response parsing

Require HTTP 200, then remove Google's XSSI guard when present: the leading
line begins with `)]}'`. Parse the remainder as JSON.

The observed structure is:

```text
root[0][0]       = "documentsuggest.search.search_response"
root[0][1]       = result records, when present
root[0][1][i][0] = organic result array, when present
root[0][2]       = next cursor, when present
```

An organic result array starts with:

```text
[URL, title, snippet, ...]
```

Check types before indexing. Map these values to fetchaller's existing Google
result type. Retain the returned URL; no Google HTML selectors or `/url?q=`
unwrapping was needed for these responses.

Some records are knowledge cards rather than organic links. Skip those when
building the ordinary result list. Do not require exactly the requested number
of organic links: a request for ten returned eight organic links on one French
second page. Validate the response message independently of its result count;
an HTML interstitial or malformed JSON must not silently become an empty,
successful Google search.

A genuine zero-result query returned HTTP 200 with this complete first message:

```json
["documentsuggest.search.search_response"]
```

The records and cursor can therefore be **absent**, not merely empty. After
validating the message name, treat a missing/null records field as an empty list
and a missing/null/empty cursor as the end of results. The validation query was
`site:example.invalid wafer-search`. This valid empty response must not trigger
document failover.

Treat titles and snippets as untrusted search content and pass them through
fetchaller's existing text/rendering handling.

## Pagination

Use `CAA=` for the first page. For sequential navigation, carry forward the
server's `root[0][2]` cursor with the same query, language, and requested count.
An absent or empty cursor ends pagination.

For fetchaller's stateless, one-indexed `page` argument, the observed cursor is
standard base64 of a protobuf message: field 1, unsigned varint result offset.
Its first byte is `0x08`.

```text
offset = (page - 1) * requested_count
cursor = base64([0x08] + encode_unsigned_varint(offset))
```

| Count | Page | Offset | Cursor |
|---:|---:|---:|---|
| 10 | 1 | 0 | `CAA=` |
| 10 | 2 | 10 | `CAo=` |
| 20 | 2 | 20 | `CBQ=` |
| 20 | 3 | 40 | `CCg=` |
| 20 | 8 | 140 | `CIwB` |

Only first and second pages have been validated live so far. Later rows describe
the encoding, not successful deep-page tests. Use a real varint encoder: a
two-byte `[0x08, offset]` shortcut breaks at offsets of 128 and above.

Prefer a cached server-returned cursor when available. Cache keys must include
the query, language, requested count, and page. Keep the count fixed across
pages. Do not fetch twenty results, display only ten, and advance by twenty.

## Document fallback and errors

Keep the eligible IDs in a configurable array, with health state separate from
the IDs themselves. A simple implementation can remember the most recently
successful ID and try another healthy ID after a document-specific failure.
Retry the **same query and cursor** when changing documents.

| Outcome | Handling |
|---|---|
| 200, valid response message | Decode results and next cursor |
| 401 / 403 / 404 | Mark that document unavailable for a cooldown; try the next configured ID within a small bounded budget |
| Any redirect | Do not follow it; record failure and its destination host/path |
| 429 | Honor `Retry-After` and the existing Google-wide backoff; do not cycle document IDs to evade throttling |
| 5xx / timeout | Use bounded transient retries/backoff, then report Google unavailable |
| HTML or unexpected response structure | Report a provider/protocol error; retain useful diagnostics without logging cookies |

Apply the request budget and rate limit across **all** document IDs. Do not
multiply retries by the full pool size or run concurrent document probes for
each search. Validation used sequential requests spaced at least six seconds
apart. Health checks should be infrequent; ordinary successful searches can
refresh health state.

Never fetch `/sorry`, `sorry.google.com`, or a Google authentication redirect.
Never add personal Google account cookies as a fallback. If every configured
document fails, preserve fetchaller's existing unavailable/partial-results
behavior. Do not relabel another engine's results as Google results.

## Suggested integration sequence

1. Replace the old Google/Opera Mini request builder in `search_google` and the
   Google branch of combined `search` with this POST request.
2. Add a JSON response decoder that returns the existing result schema.
3. Map the existing `page` argument to a returned or encoded cursor.
4. Load the verified document array from configuration and add bounded failover.
5. Keep language, pagination, cache, and provider-attribution behavior explicit.
6. Use captured real responses as parser fixtures. Keep live acceptance checks
   opt-in and CAPTCHA-safe.

No change is needed to wafer's TLS profiles or challenge solvers. Fetchaller can
use an ordinary HTTP client for this JSON API. The [wafer recipes](ref-google-research.md)
remain examples of another compatible client, not a dependency of the solution.

## Observed acceptance and limits

Successful web queries included Python asyncio, Python dataclasses, Banff
National Park, IKEA Canada desks, and a French Louvre query. Real second pages
were observed. The Reddit query returned ten `reddit.com` links on each of two
pages, with zero URL overlap, including actual thread URLs.

The endpoint is a legacy Google application interface. An eligible public
document remains a dependency, even with multiple verified IDs. We have not
established identical ranking, freshness, SafeSearch, geographic targeting, or
time filtering to the ordinary Google SERP. Do not advertise unsupported filter
parity. This change provides observed general-web results and pagination; it
does not establish successful validation of the normal SearchGuard VM.

The route was discovered via the
[SearXNG discussion](https://github.com/searxng/searxng/issues/6359) and a
[current public implementation](https://github.com/vojkovic/priv/blob/main/src/engines/google_docs.c).
The acceptance claims above come from direct requests made during this work.
