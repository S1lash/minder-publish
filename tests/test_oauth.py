"""OAuth boundaries exercised against real SQLite and maintained SDK handlers."""

import base64
import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest
from browser_navigation import navigation_target

pytest.importorskip("mcp")

from starlette.applications import Starlette

from minder_publish.errors import PublishError
from minder_publish.server.auth import AuthStore, Principal
from minder_publish.server.config import Config
from minder_publish.server.core import Core
from minder_publish.server.oauth.routes import OAuthService
from minder_publish.server.oauth.store import (
    OAuthFailure,
    OAuthOptions,
    OAuthStore,
    digest,
)
from minder_publish.server.upload_jobs import UploadJobs

BASE = "https://publish.test"
CALLBACK = "https://client.example/callback"
PASSWORD = "test-only long owner passphrase"
VERIFIER = "a" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)


@pytest.fixture
def oauth(tmp_path):
    config = Config(
        tmp_path, base_url=BASE, secret="test-only-secret-" * 4, testing=True
    )
    core = Core(config).initialize()
    auth = AuthStore(config)
    auth.initialize()
    auth.bootstrap("owner", PASSWORD)
    raw, _ = auth.login("owner", PASSWORD, "local-test")
    service = OAuthService(config, auth).initialize()
    app = Starlette()
    service.install(app)
    yield service, app, raw
    core.close()


@pytest.fixture
async def browser(oauth):
    service, app, raw = oauth
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False
    ) as client:
        client.cookies.set(
            service.config.owner_cookie, raw, domain="publish.test", path="/"
        )
        yield client


async def register(browser, **changes):
    metadata = {
        "client_name": "A publishing client",
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "materials:read materials:manage",
        **changes,
    }
    response = await browser.post("/oauth/register", json=metadata)
    assert response.status_code == 201, response.text
    return response.json()


async def pending(browser, client, **changes):
    params = {
        "client_id": client["client_id"],
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": BASE + "/mcp",
        "scope": "materials:manage",
        "state": "callback-state",
        **changes,
    }
    response = await browser.get("/oauth/authorize", params=params)
    assert response.status_code == 302, response.text
    assert urlsplit(response.headers["location"]).path == "/oauth/consent"
    return parse_qs(urlsplit(response.headers["location"]).query)["request"][0]


async def code(browser, oauth, client, **changes):
    service, _, raw = oauth
    request_id = await pending(browser, client, **changes)
    page = await browser.get("/oauth/consent", params={"request": request_id})
    assert page.status_code == 200, page.text
    response = await browser.post(
        "/oauth/consent",
        data={
            "request": request_id,
            "csrf": service.auth.csrf(raw),
            "decision": "approve",
        },
        headers={"Origin": BASE},
    )
    assert response.status_code == 303, response.text
    target = await navigation_target(browser, response)
    values = parse_qs(urlsplit(target).query)
    assert values["state"] == ["callback-state"] and values["iss"] == [BASE]
    return values["code"][0]


def exchange(client, authorization_code, **changes):
    return {
        "grant_type": "authorization_code",
        "code": authorization_code,
        "client_id": client["client_id"],
        "code_verifier": VERIFIER,
        "redirect_uri": CALLBACK,
        "resource": BASE + "/mcp",
        **changes,
    }


async def tokens(browser, oauth, **client_changes):
    client = await register(browser, **client_changes)
    authorization_code = await code(browser, oauth, client)
    data = exchange(client, authorization_code)
    if "client_secret" in client:
        data["client_secret"] = client["client_secret"]
    result = await browser.post("/oauth/token", data=data)
    assert result.status_code == 200, result.text
    return client, result.json()


