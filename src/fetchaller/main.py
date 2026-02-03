"""Main entry point for fetchaller-mcp."""

import argparse
import asyncio
import signal
import sys

from .config import load_config


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="fetchaller MCP server - fetch any URL with TLS fingerprint impersonation"
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run in HTTP mode (for remote deployment) instead of stdio mode",
    )
    args = parser.parse_args()

    config = load_config()

    if args.http:
        # HTTP mode for remote deployment
        asyncio.run(run_http_mode(config))
    else:
        # Stdio mode for local use
        asyncio.run(run_stdio_mode(config))


async def run_stdio_mode(config) -> None:
    """Run in stdio mode (local MCP server)."""
    from .server import run_stdio_server

    await run_stdio_server(config)


async def run_http_mode(config) -> None:
    """Run in HTTP mode (remote deployment)."""
    from .http.app import run_http_server

    # Set up graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(sig):
        print(f"\n[Received {sig.name}] Shutting down gracefully...", file=sys.stderr)
        shutdown_event.set()

    # Register signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    # Run server
    try:
        await run_http_server(config)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
