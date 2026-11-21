"""Opening admission counts missing keys and uses only the resolved ASGI peer."""

import asyncio
import hashlib
import hmac
import re
import time

import httpx
import pytest
from test_http import publish
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from minder_publish.errors import PublishError
from minder_publish.server import auth as auth_module
from minder_publish.server.auth import AuthStore


def bucket_hash(auth, bucket, namespace="publication"):
    key = hmac.new(
        auth.config.secret.encode(), bucket.encode(), hashlib.sha256
    ).hexdigest()
    return "publication:" + key if namespace == "publication" else key


def seed_opening_bucket(auth, peer, count=300):
    instant = time.time()
    key = bucket_hash(auth, "open:" + peer)
    with auth.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO auth_attempts VALUES (?,?,?,?)",
            (key, instant, count, instant + 60),
        )
    return key


def opening_row(auth, peer):
    with auth.connection() as connection:
        row = connection.execute(
            "SELECT * FROM auth_attempts WHERE bucket_hash=?",
            (bucket_hash(auth, "open:" + peer),),
        ).fetchone()
    return dict(row) if row else None


async def test_three_hundred_missing_keys_exhaust_one_peer_despite_spoofed_headers(
    application, client, monkeypatch
):
    material, _ = await publish(application, client, access="link")
    instant = time.time()
    monkeypatch.setattr(auth_module.time, "time", lambda: instant)
    peer = "198.51.100.20"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=(peer, 40000)),
        base_url="https://publish.test",
        follow_redirects=False,
    ) as attacker:
        for index in range(300):
            spoofed = f"198.18.{index // 256}.{index % 256}"
            response = await attacker.get(
                f"/p/missing-{index}",
                headers={
                    "X-Forwarded-For": spoofed,
                    "X-Real-IP": spoofed,
                    "Forwarded": f"for={spoofed}",
                    "User-Agent": "TelegramBot" if index % 2 else "Normal browser",
                },
            )
            assert response.status_code == 404
        for path, method, headers in (
            ("/p/another-missing-key", "GET", {}),
            (material["stable_url"], "GET", {"User-Agent": "TelegramBot"}),
            (material["stable_url"], "HEAD", {}),
        ):
            response = await attacker.request(method, path, headers=headers)
            assert response.status_code == 429
    auth = application.state.auth
    assert opening_row(auth, peer)["count"] == 300
    with auth.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 1
        )


async def test_untrusted_proxy_cannot_change_the_resolved_opening_peer(application):
    peer = "198.51.100.30"
    auth = application.state.auth
    seed_opening_bucket(auth, peer)
    wrapped = ProxyHeadersMiddleware(application, trusted_hosts="127.0.0.1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped, client=(peer, 40000)),
        base_url="https://publish.test",
    ) as attacker:
        for index in range(3):
            response = await attacker.get(
                "/p/unknown",
                headers={
                    "X-Forwarded-For": f"203.0.113.{index + 1}",
                    "X-Real-IP": f"192.0.2.{index + 1}",
                },
            )
            assert response.status_code == 429
    assert opening_row(auth, peer)["count"] == 300
    assert opening_row(auth, "203.0.113.1") is None


async def test_trusted_proxy_selects_forwarded_peer_without_using_x_real_ip(
    application,
):
    auth = application.state.auth
    exhausted = "198.51.100.40"
    fresh = "198.51.100.41"
    seed_opening_bucket(auth, exhausted)
    wrapped = ProxyHeadersMiddleware(application, trusted_hosts="127.0.0.1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped, client=("127.0.0.1", 40000)),
        base_url="https://publish.test",
    ) as proxy:
        response = await proxy.get(
            "/p/unknown",
            headers={"X-Forwarded-For": exhausted, "X-Real-IP": fresh},
        )
        assert response.status_code == 429
        response = await proxy.get(
            "/p/unknown",
            headers={"X-Forwarded-For": fresh, "X-Real-IP": exhausted},
        )
        assert response.status_code == 404
    assert opening_row(auth, fresh)["count"] == 1
    assert opening_row(auth, "127.0.0.1") is None


