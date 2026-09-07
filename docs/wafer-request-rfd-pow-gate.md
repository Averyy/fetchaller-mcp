# Proof-of-work gate on redflagdeals.com — measurements for wafer

> **RESOLVED in wafer (2026-09-07, unreleased on main as of writing).** wafer
> now detects the gate as `ChallengeType.POW` on any status and solves it
> inline: `solve_pow` in `wafer/_solvers.py` runs the hash loop, writes the
> five-field `pow_bypass` cookie on `.redflagdeals.com` for `cookie_duration`,
> and the session replays. Verified live: cold `wafer.get()` on the §6 thread
> URL returns 200 with the thread; the www front page then loads on the same
> jar with no re-solve; a cold `AsyncSession` also passes. The §3
> transcription was correct as written (counter 90 for the measured nonce).
> Reference: `docs/ref-pow.md` in wafer. fetchaller action once the next wafer
> release ships: bump the floor and add a thread smoke test; no code change.

**Verdict: this is a wafer issue, not a fetchaller issue.** wafer returns a
challenge page as an ordinary success and fetchaller renders it as (empty)
content. The challenge is a SHA-256 proof-of-work that the page's own script
solves in plain JavaScript; it needs no browser, no vendor SDK, and no
CAPTCHA. It is the ACW shape: pure computation, then set a cookie and replay.

This is a report, not a design. Everything in §1–§2 and §4 was measured live;
§3 is transcribed from the challenge script itself. How to handle it is
wafer's call.

Measured 2026-09-07 against wafer 0.4.9 from a residential Canadian
connection. Every number came from a live request.

Target: `redflagdeals.com` — both `www.redflagdeals.com` (the deals front
page) and `forums.redflagdeals.com` (phpBB). fetchaller has supported RFD
forum threads since the generic-forum work, and they stopped working when
this gate went up. The gate is home-grown: `Server: Varnish`, no vendor
markers, no third-party script.

---

## 1. What the block looks like

A clean **HTTP 202**, 2,671-byte body, `text/html`, same on every path of
both hosts. No error, no non-2xx status in the sense any client checks for,
no redirect:

```
GET https://forums.redflagdeals.com/viewtopic.php?t=2789391
  -> 202  2,671 bytes
     server: Varnish
     cache-control: no-store, private
     set-cookie: pow_trace=<32 hex>|<issued_at>; domain=.redflagdeals.com; path=/;
                 max-age=86400; SameSite=Lax; Secure
```

Identical answer (202, 2,671 bytes, fresh nonce each time) for:

| URL | result |
|---|---|
| `https://www.redflagdeals.com/` | 202 gate |
| `https://forums.redflagdeals.com/` | 202 gate |
| `https://forums.redflagdeals.com/hot-deals-f9/` | 202 gate |
| `https://forums.redflagdeals.com/<slug>-2789391/` | 202 gate |
| `https://forums.redflagdeals.com/viewtopic.php?t=2789391` | 202 gate |
| `https://forums.redflagdeals.com/search.php?keywords=roborock` | 202 gate |
| `https://forums.redflagdeals.com/feed/forum/9` | **200**, Atom, 19,711 b |
| `https://forums.redflagdeals.com/feed/topic/2789391` | **200**, Atom, 11,725 b |
| `https://forums.redflagdeals.com/robots.txt` | **200** |

So the gate is host-wide with the feed routes and `robots.txt` exempt. That
exemption is why fetchaller's forum *listing* path kept working (it reads
`/feed/forum/{id}`) while every thread, the front page, and search all went
dark at once.

The body is a `<head>` holding one inline script and a `<noscript>` saying
"JavaScript Required". Nothing else. Verbatim head of the script:

```js
window.POW_CHALLENGE_DATA={
    challenge_nonce:'b413c64d9e35eeb0ed325e2dc4d8fb3d',
    challenge_hmac:'5432fc7d85cd4baa7f02baa4',
    difficulty:'2',
    difficulty_char:'b',
    issued_at:'1788820622',
    cookie_duration:'3600',
    cookie_domain:'.redflagdeals.com',
    referrer:'(null)',
    headless_check:'1'
};
```

The `pow_trace` cookie value is `challenge_nonce|issued_at` — the same two
values the script hashes over — with a 24-hour lifetime.

## 2. `detect_challenge` returns `None`

```python
detect_challenge(202, {"server": "Varnish", ...}, body)  ->  None
detect_challenge(200, {}, body)                           ->  None
detect_challenge(403, {}, body)                           ->  ChallengeType.GENERIC_JS
```

The generic-JS arm would catch it (small body, `<script>` present) but that
arm is gated to 403/429, and this site never sends either. Nothing in
`ChallengeType` names a proof-of-work gate, and `POW_CHALLENGE_DATA` /
`pow_bypass` / `pow_trace` appear nowhere in `wafer/_challenge.py`.

**Downstream effect.** fetchaller treats any status below 400 as content
(`tools/fetch.py`, the `status_code >= 400` check). Its recovery path for
blocks — catching `wafer.ChallengeDetected` and escalating to `render()` —
never fires, because no exception is raised. The 2,671-byte page then goes
through the HTML pipeline, which strips the `<script>` and `<noscript>`, and
the tool returns an **empty document** for a live thread. A user reads that as
"threads are JavaScript-gated", which is what happened here. That
silent-success path is the whole reason this is filed against wafer.

## 3. The challenge, transcribed from the page script

The solve is entirely client-side and the script is not obfuscated. Reading
it out:

