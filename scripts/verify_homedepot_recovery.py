"""Require a real 206 -> guarded product-page solve -> HTTP product 200 via MCP.

The only challenge trigger is a real unsolved product GET in the gateway's
own wafer session, followed by five seconds of spacing. No response or cookie
is fabricated. Private diagnostic hooks observe wafer; production fetchaller
does not prime pages, inspect cookies, or implement recovery.

    uv run --no-sync python scripts/verify_homedepot_recovery.py --wafer-path ../wafer
    uv run --no-sync python scripts/verify_homedepot_recovery.py \
        --wafer-path ../wafer --docker-image fetchaller-mcp:test
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PRODUCT_URL = "https://www.homedepot.com/p/204279858"
PREFIX = "HD_RECOVERY "


def record(event, **fields):
    print(PREFIX + json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


async def serve(prime_url):
    import wafer
    import wafer._async as wafer_async
    from wafer.browser import BrowserSolver

    from fetchaller.config import load_config
    from fetchaller.homedepot import com
    from fetchaller.main import run_stdio_mode
    from fetchaller.security.browser_proxy import BrowserEgressProxy
    from fetchaller.server import _AuditedBrowserSolver

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    record("source", wafer=str(Path(wafer.__file__).resolve()))
    original_session = com.get_gateway_session
    original_detect = wafer_async.is_akamai_api_refusal
    original_solve = _AuditedBrowserSolver.asolve
    original_setup = BrowserSolver._setup_headless_patches
    original_post = wafer.AsyncSession.post
    original_approved_ips = BrowserEgressProxy._approved_ips
    primed = False

    async def primed_session(browser_solver=None):
        nonlocal primed
        if not isinstance(browser_solver, _AuditedBrowserSolver):
            raise RuntimeError("Gateway did not receive the server's audited solver")
        if not (browser_solver.egress_guard_proxy or "").startswith("socks5://127.0.0.1:"):
            raise RuntimeError("Gateway solver is missing the loopback egress guard")
        session = await original_session(browser_solver)
        if not primed:
            primed = True
            # Preserve the site's own cookie jar in this exact gateway session.
            saved_solver, saved_rotations = session._browser_solver, session.max_rotations
            session._browser_solver, session.max_rotations = None, 0
            try:
                response = await session.get(prime_url, timeout=30)
            finally:
                session._browser_solver, session.max_rotations = saved_solver, saved_rotations
            record("prime", url=prime_url, status=response.status_code, challenge=response.challenge_type,
                   cookie_names=[c["name"] for c in session.cookie_scope_summary(PRODUCT_URL)])
            if response.challenge_type != "akamai":
                raise RuntimeError("Unsolved product GET did not trigger Akamai; recovery is unverified")
            await asyncio.sleep(5)
        return session

    def observed_detect(status, headers, body):
        refused = original_detect(status, headers, body)
        if refused:
            record("refusal", status=status, server=headers.get("server"))
        return refused

    async def observed_solve(self, url, *args, **kwargs):
        record("solve_start", url=url, embedder=kwargs.get("embedder"))
        result = await original_solve(self, url, *args, **kwargs)
        record("solve_end", cookie_count=len(result.cookies) if result else 0,
               passthrough=bool(result and result.response is not None))
        return result

    def observed_setup(self, page, *args, **kwargs):
        def response_seen(response):
            if response.request.resource_type == "document" and "www.homedepot.com/p/" in response.url:
                record("browser_document", status=response.status, url=response.url)
        page.on("response", response_seen)
        return original_setup(self, page, *args, **kwargs)

    def observed_approved_ips(self, host):
        try:
            return original_approved_ips(self, host)
        except Exception as exc:
            record("egress_rejected", host=host, error=type(exc).__name__)
            raise

    async def observed_post(self, url, **kwargs):
        if not url.startswith(com.GATEWAY):
            return await original_post(self, url, **kwargs)
        record("post_start", url=url, referer=kwargs.get("headers", {}).get("Referer"))
        started = time.monotonic()
        response = await original_post(self, url, **kwargs)
        product = (response.json().get("data") or {}).get("product") or {}
        record("post_end", status=response.status_code, server=response.headers.get("server"),
               item_id=product.get("itemId"), model=(product.get("identifiers") or {}).get("modelNumber"),
               elapsed=round(time.monotonic() - started, 2))
        return response

    com.get_gateway_session = primed_session
    wafer_async.is_akamai_api_refusal = observed_detect
    _AuditedBrowserSolver.asolve = observed_solve
    BrowserSolver._setup_headless_patches = observed_setup
    wafer.AsyncSession.post = observed_post
    BrowserEgressProxy._approved_ips = observed_approved_ips
    await run_stdio_mode(load_config())


async def verify(args):
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    paths = [str(root / "src")]
    wafer_path = Path(args.wafer_path).resolve() if args.wafer_path else None
    if wafer_path:
        if not (wafer_path / "wafer" / "__init__.py").is_file():
            raise ValueError("--wafer-path must name the wafer working tree")
        paths.insert(0, str(wafer_path))
    if args.docker_image:
        command = "docker"
        argv = ["run", "--rm", "-i", "--platform", "linux/amd64",
                "-v", f"{root}:/work:ro", "-e", "BROWSER_PREFLIGHT=0"]
        if wafer_path:
            argv += ["-v", f"{wafer_path}:/wafer:ro"]
        argv += ["-e", "PYTHONPATH=" + ("/wafer:" if wafer_path else "") + "/work/src",
                 args.docker_image, "uv", "run", "--no-sync", "python",
                 "/work/scripts/verify_homedepot_recovery.py", "--server", "--prime-url", args.prime_url]
    else:
        command = sys.executable
        argv = [str(Path(__file__).resolve()), "--server", "--prime-url", args.prime_url]
        env["PYTHONPATH"] = os.pathsep.join(paths)
        # Preflight downloads unrelated models; the real solve still launches
        # the normal guarded browser and its readiness checks remain enabled.
        env["BROWSER_PREFLIGHT"] = "0"
    with tempfile.TemporaryDirectory(prefix="hd-recovery-") as data:
        env["DATA_DIR"] = data
        env["WAFER_CACHE_DIR"] = str(Path(data) / "wafer")
        params = StdioServerParameters(command=command, args=argv, env=env, cwd=str(root))
        with (output / "server.log").open("w+") as diagnostic:
            async with stdio_client(params, errlog=diagnostic) as streams:
                async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=180)) as client:
                    await client.initialize()
                    started = time.monotonic()
                    response = await client.call_tool("fetch", {"url": PRODUCT_URL, "timeout": 90})
                    content = "\n".join(block.text for block in response.content if block.type == "text")
                    (output / "product.md").write_text(content)
                    elapsed = time.monotonic() - started
            diagnostic.seek(0)
            logs = diagnostic.read()
    events = [json.loads(line[len(PREFIX):]) for line in logs.splitlines() if line.startswith(PREFIX)]
    for event in events:
        print(json.dumps(event), flush=True)
    audit_lines = [line for line in logs.splitlines() if "BROWSER_DISPATCH_SUMMARY" in line or "BROWSER_EGRESS_SUMMARY" in line]
    for line in audit_lines:
        print(line, flush=True)
    kinds = [event["event"] for event in events]
    expected = ["prime", "post_start", "refusal", "solve_start", "solve_end", "post_end"]
    positions = [kinds.index(kind) if kind in kinds else -1 for kind in expected]
    checks = {
        "sequence": all(i >= 0 for i in positions) and positions == sorted(positions),
        "single_solve": kinds.count("solve_start") == 1,
        "product_referer": any(e["event"] == "post_start" and e["referer"] == PRODUCT_URL for e in events),
        "product_solve": any(e["event"] == "solve_start" and e.get("embedder") == PRODUCT_URL for e in events),
        "browser_product_200": any(e["event"] == "browser_document" and e["status"] == 200 and e["url"].endswith("/204279858") for e in events),
        "solver_cookies": any(e["event"] == "solve_end" and e["cookie_count"] > 0 and not e["passthrough"] for e in events),
        "http_product_200": any(e["event"] == "post_end" and e["status"] == 200 and e["server"] == "envoy" and e["item_id"] == "204279858" and e["model"] == "DCD771C2" for e in events),
        "rendered_product": not response.isError and "DCD771C2" in content and "204279858" in content,
        "audited_dispatch": bool(re.search(r"BROWSER_DISPATCH_SUMMARY total=1 reddit=0", logs)),
        # Rejected third-party destinations mean the guard is enforcing its
        # policy, not a failed recovery. Never loosen it to get zero denials.
        "guarded_egress": bool(re.search(r"BROWSER_EGRESS_SUMMARY allowed=[1-9]\d* denied=\d+", logs)),
    }
    if wafer_path:
        expected_source = "/wafer/wafer/__init__.py" if args.docker_image else str(wafer_path / "wafer" / "__init__.py")
        checks["wafer_source"] = any(e["event"] == "source" and e["wafer"] == expected_source for e in events)
    summary = {"passed": all(checks.values()), "elapsed": round(elapsed, 2), "checks": checks}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wafer-path")
    parser.add_argument("--docker-image")
    parser.add_argument("--prime-url", default=PRODUCT_URL)
    parser.add_argument("--output", default="/tmp/fetchaller-hd-recovery")
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.prime_url.startswith("https://www.homedepot.com/p/") or not args.prime_url.endswith("/204279858"):
        parser.error("--prime-url must name product 204279858 on www.homedepot.com")
    return asyncio.run(serve(args.prime_url) if args.server else verify(args))


if __name__ == "__main__":
    raise SystemExit(main())