async def test_concurrent_requests_cannot_overrun_the_last_opening_slot(application):
    auth = application.state.auth
    peer = "198.51.100.50"
    seed_opening_bucket(auth, peer, count=299)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=(peer, 40000)),
        base_url="https://publish.test",
    ) as attacker:
        responses = await asyncio.gather(
            *(attacker.get(f"/p/unknown-{index}") for index in range(8))
        )
    assert sorted(response.status_code for response in responses) == [404] + [429] * 7
    assert opening_row(auth, peer)["count"] == 300


async def test_legacy_path_cannot_skip_stable_opening_admission(application):
    peer = "198.51.100.60"
    seed_opening_bucket(application.state.auth, peer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=(peer, 40000)),
        base_url="https://publish.test",
        follow_redirects=True,
    ) as attacker:
        response = await attacker.get("/unknown-legacy-key")
    assert response.status_code == 429
    assert response.history[0].status_code == 303
    assert response.url.path == "/p/unknown-legacy-key"


async def test_opening_budget_survives_auth_store_restart(application):
    auth = application.state.auth
    peer = "198.51.100.70"
    seed_opening_bucket(auth, peer)
    restarted = AuthStore(application.state.config)
    restarted.initialize()
    with pytest.raises(PublishError) as error:
        restarted.throttle(
            "open:" + peer, maximum=300, seconds=60, namespace="publication"
        )
    assert error.value.code == "rate_limited"
    assert opening_row(restarted, peer)["count"] == 300


async def test_opening_budget_expires_at_its_fixed_window_boundary(
    application, monkeypatch
):
    auth = application.state.auth
    peer = "198.51.100.80"
    instant = [1000.0]
    monkeypatch.setattr(auth_module.time, "time", lambda: instant[0])
    seed_opening_bucket(auth, peer)
    instant[0] = 1059.999
    with pytest.raises(PublishError) as error:
        auth.throttle("open:" + peer, maximum=300, seconds=60, namespace="publication")
    assert error.value.code == "rate_limited"
    instant[0] = 1060.0
    auth.throttle("open:" + peer, maximum=300, seconds=60, namespace="publication")
    row = opening_row(auth, peer)
    assert row["count"] == 1 and row["started_at"] == 1060.0
    assert row["expires_at"] == 1120.0


async def test_full_publication_pool_preserves_owner_login_and_reauthentication(
    application, monkeypatch
):
    auth = application.state.auth
    instant = [1000.0]
    monkeypatch.setattr(auth_module.time, "time", lambda: instant[0])
    existing_peer = "198.51.100.90"
    seed_opening_bucket(auth, existing_peer, count=1)
    with auth.connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO auth_attempts VALUES (?,?,?,?)",
            (
                (bucket_hash(auth, f"open:synthetic-peer-{index}"), 1000.0, 1, 1060.0)
                for index in range(9999)
            ),
        )
    cookie, _ = auth.login("owner", "a long test passphrase", "198.51.100.91")
    assert auth.reauthenticate(cookie, "a long test passphrase", "198.51.100.91")[
        "recent_auth"
    ]
    auth.throttle(
        "open:" + existing_peer, maximum=300, seconds=60, namespace="publication"
    )
    with pytest.raises(PublishError) as error:
        auth.throttle("open:new-peer", maximum=300, seconds=60, namespace="publication")
    assert error.value.code == "rate_limited"
    with auth.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0]
            == 10002
        )
    instant[0] = 1060.0
    auth.throttle("open:new-peer", maximum=300, seconds=60, namespace="publication")
    with auth.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 3
        )