async def test_metadata_consent_and_resource_bound_hashed_tokens(oauth, browser):
    service, _, _ = oauth
    metadata = (await browser.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == BASE
    assert metadata["token_endpoint"] == BASE + "/oauth/token"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    resource = await browser.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.json()["resource"] == BASE + "/mcp"
    client, issued = await tokens(browser, oauth)
    principal = service.store.principal(issued["access_token"], BASE + "/mcp")
    assert principal["scope"] == "materials:manage"
    assert service.store.principal(issued["access_token"], BASE + "/api/v1") is None
    assert service.store.principal(issued["refresh_token"], BASE + "/mcp") is None
    verified = await service.verify_token(issued["access_token"])
    assert (
        verified.client_id == client["client_id"] and verified.resource == BASE + "/mcp"
    )
    with service.store.repository.connect() as connection:
        dump = "\n".join(connection.iterdump())
    assert issued["access_token"] not in dump and issued["refresh_token"] not in dump


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"resource": BASE + "/other"}, "invalid_target"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"redirect_uri": CALLBACK + "/other"}, "invalid_request"),
        ({"redirect_uri": "https://attacker.example/callback"}, "invalid_request"),
    ],
)
async def test_authorize_rejects_invalid_resource_pkce_redirect(
    browser, changes, error
):
    client = await register(browser)
    response = await browser.get(
        "/oauth/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": BASE + "/mcp",
            **changes,
        },
    )
    assert response.status_code == 400 and response.json()["error"] == error
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "change",
    [
        {"code_verifier": "b" * 64},
        {"resource": BASE + "/other"},
        {"redirect_uri": CALLBACK + "/other"},
    ],
)
async def test_wrong_verifier_audience_or_redirect_cannot_exchange(
    oauth, browser, change
):
    client = await register(browser)
    authorization_code = await code(browser, oauth, client)
    response = await browser.post(
        "/oauth/token", data=exchange(client, authorization_code, **change)
    )
    assert response.status_code == 400
    assert (
        await browser.post("/oauth/token", data=exchange(client, authorization_code))
    ).status_code == 200


async def test_authorization_code_is_one_use_and_replay_revokes_family(oauth, browser):
    service, _, _ = oauth
    client = await register(browser)
    authorization_code = await code(browser, oauth, client)
    response = await browser.post(
        "/oauth/token", data=exchange(client, authorization_code)
    )
    issued = response.json()
    replay = await browser.post(
        "/oauth/token", data=exchange(client, authorization_code)
    )
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert service.store.principal(issued["access_token"], BASE + "/mcp") is None


async def test_refresh_rotation_reuse_and_scope_downscoping(oauth, browser):
    service, _, _ = oauth
    client, issued = await tokens(browser, oauth)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": issued["refresh_token"],
        "client_id": client["client_id"],
        "resource": BASE + "/mcp",
    }
    escalation = await browser.post("/oauth/token", data={**form, "scope": "owner"})
    assert escalation.status_code == 400
    rotated = await browser.post("/oauth/token", data=form)
    assert rotated.status_code == 200
    fresh = rotated.json()
    assert fresh["refresh_token"] != issued["refresh_token"]
    assert service.store.principal(issued["access_token"], BASE + "/mcp") is None
    assert service.store.principal(fresh["access_token"], BASE + "/mcp")
    service.store.cleanup()
    replay = await browser.post("/oauth/token", data=form)
    assert replay.status_code == 400
    assert service.store.principal(fresh["access_token"], BASE + "/mcp") is None


async def test_refresh_downscope_closes_pending_manage_job_authority(oauth, browser):
    service, _, owner_raw = oauth
    registered, issued = await tokens(browser, oauth)
    granted = service.store.principal(issued["access_token"], service.store.resource)
    creator = Principal("oauth", granted["id"], granted["scope"])
    owner = service.auth.session(owner_raw)
    core = Core(service.config).initialize()
    try:
        jobs = UploadJobs(core).initialize()
        pending_job = jobs.create({"kind": "file"}, creator)
        prepared_job = jobs.create({"kind": "file"}, creator)
        upload = jobs.start(
            prepared_job["id"], owner, {"filename": "reviewed.txt", "size_bytes": 8}
        )

        async def body():
            yield b"reviewed"

        await core.receive_upload(upload["id"], body(), owner.subject)
        core.inspect_upload(upload["id"], owner.subject)
        response = await browser.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": registered["client_id"],
                "resource": service.store.resource,
                "scope": "materials:read",
            },
        )
        assert response.status_code == 200, response.text
        narrowed = response.json()
        assert (
            service.store.principal(issued["access_token"], service.store.resource)
            is None
        )
        assert (
            service.store.principal(narrowed["access_token"], service.store.resource)[
                "scope"
            ]
            == "materials:read"
        )
        # No cleanup/precheck before commit: the transactional creator guard itself
        # must observe the narrowed family even with the original captured principal.
        with pytest.raises(PublishError) as refused:
            core.commit_upload(upload["id"], owner.subject, authorize=jobs.guard_commit)
        assert refused.value.code == "upload_job_creator_revoked"
        with pytest.raises(PublishError):
            jobs.start(
                pending_job["id"],
                owner,
                {"filename": "not-claimed.txt", "size_bytes": 1},
            )
        assert core.list_publications()["total"] == 0
        assert service.store.connections()[0]["scopes"] == ["materials:read"]
        with pytest.raises(OAuthFailure) as escalation:
            service.store.refresh(
                narrowed["refresh_token"], registered["client_id"], ["materials:manage"]
            )
        assert escalation.value.code == "invalid_scope"
    finally:
        core.close()


