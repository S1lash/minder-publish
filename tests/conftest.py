"""Shared temporary HTTP fixtures; server dependencies load only when requested."""

import pytest


class BrowserClient:
    """Follow only the server's verified opening-page refresh when navigation is enabled."""

    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def request(self, method, url, **kwargs):
        response = await self.client.request(method, url, **kwargs)
        if method.upper() != "GET" or kwargs.get("follow_redirects") is False:
            return response
        from browser_navigation import opening_target
        target = opening_target(response)
        if target is not None:
            return await self.client.get(target, headers=kwargs.get("headers"), follow_redirects=True)
        return response

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def head(self, url, **kwargs):
        return await self.request("HEAD", url, **kwargs)


@pytest.fixture
async def application(tmp_path):
    from minder_publish.server.app import create_app
    from minder_publish.server.config import Config

    config = Config(
        data_dir=tmp_path,
        base_url="https://publish.test",
        secret="test-only-secret-" * 4,
        testing=True,
    )
    result = create_app(config)
    async with result.router.lifespan_context(result):
        result.state.auth.bootstrap("owner", "a long test passphrase")
        yield result


@pytest.fixture
async def client(application):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://publish.test",
        follow_redirects=True,
    ) as value:
        yield BrowserClient(value)
