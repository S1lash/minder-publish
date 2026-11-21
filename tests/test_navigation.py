"""Native PRG tickets are one-use navigation references, never new authority."""

import asyncio
import json
import shutil
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import continuation_target
from test_http import publish

from minder_publish.server.auth import digest
from minder_publish.server.navigation import Navigation

pytest_plugins = ("test_http",)


async def opening(application, client, *, guest=False):
    material, _ = await publish(
        application, client, access="password", password="material-password"
    )
    url = material["stable_url"] + ("?guest=1" if guest else "")
    gate = await client.get(url)
    from test_guest_view import csrf_from

    response = await client.post(
        url,
        data={"csrf": csrf_from(gate), "password": "material-password"},
        headers={"Origin": application.state.config.control_origin},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert urlsplit(location).netloc == "publish.test"
    assert urlsplit(location).path == "/__mp/continue"
    return material, location, parse_qs(urlsplit(location).query)["ticket"][0]


@pytest.mark.parametrize("guest", [False, True])
async def test_prg_preserves_form_boundary_content_realm_and_one_use(
    application, client, guest
):
    material, location, ticket = await opening(application, client, guest=guest)
    client.cookies.set("mp_lang", "fr", domain="publish.test", path="/")
    client.cookies.set("mp_theme", "dark", domain="publish.test", path="/")
    page = await client.get(location, follow_redirects=False)
    assert 'lang="fr"' in page.text and 'data-theme="dark"' in page.text
    assert "Chargement…" in page.text and "Ouvrir" in page.text
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    assert "<noscript>" not in page.text and 'id="mp-continue"' in page.text
    assert '<meta http-equiv="refresh" content="2;url=' in page.text
    from minder_publish.server.navigation import PUBLICATION_NAVIGATION_SCRIPT

    assert "<script>" + PUBLICATION_NAVIGATION_SCRIPT + "</script>" in page.text
    target = continuation_target(page)
    config = application.state.config
    assert (
        urlsplit(target).netloc
        == urlsplit(
            config.content_origin(material["current_revision"], guest=guest)
        ).netloc
    )
    assert (await client.get(location)).status_code == 403
    content = await client.get(target)
    assert content.status_code == 200 and content.content == b"<h1>Private result</h1>"
    assert (await client.get(target)).status_code == 403


@pytest.mark.parametrize(
    "failure",
    [
        "tampered",
        "expired",
        "missing_cookie",
        "wrong_realm",
        "revoked_grant",
        "foreign_target",
        "foreign_audience",
        "wrong_purpose",
        "sealed_tamper",
        "reset",
    ],
)
async def test_navigation_rejects_invalid_ticket_or_authority(
    application, client, failure
):
    _, location, ticket = await opening(application, client)
    auth = application.state.auth
    if failure == "tampered":
        location = location.replace(
            ticket, ticket[:-1] + ("A" if ticket[-1] != "A" else "B")
        )
    elif failure == "missing_cookie":
        client.cookies.clear()
    elif failure == "reset":
        auth.invalidate_all()
    else:
        with auth.connection(write=True) as connection:
            if failure == "expired":
                connection.execute(
                    "UPDATE auth_intents SET expires_at=? WHERE token_hash=?",
                    (time.time() - 1, digest(ticket)),
                )
            elif failure == "revoked_grant":
                connection.execute("DELETE FROM auth_unlocks")
            else:
                row = connection.execute(
                    "SELECT payload FROM auth_intents WHERE token_hash=?",
                    (digest(ticket),),
                ).fetchone()
                payload = json.loads(row["payload"])
                if failure == "wrong_realm":
                    payload["realm"] = "probe"
                if failure == "foreign_target":
                    payload[
                        "target"
                    ] = 'https://attacker.example/"><script>alert(1)</script>'
                if failure == "foreign_audience":
                    payload["audience"] = "https://attacker.example"
                if failure == "wrong_purpose":
                    payload["purpose"] = "handoff"
                if failure == "sealed_tamper":
                    payload["sealed"] = (
                        payload["sealed"][:-5]
                        + ("A" if payload["sealed"][-5] != "A" else "B")
                        + payload["sealed"][-4:]
                    )
                connection.execute(
                    "UPDATE auth_intents SET payload=? WHERE token_hash=?",
                    (json.dumps(payload), digest(ticket)),
                )
    response = await client.get(location, follow_redirects=False)
    assert response.status_code == 403
    assert "attacker.example" not in response.text and "<script>" not in response.text


async def test_wrong_route_method_or_query_cannot_consume_navigation(
    application, client
):
    material, location, ticket = await opening(application, client)
    origin = application.state.config.content_origin(material["current_revision"])
    assert (
        await client.get(origin + "/__mp/continue?ticket=" + ticket)
    ).status_code == 404
    assert (
        await client.head(origin + "/__mp/continue?ticket=" + ticket)
    ).status_code == 404
    assert (
        await client.get("https://foreign.test/__mp/continue?ticket=" + ticket)
    ).status_code == 421
    assert (await client.head(location)).status_code == 405
    assert (
        await client.get(location + "&target=https://attacker.example")
    ).status_code == 403
    assert (await client.get(location + "&ticket=" + ticket)).status_code == 403
    assert (
        await client.get(origin + "/__mp/start?intent=" + ticket)
    ).status_code == 403
    continuation_target(await client.get(location, follow_redirects=False))


async def test_navigation_ticket_is_consumed_atomically(application, client):
    _, location, _ = await opening(application, client)
    responses = await asyncio.gather(
        *(client.get(location, follow_redirects=False) for _ in range(2))
    )
    assert sorted(response.status_code for response in responses) == [200, 403]


async def test_regular_handoff_is_not_a_navigation_ticket(application, client):
    _, _, ticket = await opening(application, client)
    with application.state.auth.connection() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM auth_intents WHERE token_hash=?", (digest(ticket),)
            ).fetchone()[0]
        )
    regular = application.state.navigation.open(payload)["details"]["intent"]
    assert "target" not in payload and "details" not in payload
    assert regular not in json.dumps(payload)
    assert (await client.get("/__mp/continue?ticket=" + regular)).status_code == 403