async def test_owner_consent_requires_session_csrf_recent_auth_and_is_one_use(
    oauth, browser
):
    service, _, raw = oauth
    client = await register(browser)
    request_id = await pending(browser, client)
    browser.cookies.clear()
    response = await browser.get("/oauth/consent", params={"request": request_id})
    assert (
        response.status_code == 303
        and urlsplit(response.headers["location"]).path == "/login"
    )
    browser.cookies.set(
        service.config.owner_cookie, raw, domain="publish.test", path="/"
    )
    assert (
        await browser.get("/oauth/consent", params={"request": request_id})
    ).status_code == 200
    form = {"request": request_id, "csrf": "invalid", "decision": "approve"}
    assert (
        await browser.post("/oauth/consent", data=form, headers={"Origin": BASE})
    ).status_code == 403
    form["csrf"] = service.auth.csrf(raw)
    assert (
        await browser.post(
            "/oauth/consent", data=form, headers={"Origin": "https://attacker.example"}
        )
    ).status_code == 403
    assert (
        await browser.post("/oauth/consent", data=form, headers={"Origin": BASE})
    ).status_code == 303
    assert (
        await browser.post("/oauth/consent", data=form, headers={"Origin": BASE})
    ).status_code == 400


async def test_confidential_client_secrets_are_hashed_and_revocation_is_client_bound(
    oauth, browser
):
    service, _, _ = oauth
    client, issued = await tokens(
        browser, oauth, token_endpoint_auth_method="client_secret_post"
    )
    row = service.store.client(client["client_id"])
    assert "client_secret" not in row["metadata"]
    assert row["secret_hash"] == digest(client["client_secret"])
    stranger = await register(browser)
    result = await browser.post(
        "/oauth/revoke",
        data={"client_id": stranger["client_id"], "token": issued["access_token"]},
    )
    assert result.status_code == 200, result.text
    assert service.store.principal(issued["access_token"], BASE + "/mcp")
    result = await browser.post(
        "/oauth/revoke",
        data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "token": issued["refresh_token"],
        },
    )
    assert (
        result.status_code == 200
        and service.store.principal(issued["access_token"], BASE + "/mcp") is None
    )


