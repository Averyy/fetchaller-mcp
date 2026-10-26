"""Guarded live Brave verification through the actual local stdio MCP server.

    uv run --with-editable ../wafer python scripts/verify_brave_search.py \
        --wafer-path ../wafer --include-all

Stops at the first provider failure. Clean searches verify retrieval/pacing,
not challenge recovery. Recovery can use fresh PoW or retained spare tokens;
the solver's recovery flag alone does not distinguish them. No requests execute
when parser workers import this module.
"""

import argparse
import asyncio
import os
import re
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def verify(args) -> int:
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    paths = [str(root / "src")]
    expected_wafer = None
    if args.wafer_path:
        expected_wafer = Path(args.wafer_path).expanduser().resolve() / "wafer" / "__init__.py"
        if not expected_wafer.is_file():
            raise ValueError("--wafer-path must name the local wafer repository")
        paths.insert(0, str(expected_wafer.parent.parent))
    env["PYTHONPATH"] = os.pathsep.join([*paths, env.get("PYTHONPATH", "")])
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "fetchaller.main"], env=env, cwd=str(root),
    )
    pages = []
    recovered = False
    topics = ("", "TaskGroup", "timeouts", "cancellation", "streams", "synchronization", "subprocesses", "queues", "runners", "event loop")
    calls = []
    for round_index in range(args.rounds):
        query = " ".join((args.query, topics[round_index % len(topics)])).strip()
        first_page = (round_index // len(topics)) * 2 + 1
        calls.extend(("brave", page, query) for page in (first_page, first_page + 1))
        if args.include_all:
            calls.append(("all", first_page, query))
    successful = True
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostic:
        async with stdio_client(parameters, errlog=diagnostic) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=360)) as client:
                await client.initialize()
                diagnostic.flush()
                diagnostic.seek(0)
                startup = diagnostic.read()
                source = re.search(r"wafer_source=(.*?) wafer_sha256=", startup)
                if source is None or (expected_wafer and Path(source[1]).resolve() != expected_wafer):
                    raise RuntimeError("MCP server did not import the requested wafer source")
                print(f"MCP wafer source: {source[1]}", flush=True)
                for provider, page, query in calls:
                    started = time.monotonic()
                    response = await client.call_tool(
                        "search", {"query": query, "provider": provider, "page": page},
                    )
                    content = "\n".join(block.text for block in response.content if block.type == "text")
                    required = ("google", "ddg", "brave") if provider == "all" else ("brave",)
                    summary = content.splitlines()[0] if content else ""
                    counts = {name: re.search(rf"\b{name}: ([1-9]\d*)\b", summary) for name in required}
                    if response.isError or not all(counts.values()):
                        print(f"FAIL provider={provider} page={page}: {content}", flush=True)
                        successful = False
                        break
                    print(
                        f"PASS provider={provider} page={page} "
                        + " ".join(f"{name}={match[1]}" for name, match in counts.items())
                        + f" elapsed={time.monotonic() - started:.2f}s",
                        flush=True,
                    )
                    if provider == "brave":
                        if "[brave]" not in content:
                            raise AssertionError("Brave results lost their provider attribution")
                        pages.append(set(re.findall(r"^\s+(https?://\S+)$", content, re.MULTILINE)))
                    diagnostic.seek(0)
                    current_logs = diagnostic.read()
                    if "BRAVE_BROWSER_SOLVE response_status=200 challenge_absent=False" in current_logs:
                        recovered = True
                    if recovered and (not args.include_all or provider == "all"):
                        break
        diagnostic.seek(0)
        logs = diagnostic.read()
    if len(pages) == 2:
        print(f"Brave page URL overlap: {len(pages[0] & pages[1])}", flush=True)
    for line in logs.splitlines():
        if any(key in line for key in ("BRAVE_BROWSER_SOLVE", "BROWSER_DISPATCH_SUMMARY", "BROWSER_EGRESS_SUMMARY")):
            print(line, flush=True)
    if recovered and successful:
        print("Live recovery observed; later cookie replay/persistence needs separate verification.", flush=True)
    elif not successful and "BRAVE_BROWSER_SOLVE" in logs:
        print("Live challenge recovery FAILED: the browser did not return recovered search results.", flush=True)
    else:
        print("Challenge recovery UNVERIFIED: no live challenge recovery was observed.", flush=True)
    if not successful:
        return 1
    return 2 if args.require_recovery and not recovered else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wafer-path")
    parser.add_argument("--include-all", action="store_true")
    parser.add_argument("--query", default="Python asyncio documentation")
    parser.add_argument("--rounds", type=int, default=1, help="1-50 rounds of distinct, paced queries/pages")
    parser.add_argument("--require-recovery", action="store_true", help="Exit 2 if no live challenge recovery was observed")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 50:
        parser.error("--rounds must be between 1 and 50")
    return asyncio.run(verify(args))


if __name__ == "__main__":
    raise SystemExit(main())