```
for i in 1, 2, 3, ... (ceiling 10,000,000):
    h = sha256_hex( challenge_nonce + issued_at + str(i) )
    if h.startswith( difficulty_char * int(difficulty) ):
        break

cookie  pow_bypass = challenge_nonce | issued_at | i | h | challenge_hmac [ | signals ]
        domain=<cookie_domain>; path=/; max-age=<cookie_duration>; SameSite=Lax; Secure

then location.reload()   (or, if the referrer was a search engine, reload with
                          utm_source/utm_medium appended — irrelevant to us)
```

Details that matter for an implementation:

- **The concatenation is plain string concat, no separator**, in the order
  `nonce + issued_at + counter`, counter as decimal text starting at 1. The
  digest is hex, lower-case, and compared by prefix.
- **`difficulty` / `difficulty_char` are data, not constants.** Today they
  are `2` and `b` (two leading `b` nibbles, so 1 in 256 hashes; ~256 SHA-256
  calls expected, microseconds in Python). Read both from the page.
- **`challenge_hmac` is opaque and must be echoed back unchanged.** It is 24
  hex chars, almost certainly a server-side MAC over `nonce|issued_at`, so
  the pair cannot be minted locally and a stale `issued_at` is presumably
  rejected. Solve against the values in the page you were just served.
- **`headless_check:'1'` appends a signals field only when a signal fires.**
  The script probes `navigator.webdriver`, WebGL availability, and (mobile
  UA only) a touch-points mismatch, and joins whatever fired with commas as
  a sixth `|` field. A clean browser fires none and sends the five-field
  cookie with no trailing field. An HTTP client should therefore send the
  five-field form; appending an empty sixth field would be a shape the
  script never produces.
- **The cookie lives `cookie_duration` seconds (3,600 today) on
  `.redflagdeals.com`**, so one solve covers both hosts for an hour. It is
  worth persisting to the cookie cache with that expiry rather than
  re-solving per session.
- **The page sets `pow_trace` alongside.** Whether the server requires it to
  accompany `pow_bypass` was not isolated; replaying on the same jar keeps
  it, which is the cheap and correct default.

**The solve-and-replay was not executed from this session.** The tooling this
was written in refused to run the hash loop, so §3 is a transcription of the
script, not a measured pass. §6 is the script to run; the expected result is
a 200 carrying the thread's HTML (phpBB markup, `<title>` naming the deal).
If it does not pass on the first try, the `pow_trace` cookie and the
`issued_at` freshness window are the two things to vary before anything else.

## 4. What was not varied

- **No other TLS identity was tried.** The gate is not fingerprint-based on
  its face — it hands every client the same script and asks for work — so a
  rotation ladder is not expected to help and was not spent against a site
  that rate-limits.
- **The IP is not the problem.** Residential Canadian connection, and the
  page is not a denial: it issues a challenge with a fresh nonce on every
  request and tells the client exactly how to pass.

## 5. Two things about this caller

fetchaller builds its sessions with `follow_redirects=False` and walks
redirects itself. There is no redirect here — the 202 is the origin's final
answer — so the hop loop is not involved and the replay is a straight
re-request of the same URL on the same jar.

As with Radware, **wafer should own the replay**. fetchaller will not replay
a 202 on its own; if it did, the two would double up.

fetchaller-side note for the record, not a request: the exempt
`/feed/topic/{id}` route returns the thread's **most recent 8 posts** as Atom,
newest first. That is a partial view (no opening post on a long thread, no
pagination) and is not a substitute for the page, but it is what fetchaller
can reach today without a solve.

## 6. Repro

Current broken behaviour (all assertions pass on wafer 0.4.9):

```python
import wafer
from wafer._challenge import detect_challenge

URL = "https://forums.redflagdeals.com/viewtopic.php?t=2789391"

r = wafer.get(URL, timeout=45)
assert r.status_code == 202
assert "POW_CHALLENGE_DATA" in r.text
assert "pow_trace=" in r.headers.get("set-cookie", "")
assert detect_challenge(r.status_code, dict(r.headers), r.text) is None
```

Expected behaviour after a fix, and the manual check that §3 is right:

```python
import hashlib, re, wafer

URL = "https://forums.redflagdeals.com/viewtopic.php?t=2789391"

with wafer.SyncSession() as s:
    r = s.get(URL, timeout=45)
    assert r.status_code == 202
    d = dict(re.findall(r"(\w+):'([^']*)'",
                        r.text.split("POW_CHALLENGE_DATA=")[1].split("};")[0]))
    prefix = d["difficulty_char"] * int(d["difficulty"])
    i = 0
    while True:
        i += 1
        h = hashlib.sha256((d["challenge_nonce"] + d["issued_at"] + str(i)).encode()).hexdigest()
        if h.startswith(prefix):
            break
    cookie = f"pow_bypass={d['challenge_nonce']}|{d['issued_at']}|{i}|{h}|{d['challenge_hmac']}"
    r2 = s.get(URL, timeout=45, headers={"Cookie": cookie})   # same jar keeps pow_trace
    assert r2.status_code == 200
    assert "POW_CHALLENGE_DATA" not in r2.text
    assert "<title>" in r2.text                                  # the thread page
```

Once shipped, the first block should read simply:

```python
r = wafer.get(URL, timeout=45)      # cold
assert r.status_code == 200
assert "POW_CHALLENGE_DATA" not in r.text
```

Topic `2789391` is a real thread (a Bose headphones deal from November 2025);
any live topic id works, and the gate fires on the front page too, so the URL
is not load-bearing.
