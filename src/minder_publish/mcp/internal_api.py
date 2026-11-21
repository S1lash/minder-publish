"""Reuse HTTP operation validation for in-process, audience-scoped MCP calls.

The private ASGI scope entry cannot be supplied by a network header. Credentials
are revalidated for every operation, including each phase of an upload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

INTERNAL_PRINCIPAL = "minder_publish.internal_mcp_principal"


class _RequestStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        for chunk in self.stream:
            yield chunk


class InternalApiTransport(httpx.BaseTransport):
    """A synchronous client adapter used by SDK synchronous tool worker threads."""

    def __init__(self, app, principal: Callable):
        self.app, self.principal = app, principal

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def dispatch():
            async def application(scope, receive, send):
                scope = {**scope, INTERNAL_PRINCIPAL: self.principal}
                await self.app(scope, receive, send)

            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=_RequestStream(request.stream),
                )
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=await response.aread(),
                    extensions=response.extensions,
                )

        # MCP's synchronous tools run in AnyIO workers. A running event loop here
        # signals an integration error; never block or create a nested loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(dispatch())
        raise RuntimeError("Use the internal API transport from a tool worker.")