async def test_navigation_seal_is_bound_to_context_and_server_key(application, client):
    _, _, ticket = await opening(application, client)
    with application.state.auth.connection() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM auth_intents WHERE token_hash=?", (digest(ticket),)
            ).fetchone()[0]
        )
        dump = "\n".join(connection.iterdump())
    navigation = application.state.navigation
    opened = navigation.open(payload)
    assert opened["details"]["intent"] not in dump and opened["target"] not in dump
    for field, value in [
        ("kind", "oauth"),
        ("realm", "owner"),
        ("subject_id", "0" * 64),
    ]:
        assert navigation.open({**payload, field: value}) is None
    from dataclasses import replace

    other = Navigation(
        replace(application.state.config, secret="another-secret-" * 4),
        application.state.auth,
    )
    assert other.open(payload) is None
    assert navigation.open({**payload, "sealed": "A" * 43693}) is None


@pytest.mark.parametrize(
    "fragment",
    ["", "#section", "#/reports/2026?view=detail", '#"><script>alert(1)</script>'],
)
def test_publication_script_preserves_browser_fragment_without_sending_it_to_server(
    fragment,
):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for client-only fragment behavior")
    from minder_publish.server.navigation import PUBLICATION_NAVIGATION_SCRIPT

    destination = "https://g-revision.publish.test/__mp/start?intent=opaque"
    program = r"""const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const link={href:input.destination};let navigated='';
const location={hash:input.fragment,replace:value=>{navigated=value;}};
const document={getElementById:id=>{if(id!=='mp-continue')throw new Error('wrong anchor');return link;}};
vm.runInNewContext(input.script,{document,location,URL},{timeout:1000});
const expected=new URL(input.destination);expected.hash=input.fragment;
if(navigated!==expected.href||link.href!==expected.href)throw new Error('fragment lost or target changed');
const actual=new URL(navigated),original=new URL(input.destination);
if(actual.origin!==original.origin||actual.pathname!==original.pathname||actual.search!==original.search)throw new Error('server-visible destination changed');
process.stdout.write('preserved');"""
    result = subprocess.run(
        [node, "-e", program],
        input=json.dumps(
            {
                "destination": destination,
                "fragment": fragment,
                "script": PUBLICATION_NAVIGATION_SCRIPT,
            }
        ),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout == "preserved"