@pytest.mark.parametrize(
    "uri",
    [
        "http://client.example/callback",
        CALLBACK + "#fragment",
        "https://user:password@client.example/callback",
        CALLBACK + "?state=ambiguous",
    ],
)
async def test_dcr_rejects_unsafe_callbacks_without_fetching(browser, uri):
    response = await browser.post(
        "/oauth/register",
        json={"redirect_uris": [uri], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 400


async def test_duplicate_parameters_body_bounds_and_content_host_routes(oauth, browser):
    service, _, _ = oauth
    response = await browser.post(
        "/oauth/register",
        content='{"redirect_uris":[],"redirect_uris":[]}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    response = await browser.post(
        "/oauth/token",
        content="client_id=one&client_id=two",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    response = await browser.post(
        "/oauth/register", content=b"x" * (service.store.options.maximum_body_bytes + 1)
    )
    assert response.status_code == 413
    for path in (
        "/.well-known/oauth-authorization-server",
        "/oauth/authorize",
        "/oauth/consent",
    ):
        assert (
            await browser.get("https://r-" + "a" * 32 + ".publish.test" + path)
        ).status_code == 404
    response = await browser.options(
        "https://r-" + "a" * 32 + ".publish.test/oauth/token",
        headers={"Origin": CALLBACK, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 404


async def test_oauth_routes_preserve_authorized_revision_files(application, client):
    from test_http import publish, sign_in

    OAuthService(application.state.config, application.state.auth).initialize().install(
        application
    )
    paths = (
        ".well-known/oauth-authorization-server",
        ".well-known/oauth-protected-resource",
        ".well-known/oauth-protected-resource/mcp",
        "oauth/authorize",
        "oauth/consent",
        "oauth/register",
        "oauth/token",
        "oauth/revoke",
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as site:
        site.writestr("index.html", "<h1>Private static site</h1>")
        for path in paths:
            stored = (
                path + "/index.html"
                if path == ".well-known/oauth-protected-resource"
                else path
            )
            site.writestr(stored, "Author bytes for " + path)
    material, _ = await publish(
        application,
        client,
        kind="site",
        filename="site.zip",
        body=archive.getvalue(),
    )
    await sign_in(client)
    opening = await client.get(material["stable_url"])
    assert opening.status_code == 200
    origin = application.state.config.content_origin(material["current_revision"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), follow_redirects=False
    ) as anonymous:
        for path in paths:
            url = origin + "/" + path
            response = await client.get(url)
            assert response.status_code == 200, (path, response.text)
            assert response.text == "Author bytes for " + path
            head = await client.head(url)
            assert head.status_code == 200 and head.content == b""
            assert int(head.headers["content-length"]) == len(response.content)
            assert (await anonymous.get(url)).status_code == 403
            for method in ("POST", "OPTIONS"):
                denied = await client.request(
                    method,
                    url,
                    headers={
                        "Origin": CALLBACK,
                        "Access-Control-Request-Method": "POST",
                    },
                )
                assert denied.status_code == 404
                assert "access-control-allow-origin" not in denied.headers
    application.state.config.legacy_hosts = ("legacy.test",)
    for host in ("legacy.test", "untrusted.test"):
        response = await client.get("https://" + host + "/oauth/authorize")
        assert response.status_code == (404 if host == "legacy.test" else 421)


async def test_code_exchange_cas_and_reset_revocation_are_durable(oauth, browser):
    service, _, _ = oauth
    client = await register(browser)
    authorization_code = await code(browser, oauth, client)

    def consume():
        try:
            return service.store.exchange_code(authorization_code, client["client_id"])
        except OAuthFailure:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: consume(), range(2)))
    assert sum(result is not None for result in outcomes) == 1
    _, fresh = await tokens(browser, oauth)
    operator = AuthStore(service.config)
    operator.initialize()
    operator.bootstrap("owner", "replacement owner password", reset=True)
    assert service.store.principal(fresh["access_token"], BASE + "/mcp") is None
    with service.store.repository.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM oauth_pending").fetchone()[0] == 0
        )
        assert connection.execute("SELECT count(*) FROM oauth_code").fetchone()[0] == 0


async def test_manage_scope_can_refresh_down_to_read_but_never_regain_manage(
    oauth, browser
):
    service, _, _ = oauth
    client, issued = await tokens(browser, oauth)
    response = await browser.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "client_id": client["client_id"],
            "resource": BASE + "/mcp",
            "scope": "materials:read",
        },
    )
    assert response.status_code == 200, response.text
    narrowed = response.json()
    assert (
        service.store.principal(narrowed["access_token"], BASE + "/mcp")["scope"]
        == "materials:read"
    )
    response = await browser.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": narrowed["refresh_token"],
            "client_id": client["client_id"],
            "resource": BASE + "/mcp",
            "scope": "materials:manage",
        },
    )
    assert response.status_code == 400 and response.json()["error"] == "invalid_scope"


async def test_confidential_basic_auth_works_without_client_id_in_form(oauth, browser):
    service, _, _ = oauth
    client = await register(browser, token_endpoint_auth_method="client_secret_basic")
    authorization_code = await code(browser, oauth, client)
    data = exchange(client, authorization_code)
    del data["client_id"]
    response = await browser.post(
        "/oauth/token", data=data, auth=(client["client_id"], client["client_secret"])
    )
    assert response.status_code == 200, response.text
    issued = response.json()
    response = await browser.post(
        "/oauth/revoke",
        data={"token": issued["access_token"]},
        auth=(client["client_id"], client["client_secret"]),
    )
    assert response.status_code == 200, response.text
    assert service.store.principal(issued["access_token"], BASE + "/mcp") is None


async def test_old_consent_requires_reauth_and_wrong_password_is_retryable(
    oauth, browser
):
    service, _, raw = oauth
    client = await register(browser)
    request_id = await pending(browser, client)
    with service.auth.connection(write=True) as connection:
        connection.execute(
            "UPDATE auth_sessions SET authenticated_at=authenticated_at-?",
            (service.config.operations["owner_recent_auth_seconds"] + 1,),
        )
    page = await browser.get("/oauth/consent", params={"request": request_id})
    assert 'name="password"' in page.text
    assert page.headers.get_list("referrer-policy") == ["same-origin"]
    form = {
        "request": request_id,
        "csrf": service.auth.csrf(raw),
        "decision": "approve",
    }
    assert (
        await browser.post("/oauth/consent", data=form, headers={"Origin": "null"})
    ).status_code == 403
    response = await browser.post("/oauth/consent", data=form, headers={"Origin": BASE})
    assert response.status_code == 403
    response = await browser.post(
        "/oauth/consent",
        data={**form, "password": "incorrect"},
        headers={"Origin": BASE},
    )
    assert response.status_code == 401 and response.headers["content-type"].startswith(
        "text/html"
    )
    assert response.headers.get_list("referrer-policy") == ["same-origin"]
    assert (
        'name="password"' in response.text and 'value="incorrect"' not in response.text
    )
    response = await browser.post(
        "/oauth/consent", data={**form, "password": PASSWORD}, headers={"Origin": BASE}
    )
    assert response.status_code == 303


