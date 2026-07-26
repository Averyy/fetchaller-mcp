# TODO: Make server state survive restarts

**Status:** investigated, not fixed
**Investigated:** 2026-07-25 (against `e0adaa1`, v3.2.3)
**Impact:** every container restart forcibly un-pairs every OAuth connector, for every user, on every device

---

## TL;DR

The OAuth authorization server keeps *all* of its state in process memory, and it
generates a **new random JWT signing secret on every start** because `JWT_SECRET` is
never set in the deployed container. Restart the container and every access token ever
issued becomes invalid at once. Because the dynamic-client registry is also in memory,
Claude cannot silently re-authorize — it gets `invalid_client` — so the only recovery is
deleting and re-adding the connector by hand.

This is not a regression. It has behaved this way since the first commit. What changed is
**how often the container restarts**: four pushes to `main` on Jul 18–19 after a six-week
quiet period, each rebuilding `:latest`.

Two further bugs surfaced while verifying the data volume, both caused by `appuser` having no
usable `HOME` in the image:

- **Finding D** — the wafer cookie cache is silently non-functional in the production image,
  so cookies never persist and every request starts from a cold session.
- **Finding E** — Chromium was installed to `/root` at build time and is unreadable by the
  runtime user, so **browser-based challenge solving has never worked in Docker** (five
  months). Arguably the most impactful bug here, and the only one that is not about restarts.
  **Already fixed** in the working tree and verified by rebuild.

