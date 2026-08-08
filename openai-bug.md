# Bug: JSON truncation is undetectable at the semantic layer

Filed 2026-08-06. Found while sweeping ATS job boards; reproduced deliberately for this report.

## Summary

`mcp__fetchaller__fetch` truncates large JSON responses into a **syntactically valid** structural
prefix. For the array-shaped responses that most listing APIs return, the result is a document that
parses cleanly, contains a plausible number of elements, and is **indistinguishable from a complete
small response**. The only signal is a single boolean key, `_fetchaller_truncated`, which carries no
count of what was dropped and no way to fetch the rest.

The practical consequence: a caller asking "does this company have any design roles?" gets back a
well-formed board with 7 jobs on it and concludes the answer is no. The real board has 734.

## Reproduction

Ground truth via `curl` (no fetchaller in the path):

```
$ curl -s 'https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true' | wc -c
12439529
$ ... | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["jobs"]))'
734

$ curl -s 'https://api.ashbyhq.com/posting-api/job-board/maintainx?includeCompensation=true' | wc -c
2179228
$ ... | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["jobs"]))'
154
```

Through fetchaller:

| URL | `maxTokens` | jobs returned | true total | valid JSON? | flag set? |
|---|---|---|---|---|---|
| Ashby `openai` | default (25000) | **7** | 734 | yes | yes |
| Ashby `openai` | 2000 | **1** | 734 | yes | yes |
| Ashby `maintainx` | **250000** (the maximum) | **70** | 154 | yes | yes |

Verification of the default-`maxTokens` OpenAI response:

```
bytes returned: 92795
VALID JSON. jobs: 7
_fetchaller_truncated present: True
_fetchaller_truncated value: True
```

And of the MaintainX response at the maximum allowed `maxTokens`:

```
bytes returned: 996900   (true board = 2,179,228 bytes / 154 jobs)
parses as valid JSON: YES
jobs returned: 70 of 154
keys at top level: ['jobs', '_fetchaller_truncated']
last job object complete? keys= 15  has jobUrl: True
```

Note the last element is a **complete, well-formed job object**. There is no ragged edge to notice.

## Correction to a claim you may have heard

Downstream notes in my job-sweep corpus asserted that responses get "cut off with **no flag set at
all**." **I could not reproduce that today** — the flag was correctly set in all three runs above.
The earlier claim was probably a caller that never checked the key. Please treat the "flag is
missing" report as unconfirmed; the defect below is real regardless and does not depend on it.

## Root cause

`src/fetchaller/tools/fetch.py`, `_encode_json_prefix()` (~line 480). The function is doing exactly
what it says: emitting one structural prefix, closing every open bracket so the output stays
standards-compliant, and setting `_JSON_TRUNCATION_KEY` (line 467).

That design is defensible and I am not asking you to emit broken JSON. The problem is that
**validity is precisely what makes the loss invisible** for list payloads. A truncated JSON document
that fails to parse is a loud error. A truncated JSON document that parses is a quiet wrong answer.

## Why `maxTokens` is not the workaround

`maxTokens` is capped at 250,000. The MaintainX board is 2.2 MB and the OpenAI board is 12.4 MB, so
**no legal value of `maxTokens` can return either board.** Raising the parameter is not a fix
available to the caller; it just changes how much gets silently dropped.

## The part that makes this worse than it looks

At default `maxTokens`, fetchaller truncated 734 jobs down to 7 — and the 92,795-character result
**still** exceeded the MCP host's tool-result cap and got spilled to a file on disk. So the
truncation did not avoid the spill it exists to prevent. It destroyed 727 job records first, and
then the client wrote the survivors to a file anyway.

If the payload is going to end up in a file regardless, truncating it beforehand is pure loss.

## Suggested fixes, roughly in order of value

1. **Make the flag quantitative.** Replace the boolean with an object naming the truncated container
   and the counts:
   ```json
   {"_fetchaller_truncated": {"path": "jobs", "included": 70, "total": 154, "bytes_total": 2179228}}
   ```
   `total` is the single most useful field — it is what turns "7 jobs" from a believable answer into
   an obviously incomplete one. The encoder already knows the source container's length at the point
   it stops.

2. **Offer continuation.** An `offset` / cursor parameter scoped to the truncated array would let a
   caller page a 734-element board in ~11 calls instead of giving up. Without it, the documented
   advice has to be "don't use this tool for large JSON," which is what my corpus now says.

3. **Skip truncation when the result is going to be spilled to a file anyway.** If the transport
   already supports handing back an on-disk artifact, a large JSON body should take that path intact
   rather than being pre-truncated.

4. **Consider erroring instead of truncating for `application/json`,** when the prefix would drop
   more than some fraction of a top-level array. `_JSON_BUDGET_ERROR` already exists for the case
   where nothing useful fits; extending that posture to "we would silently drop 90% of an array" is
   consistent with it. A hard error is recoverable. A quiet wrong answer is not.

## Second, separate defect: large-output cache collision across parallel callers

Observed the same day, in the same sweep, and I believe it is unrelated to the truncation issue
above.

**Two concurrent agents each fetched a different company's bulk ATS JSON, and both received the same
company's payload** — one of the two requests returned content belonging to the other's URL. The
affected agent noticed only because the company name in the response did not match the board it had
asked for, and worked around it with `curl`.

I do not have a minimal reproduction — it surfaced under real parallel load, not a controlled test —
so treat this as a lead rather than a confirmed bug. But the shape suggests a cache key that is not
fully discriminating for large responses, possibly one that collides when two big payloads are
spilled or memoized concurrently. Worth checking whether the large-output path keys on something
narrower than the full request (URL + method + body + headers).

**Why it matters more than a normal cache bug:** the wrong-but-valid payload is silently wrong in
exactly the same way the truncation issue is. A sweep that trusts the response records "Company A has
no design roles" using Company B's board. Neither the caller nor the response carries any signal that
a substitution happened.

## Impact observed

Across one sweep of ~250 company job boards, this pattern produced or nearly produced false "this
company has zero design roles" conclusions on at least: OpenAI (7 of 734), MaintainX (70 of 154, and
8 of 154 on an earlier attempt), Veeva (8 of 500), Waabi (6 of 62), and the Harvey and Ashby boards.
Every one of those had to be re-fetched with `curl` to get a trustworthy answer.

The operational rule I have had to adopt: **for any JSON board expected to exceed ~50 elements, use
`curl` and not fetchaller, and always check the returned element count against the source's own
stated total before concluding anything is absent.** Fixes 1 and 2 above would let me drop that rule.