async def test_deny_needs_active_owner_csrf_but_does_not_require_fresh_password(
    oauth, browser
):
    service, _, raw = oauth
    client = await register(browser)
    request_id = await pending(browser, client)
    await browser.get("/oauth/consent", params={"request": request_id})
    with service.auth.connection(write=True) as connection:
        connection.execute("UPDATE auth_sessions SET authenticated_at=0")
    response = await browser.post(
        "/oauth/consent",
        data={
            "request": request_id,
            "csrf": service.auth.csrf(raw),
            "decision": "deny",
        },
        headers={"Origin": BASE},
    )
    assert response.status_code == 303
    target = await navigation_target(browser, response)
    assert parse_qs(urlsplit(target).query)["error"] == ["access_denied"]
    assert not service.store.connections()


async def test_expired_tokens_codes_pending_and_unknown_schema_fail_closed(
    oauth, browser
):
    service, _, raw = oauth
    client, issued = await tokens(browser, oauth)
    instant = int(service.store.clock())
    service.store.clock = lambda: instant + service.store.options.access_seconds + 1
    assert service.store.principal(issued["access_token"], BASE + "/mcp") is None
    assert service.store.token(issued["refresh_token"], kind="refresh")
    service.store.clock = lambda: instant + service.store.options.refresh_seconds + 1
    assert service.store.token(issued["refresh_token"], kind="refresh") is None
    with service.store.repository.connect(immediate=True) as connection:
        connection.execute("UPDATE oauth_schema_version SET version=99")
    with pytest.raises(PublishError) as error:
        OAuthStore(service.config).initialize()
    assert error.value.code == "unknown_oauth_schema"


async def test_revoking_one_application_does_not_revoke_another_or_owner(
    oauth, browser
):
    service, _, raw = oauth
    first, a = await tokens(browser, oauth)
    second, b = await tokens(browser, oauth)
    connection_id = service.store.principal(a["access_token"], BASE + "/mcp")["id"]
    service.store.revoke_connection(connection_id)
    assert service.store.principal(a["access_token"], BASE + "/mcp") is None
    assert service.store.principal(b["access_token"], BASE + "/mcp")
    assert service.auth.session(raw)
    assert len(service.store.connections()) == 1
    assert service.store.connections()[0]["client_id"] == second["client_id"]


def test_consent_atomic_owner_guard_cannot_create_code_after_logout(oauth):
    service, _, raw = oauth
    request_id = service.store.pending(
        "client",
        {
            "redirect_uri": CALLBACK,
            "scopes": ["materials:read"],
            "resource": BASE + "/mcp",
        },
    )
    service.store.pending_details(request_id, digest(raw))
    service.auth.logout(raw)
    with pytest.raises(PublishError):
        service.store.consent(
            request_id,
            digest(raw),
            True,
            lambda connection: service.auth.require_recent_auth(
                raw, connection=connection
            ),
        )
    with service.store.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM oauth_code").fetchone()[0] == 0


async def test_registration_caps_and_pending_caps_are_enforced(oauth, browser):
    service, _, _ = oauth
    service.store.options = replace(
        service.store.options, maximum_clients=1, maximum_pending=1
    )
    client = await register(browser)
    await pending(browser, client)
    response = await browser.post(
        "/oauth/register",
        json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 429
    response = await browser.get(
        "/oauth/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": BASE + "/mcp",
        },
    )
    assert response.status_code in (302, 429)


async def test_expired_pending_and_code_cannot_be_redeemed(oauth, browser):
    service, _, raw = oauth
    client = await register(browser)
    request_id = await pending(browser, client)
    instant = int(service.store.clock())
    service.store.clock = lambda: instant + service.store.options.pending_seconds + 1
    assert (
        await browser.get("/oauth/consent", params={"request": request_id})
    ).status_code == 400
    service.store.clock = lambda: instant
    authorization_code = await code(browser, oauth, client)
    service.store.clock = lambda: instant + service.store.options.code_seconds + 1
    response = await browser.post(
        "/oauth/token", data=exchange(client, authorization_code)
    )
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


