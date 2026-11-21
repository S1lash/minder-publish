"""Owner password retrieval has a narrower boundary than material management."""

import logging
import time

import anyio
import pytest
from test_http import publish, sign_in

from minder_publish.client.api import PublishClient
from minder_publish.client.errors import ClientError
from minder_publish.mcp.internal_api import InternalApiTransport
from minder_publish.server.auth import Principal


async def test_copy_requires_owner_empty_body_origin_and_csrf(
    application, client, monkeypatch
):
    material, manage = await publish(
        application, client, access="password", password="owner-only password"
    )
    path = f"/api/v1/publications/{material['id']}/password-copy"
    assert (await client.post(path)).status_code == 401
    assert (
        await client.post(path, headers=manage, content=b"not json")
    ).status_code == 403
    token = application.state.auth.create_token("read", "materials:read")["token"]
    assert (
        await client.post(path, headers={"Authorization": "Bearer " + token})
    ).status_code == 403
    headers = await sign_in(client)
    assert (await client.post(path)).status_code == 403
    assert (
        await client.post(path, headers={**headers, "Origin": "https://evil.test"})
    ).status_code == 403
    assert (
        await client.post(path, headers={**headers, "X-CSRF-Token": "wrong"})
    ).status_code == 403
    with monkeypatch.context() as patch:

        def forbidden(_publication):
            pytest.fail("Rejected bodies must never reach password decryption")

        patch.setattr(application.state.core.password_vault, "open", forbidden)
        assert (await client.post(path, headers=headers, json={})).status_code == 400
    assert (await client.get(path, headers=headers)).status_code == 404
    response = await client.post(path, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"] == {"password": "owner-only password"}
    assert response.headers["cache-control"] == "no-store"
    origin = application.state.config.content_origin(material["current_revision"])
    assert (await client.post(origin + path, headers=headers)).status_code == 404


async def test_recent_auth_is_checked_inside_the_publication_transaction(
    application, client, monkeypatch
):
    material, _ = await publish(
        application, client, access="password", password="recent owner password"
    )
    headers = await sign_in(client)
    path = f"/api/v1/publications/{material['id']}/password-copy"
    auth = application.state.auth
    with auth.connection(write=True) as connection:
        connection.execute(
            "UPDATE auth_sessions SET authenticated_at=?",
            (
                time.time()
                - application.state.config.operations["owner_recent_auth_seconds"]
                - 1,
            ),
        )
    response = await client.post(path, headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "reauthentication_required"
    assert (
        await client.post(
            "/api/v1/auth/reauth",
            headers=headers,
            json={"password": "a long test passphrase"},
        )
    ).status_code == 200
    original = auth.require_recent_auth
    seen = []

    def checked(raw, *, connection=None):
        assert connection is not None and connection.in_transaction
        seen.append(connection)
        return original(raw, connection=connection)

    monkeypatch.setattr(auth, "require_recent_auth", checked)
    assert (await client.post(path, headers=headers)).status_code == 200
    assert len(seen) == 1


async def test_revocation_after_admission_cannot_return_a_password(
    application, client, monkeypatch
):
    material, _ = await publish(
        application, client, access="password", password="revocation protected"
    )
    headers = await sign_in(client)
    auth = application.state.auth
    original = auth.validate_csrf

    def revoked_after_csrf(raw, supplied):
        valid = original(raw, supplied)
        auth.invalidate_all()
        return valid

    monkeypatch.setattr(auth, "validate_csrf", revoked_after_csrf)
    response = await client.post(
        f"/api/v1/publications/{material['id']}/password-copy", headers=headers
    )
    assert response.status_code == 401
    assert "revocation protected" not in response.text


async def test_internal_oauth_cannot_retrieve_a_password(application, client):
    material, _ = await publish(
        application, client, access="password", password="not an app secret"
    )

    def operation():
        with PublishClient(
            "https://publish.test",
            "opaque-oauth-test-credential",
            transport=InternalApiTransport(
                application,
                lambda: Principal("oauth", "copy-test-grant", "materials:manage"),
            ),
        ) as api:
            value = api.get(material["id"])
            assert "password_sealed" not in value and "password_hash" not in value
            with pytest.raises(ClientError) as error:
                api.request("POST", f"/publications/{material['id']}/password-copy")
            assert error.value.status == 403

    await anyio.to_thread.run_sync(operation)


async def test_passwords_and_ciphertext_stay_out_of_metadata_and_logs(
    application, client, caplog
):
    secret = "synthetic-never-log-publication-password"
    caplog.set_level(logging.INFO, logger="minder_publish.http")
    material, manage = await publish(
        application, client, access="password", password=secret
    )
    headers = await sign_in(client)
    stored = application.state.core.get_publication(material["id"])
    for response in (
        await client.get("/api/v1/publications", headers=manage),
        await client.get(f"/api/v1/publications/{material['id']}", headers=manage),
        await client.patch(
            f"/api/v1/publications/{material['id']}",
            headers=manage,
            json={"title": "Updated"},
        ),
    ):
        assert response.status_code == 200
        assert secret not in response.text
        assert (
            "password_sealed" not in response.text
            and "password_hash" not in response.text
        )
    response = await client.post(
        f"/api/v1/publications/{material['id']}/password-copy", headers=headers
    )
    assert response.status_code == 200
    assert secret not in caplog.text
    assert stored["password_sealed"]["sealed"] not in caplog.text


async def test_hash_only_copy_response_guides_explicit_reset(application, client):
    material, _ = await publish(
        application, client, access="password", password="legacy password"
    )
    core = application.state.core
    with core.repository.connect(immediate=True) as connection:
        saved = core._publication(connection, material["id"])
        saved.pop("password_sealed")
        core._save_publication(connection, saved)
    headers = await sign_in(client)
    response = await client.post(
        f"/api/v1/publications/{material['id']}/password-copy", headers=headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "password_copy_unavailable"
    assert response.json()["action"] == "reset_publication_password"
