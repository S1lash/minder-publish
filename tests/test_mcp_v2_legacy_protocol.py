"""Optional gate using a genuinely isolated MCP SDK 1.x client interpreter.

Set MINDER_PUBLISH_LEGACY_PYTHON to that interpreter; runtime dependencies remain
SDK 2.x. No owner-specific interpreter path is stored in the repository.
"""

import json
import os
import subprocess

import pytest
import test_client_live_api

live_api = test_client_live_api.live_api


@pytest.mark.skipif(
    not os.environ.get("MINDER_PUBLISH_LEGACY_PYTHON"),
    reason="Set MINDER_PUBLISH_LEGACY_PYTHON to an isolated MCP 1.x interpreter.",
)
@pytest.mark.parametrize("live_api", [True], indirect=True)
def test_sdk1_client_negotiates_legacy_protocol_and_publishes_real_bytes(live_api):
    config, manage, _ = live_api
    environment = os.environ.copy()
    environment["MP_TEST_URL"] = config.base_url + "/mcp/"
    environment["MP_TEST_TOKEN"] = manage
    script = """
import asyncio, importlib.metadata, json, os
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession
async def main():
    sdk = importlib.metadata.version('mcp')
    assert sdk.startswith('1.'), 'This gate must run a real MCP 1.x client.'
    async with streamablehttp_client(os.environ['MP_TEST_URL'], headers={
        'Authorization': 'Bearer ' + os.environ['MP_TEST_TOKEN']}) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert 'publish_content' in names and 'publish_file' not in names
            published = await session.call_tool('publish_content', {
                'content': 'legacy SDK real bytes', 'filename': 'legacy.txt',
                'summary': 'SDK1 compatibility gate'})
            assert not published.isError, published
            print(json.dumps({'sdk': sdk, 'protocol': initialized.protocolVersion, 'published': True}))
asyncio.run(main())
"""
    result = subprocess.run(
        [environment["MINDER_PUBLISH_LEGACY_PYTHON"], "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.replace(manage, "[test-token]")[-2000:]
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["sdk"].startswith("1.") and summary["protocol"] == "2025-11-25"
    assert summary["published"] is True
    import httpx

    response = httpx.get(
        config.base_url + "/api/v1/publications",
        params={"q": "SDK1 compatibility gate"},
        headers={"Authorization": f"Bearer {manage}"},
    )
    assert response.status_code == 200 and response.json()["data"]["total"] == 1