async def test_token_budget_refuses_issuance_without_consuming_authorization_code(
    oauth, browser
):
    service, _, _ = oauth
    client = await register(browser)
    authorization_code = await code(browser, oauth, client)
    service.store.options = replace(service.store.options, maximum_tokens=1)
    response = await browser.post(
        "/oauth/token", data=exchange(client, authorization_code)
    )
    assert (
        response.status_code == 429
        and response.json()["error"] == "temporarily_unavailable"
    )
    service.store.options = replace(service.store.options, maximum_tokens=2)
    assert (
        await browser.post("/oauth/token", data=exchange(client, authorization_code))
    ).status_code == 200


async def test_simultaneous_refresh_consumption_revokes_the_winning_refresh_family(
    oauth, browser
):
    service, _, _ = oauth
    client, issued = await tokens(browser, oauth)

    def consume():
        try:
            return service.store.refresh(
                issued["refresh_token"], client["client_id"], ["materials:read"]
            )
        except OAuthFailure:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: consume(), range(2)))
    assert sum(result is not None for result in outcomes) == 1
    for result in outcomes:
        if result:
            assert (
                service.store.principal(result["access_token"], BASE + "/mcp") is None
            )


async def test_consent_session_binding_and_escaped_client_metadata(oauth, browser):
    service, _, raw = oauth
    client = await register(browser, client_name='<script>alert("untrusted")</script>')
    request_id = await pending(browser, client)
    response = await browser.get("/oauth/consent", params={"request": request_id})
    assert response.status_code == 200
    assert '<script>alert("untrusted")</script>' not in response.text
    assert "&lt;script&gt;" in response.text
    other, _ = service.auth.login("owner", PASSWORD, "different-browser")
    browser.cookies.set(
        service.config.owner_cookie, other, domain="publish.test", path="/"
    )
    assert (
        await browser.get("/oauth/consent", params={"request": request_id})
    ).status_code == 400
    browser.cookies.set(
        service.config.owner_cookie, raw, domain="publish.test", path="/"
    )
    assert (
        await browser.get("/oauth/consent", params={"request": request_id})
    ).status_code == 200


async def test_sdk_error_redirect_preserves_issuer_and_never_issues_unknown_scope(
    browser,
):
    client = await register(browser)
    response = await browser.get(
        "/oauth/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": BASE + "/mcp",
            "scope": "owner",
        },
    )
    assert response.status_code == 302
    values = parse_qs(urlsplit(response.headers["location"]).query)
    assert values["error"] == ["invalid_scope"] and values["iss"] == [BASE]


async def test_unauthorized_client_cannot_redeem_another_clients_code_or_refresh(
    oauth, browser
):
    client = await register(browser)
    authorization_code = await code(browser, oauth, client)
    stranger = await register(browser)
    response = await browser.post(
        "/oauth/token", data=exchange(stranger, authorization_code)
    )
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"
    response = await browser.post(
        "/oauth/token", data=exchange(client, authorization_code)
    )
    issued = response.json()
    response = await browser.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "client_id": stranger["client_id"],
            "resource": BASE + "/mcp",
        },
    )
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


def test_expired_unapproved_client_registrations_are_reclaimed(oauth):
    service, _, _ = oauth
    service.store.options = OAuthOptions(maximum_clients=1, registration_seconds=1)
    service.store.register("unused", {"client_name": "unused"})
    instant = int(service.store.clock())
    service.store.clock = lambda: instant + 2
    service.store.register("fresh", {"client_name": "fresh"})
    assert service.store.client("unused") is None
    assert service.store.client("fresh")


