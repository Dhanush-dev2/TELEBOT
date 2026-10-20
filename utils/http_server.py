"""Minimal keep-alive HTTP endpoint for Render's free web service tier."""

from __future__ import annotations

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_server(port: int) -> web.AppRunner:
    """Bind 0.0.0.0:PORT and answer GET / with 200 OK."""
    app = web.Application()
    app.router.add_get("/", _health)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(
        "Keep-alive HTTP server listening on port %s (host=0.0.0.0)",
        port,
    )
    return runner


async def stop_server(runner: web.AppRunner) -> None:
    await runner.cleanup()
    logger.info("Keep-alive HTTP server stopped")