async def test_full_authentication_pool_does_not_block_publication_openings(
    application, client
):
    auth = application.state.auth
    instant = time.time()
    with auth.connection(write=True) as connection:
        connection.executemany(
            "INSERT INTO auth_attempts VALUES (?,?,?,?)",
            (
                (
                    bucket_hash(
                        auth,
                        f"login:synthetic-peer-{index}",
                        namespace="authentication",
                    ),
                    instant,
                    1,
                    instant + 900,
                )
                for index in range(10000)
            ),
        )
    with pytest.raises(PublishError) as error:
        auth.throttle("login:new-peer")
    assert error.value.code == "rate_limited"
    response = await client.get("/p/unknown-material")
    assert response.status_code == 404
    assert opening_row(auth, "127.0.0.1")["count"] == 1


async def test_authentication_preserves_legacy_hash_buckets_and_publication_is_separate(
    application,
):
    auth = application.state.auth
    bucket = "login:legacy-peer"
    instant = time.time()
    legacy_key = bucket_hash(auth, bucket, namespace="authentication")
    with auth.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO auth_attempts VALUES (?,?,?,?)",
            (legacy_key, instant, 9, instant + 900),
        )
    auth.throttle(bucket)
    with pytest.raises(PublishError) as error:
        auth.throttle(bucket)
    assert error.value.code == "rate_limited"
    auth.throttle(bucket, namespace="publication")
    with auth.connection() as connection:
        counts = {
            row["bucket_hash"]: row["count"]
            for row in connection.execute("SELECT bucket_hash,count FROM auth_attempts")
        }
    assert counts == {legacy_key: 10, bucket_hash(auth, bucket): 1}


async def test_publication_password_failures_use_the_publication_namespace(
    application, client
):
    material, _ = await publish(
        application, client, access="password", password="material password"
    )
    gate = await client.get(material["stable_url"])
    csrf = re.search('name="csrf" value="([a-f0-9]+)"', gate.text).group(1)
    response = await client.post(
        material["stable_url"],
        headers={"Origin": "https://publish.test"},
        data={"csrf": csrf, "password": "wrong password"},
    )
    assert response.status_code == 401
    auth = application.state.auth
    password_key = bucket_hash(auth, f"password:{material['id']}:127.0.0.1")
    with auth.connection() as connection:
        assert (
            connection.execute(
                "SELECT count FROM auth_attempts WHERE bucket_hash=?", (password_key,)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM auth_attempts WHERE bucket_hash NOT LIKE 'publication:%'"
            ).fetchone()[0]
            == 0
        )


async def test_throttle_rejects_unknown_internal_namespace_before_writing(application):
    auth = application.state.auth
    with pytest.raises(ValueError):
        auth.throttle("test", namespace="user-controlled")
    with auth.connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0] == 0
        )


async def test_custom_opening_window_is_shared_by_real_routes(application, client):
    core = application.state.core
    core.update_settings({"defaults": {"publication_open_attempts_per_minute": 1}})
    assert (await client.get("/p/missing")).status_code == 404
    blocked = await client.get("/p/missing")
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60


async def test_password_budget_uses_material_policy(application, client):
    material, _ = await publish(application, client, access="password", password="material password")
    application.state.core.update_settings({"defaults": {
        "publication_password_attempts": 1, "publication_password_window_minutes": 30,
    }})
    gate = await client.get(material["stable_url"])
    csrf = re.search('name="csrf" value="([a-f0-9]+)"', gate.text).group(1)
    request = {"headers": {"Origin": "https://publish.test"}, "data": {"csrf": csrf, "password": "mistyped"}}
    assert (await client.post(material["stable_url"], **request)).status_code == 401
    blocked = await client.post(material["stable_url"], **request)
    assert blocked.status_code == 429
    assert 1790 <= int(blocked.headers["Retry-After"]) <= 1800


@pytest.mark.parametrize(("field", "value"), [
    ("publication_open_attempts_per_minute", 0), ("publication_open_attempts_per_minute", 301),
    ("publication_password_attempts", True), ("publication_password_attempts", 11),
    ("publication_password_window_minutes", 14), ("publication_password_window_minutes", 61),
])
def test_publication_security_settings_reject_unsafe_values(application, field, value):
    with pytest.raises(PublishError) as error:
        application.state.core.update_settings({"defaults": {field: value}})
    assert error.value.field == field