async def test_persisted_grants_for_another_resource_never_cross_instance_audience(
    oauth, browser
):
    service, _, _ = oauth
    client, issued = await tokens(browser, oauth)
    with service.store.repository.connect(immediate=True) as connection:
        connection.execute("UPDATE oauth_family SET resource=?", (BASE + "/other",))
    assert await service.verify_token(issued["access_token"]) is None
    response = await browser.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "client_id": client["client_id"],
            "resource": BASE + "/mcp",
        },
    )
    assert response.status_code == 400
    authorization_code = await code(browser, oauth, client)
    with service.store.repository.connect(immediate=True) as connection:
        row = connection.execute(
            "SELECT payload FROM oauth_code WHERE id=?", (digest(authorization_code),)
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["resource"] = BASE + "/other"
        connection.execute(
            "UPDATE oauth_code SET payload=? WHERE id=?",
            (json.dumps(payload), digest(authorization_code)),
        )
    response = await browser.post(
        "/oauth/token", data=exchange(client, authorization_code)
    )
    assert response.status_code == 400


async def test_owner_app_revocation_invalidates_codes_authorized_before_revoke(
    oauth, browser
):
    service, _, _ = oauth
    client, issued = await tokens(browser, oauth)
    connection_id = service.store.principal(issued["access_token"], BASE + "/mcp")["id"]
    authorization_code = await code(browser, oauth, client)
    pending_id = await pending(browser, client)
    service.store.revoke_connection(connection_id)
    response = await browser.post(
        "/oauth/token", data=exchange(client, authorization_code)
    )
    assert response.status_code == 400
    assert (
        await browser.get("/oauth/consent", params={"request": pending_id})
    ).status_code == 400
    assert not service.store.connections()


def test_operator_reset_refuses_unknown_oauth_schema_without_partial_reset(oauth):
    service, _, raw = oauth
    with service.store.repository.connect(immediate=True) as connection:
        connection.execute("UPDATE oauth_schema_version SET version=99")
    operator = AuthStore(service.config)
    operator.initialize()
    with pytest.raises(PublishError) as error:
        operator.bootstrap("owner", "replacement owner password", reset=True)
    assert error.value.code == "unknown_oauth_schema"
    assert service.auth.session(raw)


async def test_authorization_responses_preserve_registered_callback_query(
    oauth, browser
):
    service, _, raw = oauth
    callback = CALLBACK + "?channel=&channel=second"
    client = await register(browser, redirect_uris=[callback])
    request_id = await pending(browser, client, redirect_uri=callback)
    await browser.get("/oauth/consent", params={"request": request_id})
    response = await browser.post(
        "/oauth/consent",
        data={
            "request": request_id,
            "csrf": service.auth.csrf(raw),
            "decision": "approve",
        },
        headers={"Origin": BASE},
    )
    target = await navigation_target(browser, response)
    assert target.startswith(callback + "&")
    assert parse_qsl(urlsplit(target).query, keep_blank_values=True)[:2] == [
        ("channel", ""),
        ("channel", "second"),
    ]
    response = await browser.get(
        "/oauth/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": callback,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": BASE + "/mcp",
            "scope": "owner",
        },
    )
    assert response.headers["location"].startswith(callback + "&")


@pytest.mark.parametrize(
    "failure", [None, "logout", "session_expiry", "used_code", "tampered_target"]
)
async def test_oauth_prg_keeps_codes_sealed_and_revalidates_live_authority(
    oauth, browser, failure
):
    service, _, raw = oauth
    callback = CALLBACK + "?channel=&channel=second"
    client = await register(browser, redirect_uris=[callback])
    state = '"><script>alert("state")</script>'
    request_id = await pending(browser, client, redirect_uri=callback, state=state)
    await browser.get("/oauth/consent", params={"request": request_id})
    response = await browser.post(
        "/oauth/consent",
        data={
            "request": request_id,
            "csrf": service.auth.csrf(raw),
            "decision": "approve",
        },
        headers={"Origin": BASE},
    )
    assert response.status_code == 303
    location = response.headers["location"]
    ticket = parse_qs(urlsplit(location).query)["ticket"][0]
    with service.auth.connection() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM auth_intents WHERE token_hash=?", (digest(ticket),)
            ).fetchone()[0]
        )
        dump = "\n".join(connection.iterdump())
    opened = service.navigation.open(payload)
    code = opened["details"]["params"]["code"]
    assert "sealed" in payload and "target" not in payload and "details" not in payload
    assert code not in dump and opened["target"] not in dump and raw not in dump
    if failure == "logout":
        service.auth.logout(raw)
    elif failure in ("session_expiry", "used_code", "tampered_target"):
        with service.auth.connection(write=True) as connection:
            if failure == "session_expiry":
                connection.execute("UPDATE auth_sessions SET expires_at=0")
            elif failure == "used_code":
                connection.execute("UPDATE oauth_code SET used_at=1")
            else:
                altered = service.navigation.seal(
                    {**opened, "target": "https://attacker.example/callback"}
                )
                connection.execute(
                    "UPDATE auth_intents SET payload=? WHERE token_hash=?",
                    (json.dumps(altered), digest(ticket)),
                )
    page = await browser.get(location)
    if failure:
        assert page.status_code == 403 and code not in page.text
        return
    from browser_navigation import continuation_target

    target = continuation_target(page)
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "<script>" not in page.text
    assert parse_qs(urlsplit(target).query)["state"] == [state]
    assert parse_qsl(urlsplit(target).query, keep_blank_values=True)[:2] == [
        ("channel", ""),
        ("channel", "second"),
    ]
    assert (await browser.get(location)).status_code == 403