**Minimum viable fix:** set a fixed `JWT_SECRET` on the container ([Fix 1](#fix-1-set-a-fixed-jwt_secret-required)).
Everything else hardens the recovery path.

---

## Symptom

Re-pairing the connector in Claude Desktop was required twice in the week of Jul 18–25.
Historically it was only needed "after a major refactor" — which, it turns out, was
simply the only thing that used to restart the container.

## Blast radius

On each restart:

| State | Where | Survives restart? |
|---|---|---|
| OAuth access tokens (all users, all devices) | JWT signed with per-process secret | **No** |
| Registered OAuth clients (DCR) | `OAuthStore.clients` dict | **No** |
| In-flight authorization codes | `OAuthStore.auth_codes` dict | **No** |
| CSRF tokens for the authorize form | `routes._csrf_tokens` dict | No (harmless, 10 min TTL) |
| Raw `MCP_API_KEY` bearer auth | hashed from env var | **Yes** |
| wafer cookie cache | see Finding D | **No** (and never has) |

The last two rows matter for triage. Anything using
`Authorization: Bearer <MCP_API_KEY>` directly — local Claude Code config, curl, scripts —
keeps working across restarts, because `verify_bearer_auth` checks the raw key *before*
falling through to the JWT path (`src/fetchaller/http/middleware.py:174-182`). Only the
OAuth connector flow breaks. That asymmetry is most likely why this went unnoticed for
months.

---

## Finding A: the JWT signing secret is regenerated on every start (primary cause)

`src/fetchaller/http/app.py:44-67`

```python
if config.jwt_secret:
    jwt_secret = hashlib.sha256(config.jwt_secret.encode()).digest()
else:
    import secrets
    jwt_secret = secrets.token_bytes(32)      # <-- new secret every process start
    if api_key_hashes:
        print("... WARNING: JWT_SECRET not set ...", file=sys.stderr)
```

Access tokens are stateless JWTs (`security/crypto.py`, `OAuthStore.verify_token`) carrying
`{client_id, api_key_hash, exp}`. Verification is signature-only, so when the key changes
every outstanding token fails at once.

**Confirmed on the live deployment** (2026-07-25, container console on `tower`):

```
# printenv | grep -c JWT_SECRET
0
```

**The container cannot receive `JWT_SECRET` today:**

- `docker-compose.yml:13-22` — `JWT_SECRET` is absent from the `environment:` list.
  Compose only injects what is listed, so a value in the host `.env` cannot reach the process.
- `.dockerignore:4` — `.env` is excluded from the build context, so `config._load_dotenv()`
  (`config.py:70-88`) finds no file inside the image.

**Do not "fix" this by reverting to deriving the secret from the API key.** That derivation
was removed deliberately in `0a10a73` and the reasoning is in the comment at `app.py:46-53`:
token payloads carry `api_key_hash` in cleartext, so in single-key deployments the derivation
seed was recoverable from any captured token, letting an attacker forge tokens. The correct
fix is to *supply* a secret, not to re-derive one.

## Finding B: the client registry is in memory — why it is a re-pair, not a re-login

`src/fetchaller/http/oauth.py:41-61` — `OAuthStore.clients` is a plain dict. Dynamic Client
Registration (RFC 7591) results are lost on restart.

So after a restart the client is wedged: its stored token 401s, and when it retries
`/authorize` with its stored `client_id`, `routes.py:300-309` returns

```json
{"error":"invalid_client","error_description":"Unknown client_id"}
```

Note this store is **not** consulted when verifying a token, only when authorizing. That is
why Fix 1 alone restores day-to-day stability: with a stable secret, tokens keep validating
and the missing registry never gets hit. It only bites when a re-authorization is genuinely
needed (token expiry, key rotation, user-initiated reconnect).

## Finding C: there is no refresh-token grant, so there is no recovery path

`src/fetchaller/http/routes.py:102` advertises only `authorization_code`, and `/token`
(`routes.py:542-547`) returns no `refresh_token`. Combined with A and B, a client that loses
its token has no automatic way back — it is a manual re-pair or nothing.

This is currently masked by `ACCESS_TOKEN_TTL = 365 days` (`config.py:9`). That is a long
life for a bearer token that cannot be revoked individually (see the trade-off note at
`oauth.py:238-242`).

## Finding D: wafer cookie cache never persists in Docker

Found while confirming what the `cookie-data` volume actually holds. It holds nothing.

`config.py:119` defaults `WAFER_CACHE_DIR` to `Path.home()/".cache"/"fetchaller"/"wafer"`.
In the production image `appuser` has home `/home/appuser`, which **does not exist and cannot
be created** — the Dockerfile's `useradd -r` (`Dockerfile:11`) does not create it and `/home`
is root-owned 755.
Verified against `ghcr.io/averyy/fetchaller-mcp:latest`:

```
$ getent passwd appuser
appuser:x:99:100::/home/appuser:/bin/false
$ ls -ld /home
drwxr-xr-x 2 root root 4096 /home            # empty — no appuser dir

# as appuser:
target: /home/appuser/.cache/fetchaller/wafer | exists: False
mkdir FAILED: PermissionError [Errno 13] Permission denied: '/home/appuser'
```

wafer's `CookieCache._write_atomic` calls `mkdir(parents=True, ...)` (`wafer/_cookies.py:297`)
and its caller swallows the failure at debug level (`wafer/_async.py:107` inside a
`try/except Exception: logger.debug(...)`), so this fails **silently** on every response
carrying `Set-Cookie`.

Consequences: cold sessions on every request, more bot challenges than necessary, and
nothing persisted across restarts. Compare `e0adaa1` ("Reddit cold-session fix") — cold
sessions are exactly this symptom. Meanwhile `docker-compose.yml:23-24` mounts
`cookie-data:/app/data` and never points `WAFER_CACHE_DIR` at it, so the compose file reads
as though cookies persist when they cannot.

`/app/data` itself is fine — it exists and is owned by `appuser` (the entrypoint chowns it),
so it is a ready home for both this cache and the persisted OAuth state from Fix 2.

Host inspection also turned up a **stale artifact**: `/app/data/cookies.json` (50 KB, last
written Feb 22). Git shows that file was introduced by `6227752` (Feb 12, botfighter) and
deleted by `054654d` (Feb 26, the wafer migration). Nothing has written to the volume since.
It can be deleted.

## Finding E: the browser was unreachable in Docker — solver never worked (FIXED)

Found while investigating D; same root defect (`appuser` has no usable `HOME`), but far more
impactful, and it is **not** a persistence issue.

`Dockerfile` builds as **root**, so `python -m patchright install chromium` wrote the browser
to root's cache. The server runs as **appuser** via `gosu`, which resolves a different — and
unreadable — path:

```
browser installed to:      /root/.cache/ms-playwright/chromium_headless_shell-1217/...
/root permissions:         drwx------ (0700, root only)
server runs as:            appuser (uid 99), HOME=/home/appuser
PLAYWRIGHT_BROWSERS_PATH:  <unset>   -- no override
```

Verified against `ghcr.io/averyy/fetchaller-mcp:latest`, running as `appuser` with the real
runtime `HOME`:

```
FAILED: BrowserType.launch: Executable doesn't exist at
        /home/appuser/.cache/ms-playwright/chromium_headless_shell-1217/...

$ gosu appuser ls /root/.cache/ms-playwright/
ls: cannot access '/root/.cache/ms-playwright/': Permission denied
```

**This has never worked in Docker.** The non-root `appuser` landed in `8d209c0` (Feb 2); the
browser install landed in `054654d` (Feb 26, v3.1.0). The browser has never been installed
anywhere the server process could reach it.

Two things hid it for five months:

- The startup line `BrowserSolver available (browser launched on first challenge)`
  (`server.py:183`) only reports that the **Python import** succeeded. It says nothing about
  whether a browser binary exists, and the failure cannot surface until a challenge fires.
- Local stdio development runs as the developer's own user with their own browser install, so
  challenge solving works locally and fails only in the container.

**Fix applied** — two `ENV` lines in the `Dockerfile`:

```dockerfile
ENV PLAYWRIGHT_BROWSERS_PATH=/app/browsers   # must precede the install step
ENV WAFER_CACHE_DIR=/app/data/wafer          # Finding D
```

The existing `RUN chown -R appuser:appuser /app` covers the new directory. Verified by
rebuilding the image and launching as `appuser` with `HOME=/home/appuser`:

```
/app/browsers  ->  drwxr-xr-x appuser users
CHROMIUM LAUNCH OK
```

Because both are baked into the image as `ENV`, they take effect on the next image pull with
no deployment/template change required (unlike `JWT_SECRET`).

**Still worth doing:** make the `BrowserSolver available` log line verify the executable
exists rather than only that the import succeeded. A misleading "available" is precisely what
let this hide.

---

## Why it surfaced now

`.github/workflows/docker.yml` rebuilds and pushes `ghcr.io/averyy/fetchaller-mcp:latest`
on **every push to `main`**. Recent successful runs:

```
2026-07-19 12:48Z  v3.2.3  Require wafer-py 0.3.3
2026-07-18 22:25Z  v3.2.2  Harden SSRF, fix cache/queue bugs
2026-07-18 19:05Z  v3.2.1  Adopt wafer 0.3.2 APIs
2026-07-18 14:21Z          DNS pinning SSRF fix
2026-06-08 12:29Z          <- previous push, six weeks earlier
```

Four image updates in ~22 hours. With `restart: unless-stopped` and any auto-pull of
`:latest`, that is up to four pairing wipes in a day. Before that, pushes were weeks apart —
hence "stable unless I made a major refactor."

**However, deploys are not the only trigger.** Host inspection on 2026-07-25 showed:

```
$ docker inspect fetchaller-mcp --format '{{.RestartCount}} {{.State.StartedAt}} {{.State.ExitCode}}'
0 2026-07-25T08:14:18.411236593Z 0
```

That start is **six days after the last image push**. Follow-up inspection identified a
recurring scheduled job as the real trigger:

```
$ uptime
 17:33:41 up 56 days, 20:25          # no host reboot

$ docker inspect fetchaller-mcp --format '{{.Created}} | {{.State.StartedAt}}'
2026-07-20T08:15:09Z | 2026-07-25T08:14:18Z
```

Reading those together:

- **56-day host uptime** rules out a reboot.
- **`Created` (Jul 20) ≠ `StartedAt` (Jul 25)** means the same container was stopped and
  started — *not* recreated, so the Jul 25 event was not an image update.
- **`RestartCount 0`** means the restart policy did not do it. Policy restarts after a crash
  increment that counter; an explicit `docker restart` does not. Something deliberately
  stopped and started it.
- Both timestamps land at **~04:14–04:15 local** (America/Toronto), five days apart. That is a
  scheduled job, not chance.

The two events differ in kind, which fits: **Jul 20 08:15Z** was a *recreate* — an
auto-updater pulling the image pushed Jul 19 12:48Z the following morning. **Jul 25 08:14Z**
was a plain *restart* with no new image available, so it is a different job — an appdata
backup or equivalent that stops/starts containers unconditionally.

So restarts come from **two** sources: deploys, and a recurring overnight task. If the latter
runs nightly, the pairing breaks nightly and is only *noticed* on days the Desktop connector
is actually used (routine work goes through the local stdio config, which never touches OAuth).

`RestartCount 0` / `ExitCode 0` also **rules out crash-restarts and OOM kills** as a
contributing cause (caveat: `RestartCount` resets on recreate, so it only covers the current
instance).

Naming the job — for awareness only, it does not affect the fix:

```bash
cat /etc/cron.d/* 2>/dev/null | grep -iE "backup|update|docker"
grep -iE "fetchaller|appdata|ca.update|backup" /var/log/syslog | tail -40
```

**This does not change any fix below.** A stable `JWT_SECRET` makes the restart *source*
irrelevant. Identifying the trigger only tells you how often it was happening.

**Still to confirm** — whether those restarts were image updates or crashes. This must run on
the Unraid **host** (SSH or the web terminal), not the container console, which has no Docker
CLI:

```bash
docker inspect fetchaller-mcp \
  --format '{{.RestartCount}} {{.State.StartedAt}} {{.State.ExitCode}}'
```

Exit code `137` would mean OOM kills against the 3 GB limit (`docker-compose.yml:31-35`),
which is plausible now that the image ships patchright Chromium. Same fixes either way, but
it would reveal a second problem underneath.

---

## Reproduction

Verified against the real code path (register → PKCE authorize → token → call `/mcp` →
restart → call again). Script in the appendix.

```
JWT_SECRET=''                  BEFORE restart: /mcp -> 200    AFTER: /mcp -> 401
JWT_SECRET='fixed-secret'      BEFORE restart: /mcp -> 200    AFTER: /mcp -> 200
```

In **both** runs, `/authorize` with the pre-restart `client_id` returned
`{"error":"invalid_client"}` — that is Finding B, and it is unaffected by Fix 1.

---

## Fixes, in priority order

### Fix 1: set a fixed `JWT_SECRET` (required)

Deployment/config only, no application logic. Resolves the reported symptom.

1. Generate once: `openssl rand -hex 32`
2. Store it in the host's compose `.env` (or the Unraid template's env vars)
3. `docker-compose.yml` — add to `environment:`:
   ```yaml
   - JWT_SECRET=${JWT_SECRET:?JWT_SECRET must be set - see todo-survivereboots.md}
   ```
   The `:?` form fails the deploy loudly rather than silently starting with an ephemeral
   secret, which is what let this hide for months.
4. `.env.sample` — document it with a generation command.
5. `README.md:425` — currently says `JWT_SECRET | (derived from API key) | Secret for OAuth
   tokens`. That has been false since `b351d00d` (Feb 3) and is very likely why it was never
   set. Correct it to: required in HTTP mode; random per-process if unset, which invalidates
   all OAuth pairings on restart.

**Acceptance:** pair the connector, `docker compose restart`, and confirm the connector still
works with no user action.

**Consider:** promote the `app.py:60-67` warning to a hard startup failure in HTTP mode when
API keys are configured but `JWT_SECRET` is unset, with an explicit opt-out
(e.g. `ALLOW_EPHEMERAL_JWT=1`) for local dev. `docker-compose.local.yml:29` has `JWT_SECRET`
commented out and would need updating. Decide with Avery — see [Open decisions](#open-decisions).

**Note:** rotating `JWT_SECRET` later invalidates every pairing by design. That is the only
revocation mechanism the current design has.

### Fix 2: persist the OAuth client registry

Without this, any future re-authorization still dead-ends at `invalid_client` and the user
is back to a manual re-pair.

- Persist `OAuthStore.clients` to `${DATA_DIR:-/app/data}/oauth_clients.json`.
- Load in `OAuthStore.from_config`; save on `register_client()` and after `_cleanup()` evicts.
- **Atomic writes** (`tempfile.mkstemp` + `os.rename` in the same dir) — `wafer/_cookies.py:287-315`
  is a good in-repo model.
- **Mode `0o600`**, dir `0o700`: the file holds `client_secret` values.
- Preserve the `client_ttl` sweep on load so a stale file cannot resurrect expired clients.
- **Change the capacity behaviour before persisting.** `register_client` (`oauth.py:113-139`)
  currently returns `None` → HTTP 503 once `max_clients` (1000) is reached, and `_cleanup()`
  only evicts clients older than `client_ttl` (365 days). Today a restart silently clears the
  registry, which accidentally serves as the relief valve. Persisting it removes that valve:
  once full, registration would hard-fail for a *year*, and `/register` is unauthenticated
  (rate-limited to 5/IP/hour, so ~200 IPs fills it). Switch to LRU eviction of the oldest
  clients instead of refusing new ones, or the fix introduces a denial-of-service.
- Corrupt/unreadable file must not prevent startup: log and start with an empty registry.
- Auth codes (10-min TTL) are **not** worth persisting — losing them mid-pairing just means
  the user retries. Skip unless it's free.
- Concurrency: fine for the current single-worker uvicorn. If multiple workers or replicas are
  ever introduced, a JSON file is no longer safe — note it in the code and see
  [Open decisions](#open-decisions).

**Acceptance:** register a client, restart, and confirm `/authorize?client_id=<old>` returns
the login page instead of `invalid_client`.

### Fix 3: add the `refresh_token` grant

Claude Desktop expects refresh tokens; their absence means there is no silent recovery from
*any* token loss.

- Add `refresh_token` to `grant_types_supported` (`routes.py:102`).
- Issue one from `/token` alongside the access token.
- Add a `grant_type=refresh_token` branch (currently hard-rejected at `routes.py:490`).
- **Rotate on use** — OAuth 2.1 requires rotation for public clients; invalidate the old one.
- Refresh tokens need to survive restarts, so land this on Fix 2's persistence layer
  (store hashes, not raw values).
- Once refresh works, **reduce `ACCESS_TOKEN_TTL`** from 365 days (`config.py:9`) to something
  defensible (1–24 h). Do not shorten it before refresh tokens exist — that would make the
  re-pair problem dramatically worse.

### Fix 4: make the wafer cookie cache actually work (Finding D)

Two viable approaches; the first is preferred since the volume already exists for this purpose:

- **Point it at the mounted volume.** Set `WAFER_CACHE_DIR=/app/data/wafer` in
  `docker-compose.yml` and as an `ENV` default in the `Dockerfile`. `/app/data` is already
  chowned to `appuser` by `entrypoint.sh`.
- **Or** give `appuser` a real home: `useradd -m -d /home/appuser` (or `mkdir -p` + `chown`)
  in the Dockerfile. Works, but leaves the cache outside the volume, so it still dies on
  container recreation.

Also add a startup writability check — attempt to create the cache dir at boot and log an
error if it fails. wafer swallows the error at debug level, so today there is no signal at
all that caching is dead.

**Acceptance:** after a fetch against a cookie-setting site, `/app/data/wafer/` contains JSON
files, and they are still there and reused after `docker compose restart`.

### Fix 5: documentation

- `README.md:425` (covered in Fix 1).
- `docs/architecture.md` — add a short "persistent state" section: what lives in `/app/data`,
  what is memory-only and why, and what a restart destroys.
- Note in the README's deployment section that `JWT_SECRET` must be stable across redeploys.

---

## Test plan

`tests/test_oauth.py` and `tests/test_config.py` already exist — extend rather than add new files.

Unit:
- A token signed under secret A fails verification under secret B.
- With a fixed `JWT_SECRET`, a token issued by one `create_app()` verifies against a second,
  independently constructed one.
- Client registry round-trips through save/load; expired clients are dropped on load; a
  corrupt file yields an empty registry rather than an exception.
- Refresh-token rotation: the old token is rejected after use.
- Raw-API-key auth is unaffected by any of the above (guard against regressing the path that
  currently works).

Manual (required by `CLAUDE.md` — unit tests alone are not sufficient):
- Run the appendix script in both modes; expect `200/401` unset and `200/200` set.
- Full pairing against the real deployment, then `docker compose restart`, then use a tool.

Before every commit (`CLAUDE.md`):
```bash
.venv/bin/ruff check src/ tests/
.venv/bin/python -m pytest tests/ -x -q
```

---

## Open decisions

1. **Fail fast on missing `JWT_SECRET` in HTTP mode?** Safer, but breaks
   `docker-compose.local.yml` as written and any bare `--http` dev run. Recommend yes, with an
   `ALLOW_EPHEMERAL_JWT=1` opt-out.
2. **Access token TTL after Fix 3** — 1 h or 24 h. Shorter is better once refresh works.
3. **Persistence layer for Fix 2/3** — JSON file (simple, single-worker only) vs SQLite
   (concurrency-safe, negligible extra weight). Depends on whether multi-worker/replica
   deployment is ever planned.
4. **Persist auth codes?** Probably not; 10-minute TTL, cheap to retry.
5. **Should `/health` report whether the JWT secret is ephemeral?** Would have caught this in
   minutes. Must not leak the secret — a boolean only.

---

## File map

| File | Relevance |
|---|---|
| `src/fetchaller/http/app.py:44-67` | Finding A — random secret generation |
| `src/fetchaller/http/oauth.py:41-61` | Finding B — in-memory client/code stores |
| `src/fetchaller/http/oauth.py:216-258` | Stateless token issue/verify |
| `src/fetchaller/http/routes.py:102` | Advertised grant types (Finding C) |
| `src/fetchaller/http/routes.py:300-309` | `invalid_client` after restart |
| `src/fetchaller/http/routes.py:455-547` | `/token` — no refresh token issued |
| `src/fetchaller/http/middleware.py:174-182` | Raw-key path that survives restarts |
| `src/fetchaller/config.py:7-10` | `ACCESS_TOKEN_TTL` = 365 d, `CLIENT_TTL` = 365 d |
| `src/fetchaller/config.py:119` | `WAFER_CACHE_DIR` default (Finding D) |
| `docker-compose.yml:13-24` | Missing `JWT_SECRET`; unused `cookie-data` volume |
| `.dockerignore:4` | `.env` excluded from image |
| `Dockerfile:11` | `useradd -r` with no home dir (root cause of D and E) |
| `Dockerfile` (ENV) | `PLAYWRIGHT_BROWSERS_PATH` / `WAFER_CACHE_DIR` — fixes for E and D |
| `src/fetchaller/server.py:183` | Misleading "BrowserSolver available" log (Finding E) |
| `README.md:425` | Incorrect `JWT_SECRET` documentation |

---

## Appendix: reproduction script

Save as `scripts/repro-restart-pairing.sh`. Takes the `JWT_SECRET` value as `$1`
(empty string reproduces the production config).

```bash
#!/bin/bash
# Does an OAuth pairing survive a server restart?
# Usage: repro-restart-pairing.sh ""                 # production config -> expect 200 then 401
#        repro-restart-pairing.sh "fixed-secret"     # fixed secret      -> expect 200 then 200
set -u
PORT=6099
REPO=/Users/avery/Code/fetchaller-mcp
VENV=$REPO/.venv/bin/python
export MCP_API_KEY=testkey123
export MCP_SERVER_URL="http://localhost:$PORT"
export HTTP_PORT=$PORT
export JWT_SECRET="${1:-}"
export WAFER_CACHE_DIR=""

start() {
  $VENV -m fetchaller.main --http >/tmp/fetchaller_repro.log 2>&1 &
  echo $! > /tmp/fetchaller_repro.pid
  for _ in $(seq 1 40); do
    curl -sf "http://localhost:$PORT/health" >/dev/null && return 0
    sleep 0.5
  done
  echo "server failed to start"; cat /tmp/fetchaller_repro.log; exit 1
}
stop() { kill "$(cat /tmp/fetchaller_repro.pid)" 2>/dev/null; wait "$(cat /tmp/fetchaller_repro.pid)" 2>/dev/null; sleep 1; }

echo "### JWT_SECRET='${JWT_SECRET}'  (empty = production config)"
cd "$REPO"
start

# 1. Dynamic client registration - what Claude Desktop does when you add a connector
CLIENT_ID=$(curl -s -X POST "http://localhost:$PORT/register" -H 'Content-Type: application/json' \
  -d '{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"],"client_name":"Claude"}' \
  | $VENV -c 'import sys,json; print(json.load(sys.stdin)["client_id"])')
echo "registered client_id=$CLIENT_ID"

# 2. PKCE challenge + fetch the authorize form (for its CSRF token)
VERIFIER=$($VENV -c 'import secrets;print(secrets.token_urlsafe(48))')
CHALLENGE=$($VENV -c "
import hashlib,base64,sys
v=sys.argv[1].encode()
print(base64.urlsafe_b64encode(hashlib.sha256(v).digest()).rstrip(b'=').decode())" "$VERIFIER")
CSRF=$(curl -s "http://localhost:$PORT/authorize?client_id=$CLIENT_ID&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&response_type=code&code_challenge=$CHALLENGE&code_challenge_method=S256" \
  | grep -o 'name="csrf_token" value="[^"]*"' | sed 's/.*value="//;s/"//')

# 3. Submit the API key -> authorization code
CODE=$(curl -s -X POST "http://localhost:$PORT/authorize" \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "redirect_uri=https://claude.ai/api/mcp/auth_callback" \
  --data-urlencode "code_challenge=$CHALLENGE" \
  --data-urlencode "api_key=$MCP_API_KEY" \
  --data-urlencode "csrf_token=$CSRF" \
  | grep -o 'code=[A-Za-z0-9_-]*' | head -1 | cut -d= -f2)

# 4. Exchange for an access token
TOKEN=$(curl -s -X POST "http://localhost:$PORT/token" -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=authorization_code" --data-urlencode "code=$CODE" \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "redirect_uri=https://claude.ai/api/mcp/auth_callback" \
  --data-urlencode "code_verifier=$VERIFIER" \
  | $VENV -c 'import sys,json; print(json.load(sys.stdin).get("access_token","NONE"))')
echo "got access token: ${TOKEN:0:25}..."

probe() {
  curl -s -o /dev/null -w '%{http_code}' -X POST "http://localhost:$PORT/mcp" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
}
echo "BEFORE restart: /mcp -> HTTP $(probe)"

stop; start
echo "AFTER  restart: /mcp -> HTTP $(probe)"

echo -n "AFTER  restart: /authorize with stored client_id -> "
curl -s "http://localhost:$PORT/authorize?client_id=$CLIENT_ID&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&response_type=code&code_challenge=$CHALLENGE&code_challenge_method=S256" | head -c 120
echo
stop
```