async def test_consent_uses_packaged_brand_preferences_and_bound_login(oauth, browser):
    service, _, raw = oauth
    client = await register(browser, client_name='<script>client</script>')
    request_id = await pending(browser, client)
    with service.auth.connection(write=True) as connection:
        connection.execute("UPDATE auth_sessions SET authenticated_at=0")
    browser.cookies.set("mp_lang", "fr", domain="publish.test", path="/")
    browser.cookies.set("mp_theme", "system", domain="publish.test", path="/")
    page = await browser.get("/oauth/consent", params={"request": request_id, "username": "attacker"})
    assert page.status_code == 200
    from html.parser import HTMLParser

    class FormParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inputs = {}
            self.labels = set()

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if tag == "input":
                self.inputs[attributes.get("name")] = attributes
            if tag == "label":
                self.labels.add(attributes.get("for"))

    parser = FormParser()
    parser.feed(page.text)
    username, password = parser.inputs["username"], parser.inputs["password"]
    assert username["value"] == "owner" and username["autocomplete"] == "username"
    assert username["type"] == "text" and "readonly" in username
    assert password["autocomplete"] == "current-password" and password["type"] == "password"
    assert username["id"] in parser.labels and password["id"] in parser.labels
    assert 'method="post" action="/oauth/consent"' in page.text
    assert 'data-theme="system"' in page.text and 'lang="fr"' in page.text
    for asset in ("ui.css", "oauth.css", "preferences.js", "oauth.js", "mark.svg"):
        assert f'/static/{asset}' in page.text
    assert 'data-oauth-preference="mp_theme"' in page.text
    assert 'data-oauth-preference="mp_lang"' in page.text
    assert '<script>client</script>' not in page.text
    assert "style-src 'self'" in page.headers["content-security-policy"]
    assert "'unsafe-inline'" not in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "same-origin"


async def test_deny_ignores_an_autofilled_wrong_password(oauth, browser):
    service, _, raw = oauth
    client = await register(browser)
    request_id = await pending(browser, client)
    await browser.get("/oauth/consent", params={"request": request_id})
    with service.auth.connection(write=True) as connection:
        connection.execute("UPDATE auth_sessions SET authenticated_at=0")
    response = await browser.post(
        "/oauth/consent",
        data={"request": request_id, "csrf": service.auth.csrf(raw), "decision": "deny", "password": "incorrect"},
        headers={"Origin": BASE},
    )
    assert response.status_code == 303
    target = await navigation_target(browser, response)
    assert parse_qs(urlsplit(target).query)["error"] == ["access_denied"]


async def test_oauth_continuation_has_local_styled_fallback_without_referrers(oauth, browser):
    service, _, raw = oauth
    callback = CALLBACK + "?source=registered"
    client = await register(browser, redirect_uris=[callback])
    request_id = await pending(browser, client, redirect_uri=callback)
    await browser.get("/oauth/consent", params={"request": request_id})
    response = await browser.post(
        "/oauth/consent",
        data={"request": request_id, "csrf": service.auth.csrf(raw), "decision": "approve"},
        headers={"Origin": BASE},
    )
    assert response.status_code == 303 and "code=" not in response.headers["location"]
    page = await browser.get(response.headers["location"])
    from browser_navigation import continuation_target

    target = continuation_target(page)
    assert target.startswith(callback + "&")
    assert parse_qs(urlsplit(target).query)["state"] == ["callback-state"]
    assert 'class="oauth-page oauth-continuation"' in page.text
    assert 'role="status"' in page.text
    assert "Returning to the application…" in page.text
    assert 'id="mp-continue"' in page.text and 'rel="noreferrer"' in page.text
    for asset in ("ui.css", "oauth.css", "mark.svg"):
        assert f'/static/{asset}' in page.text
    assert "<script" not in page.text
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["cache-control"] == "no-store"
    assert "font-src 'self'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert (await browser.get(response.headers["location"])).status_code == 403
