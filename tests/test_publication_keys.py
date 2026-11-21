"""Short identities reserve committed, pending and deleted names atomically."""

import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_core_domain import publish

from minder_publish.errors import PublishError
from minder_publish.server.core import Core
from minder_publish.server.storage import safe_id

pytest_plugins = ("test_core_domain",)


def plan(**extra):
    return dict(kind="file", filename="note.txt", size_bytes=5, **extra)


def test_viewer_route_is_reserved_for_new_keys_without_invalidating_existing_ids(core):
    with pytest.raises(PublishError) as error:
        core.create_upload(plan(key="viewer"), "owner")
    assert error.value.code == "reserved_key"
    assert safe_id("viewer") == "viewer"


async def test_existing_viewer_key_still_opens_on_publication_route(application, client, monkeypatch):
    from minder_publish.server import storage
    with monkeypatch.context() as previous_version:
        previous_version.setattr(storage, "_RESERVED_KEYS", storage._RESERVED_KEYS - {"viewer"})
        # Existing imports can retain this identifier, so emulate its earlier creation.
        # The test HTTP helper does not accept custom keys; reserve via upload directly.
        body = b"legacy viewer"
        token = application.state.auth.create_token("legacy", "materials:manage")["token"]
        headers = {"Authorization": "Bearer " + token}
        upload = await client.post("/api/v1/uploads", json={"kind": "file", "filename": "legacy.txt", "size_bytes": len(body), "key": "viewer", "access": "link"}, headers=headers)
        assert upload.status_code == 201
        upload_id = upload.json()["data"]["id"]
        assert (await client.put(f"/api/v1/uploads/{upload_id}/content", content=body, headers=headers)).status_code == 200
        assert (await client.post(f"/api/v1/uploads/{upload_id}/commit", headers=headers)).status_code == 200
    opened = await client.get("/p/viewer")
    assert opened.status_code == 200 and opened.content == body


def test_new_random_keys_are_short_and_old_custom_keys_stay_unchanged(core):
    first = publish(core)
    assert re.fullmatch(r"[a-z0-9]{8}", first["id"])
    assert re.fullmatch(r"[a-f0-9]{32}", first["current_revision"])
    explicit = publish(core, key="test1")
    assert explicit["id"] == "test1"
    legacy = publish(core, key="legacy-" + "a" * 25)
    updated = publish(
        core, publication_id=legacy["id"], expected_revision=legacy["current_revision"]
    )
    assert updated["id"] == legacy["id"]


def test_random_collisions_grow_across_committed_pending_and_tombstones(
    core, monkeypatch
):
    publish(core, key="aaaaaaaa")
    with pytest.raises(PublishError, match="publication exists"):
        core.create_upload(plan(key="aaaaaaaa"), "owner")
    core.create_upload(plan(key="aaaaaaaaa"), "owner")
    with core.repository.connect(immediate=True) as connection:
        connection.execute(
            "INSERT INTO deletion_tombstone(publication_id, deleted_at) VALUES(?,?)",
            ("aaaaaaaaaa", "2026-01-01T00:00:00Z"),
        )
    monkeypatch.setattr(
        "minder_publish.server.core.secrets.choice", lambda alphabet: "a"
    )
    result = core.create_upload(plan(), "owner")
    with core.repository.connect() as connection:
        data = json.loads(
            connection.execute(
                "SELECT data FROM upload WHERE id=?", (result["id"],)
            ).fetchone()[0]
        )
    assert data["publication"]["id"] == "a" * 11


@pytest.mark.parametrize("minimum,maximum", [(4, 4), (5, 6), (8, 16), (32, 32)])
def test_owner_key_length_range_controls_new_keys(core, minimum, maximum):
    core.update_settings(
        {
            "defaults": {
                "publication_key_min_characters": minimum,
                "publication_key_max_characters": maximum,
            }
        }
    )
    assert len(publish(core)["id"]) == minimum
    # Explicit names keep their independent create-only validation.
    assert publish(core, key="hi")["id"] == "hi"


@pytest.mark.parametrize(
    "minimum,maximum", [(3, 16), (8, 33), (9, 8), (True, 16), (8, 8.5)]
)
def test_invalid_key_length_range_rejects_without_changing_settings(
    core, minimum, maximum
):
    before = core.settings()["defaults"]
    with pytest.raises(PublishError):
        core.update_settings(
            {
                "defaults": {
                    "publication_key_min_characters": minimum,
                    "publication_key_max_characters": maximum,
                }
            }
        )
    assert core.settings()["defaults"] == before


def test_collision_at_maximum_fails_without_overwrite(core, monkeypatch):
    core.update_settings(
        {
            "defaults": {
                "publication_key_min_characters": 8,
                "publication_key_max_characters": 8,
            }
        }
    )
    original = publish(core, key="aaaaaaaa")
    monkeypatch.setattr(
        "minder_publish.server.core.secrets.choice", lambda alphabet: "a"
    )
    with pytest.raises(PublishError) as rejected:
        core.create_upload(plan(), "owner")
    assert rejected.value.code == "publication_key_exhausted"
    assert (
        core.get_publication(original["id"])["current_revision"]
        == original["current_revision"]
    )


def test_existing_settings_gain_read_defaults_without_rewriting_database(core):
    with core.repository.connect(immediate=True) as connection:
        data = json.loads(
            connection.execute("SELECT data FROM core_settings WHERE id=1").fetchone()[
                0
            ]
        )
        for name in (
            "publication_key_min_characters",
            "publication_key_max_characters",
        ):
            data["defaults"].pop(name)
        saved = json.dumps(data)
        connection.execute("UPDATE core_settings SET data=? WHERE id=1", (saved,))
    restarted = Core(core.config).initialize()
    assert restarted.settings()["defaults"]["publication_key_min_characters"] == 8
    with restarted.repository.connect() as connection:
        assert (
            connection.execute("SELECT data FROM core_settings WHERE id=1").fetchone()[
                0
            ]
            == saved
        )


def test_collision_at_maximum_retries_a_fresh_candidate(core, monkeypatch):
    core.update_settings(
        {
            "defaults": {
                "publication_key_min_characters": 8,
                "publication_key_max_characters": 8,
            }
        }
    )
    publish(core, key="aaaaaaaa")
    symbols = iter("aaaaaaaa" + "bbbbbbbb")
    monkeypatch.setattr(
        "minder_publish.server.core.secrets.choice", lambda alphabet: next(symbols)
    )
    assert publish(core)["id"] == "bbbbbbbb"


def test_concurrent_allocations_reserve_distinct_pending_names(core, monkeypatch):
    monkeypatch.setattr(
        "minder_publish.server.core.secrets.choice", lambda alphabet: "a"
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        uploads = list(
            executor.map(lambda _: core.create_upload(plan(), "owner"), range(2))
        )
    with core.repository.connect() as connection:
        identities = {
            json.loads(
                connection.execute(
                    "SELECT data FROM upload WHERE id=?", (item["id"],)
                ).fetchone()[0]
            )["publication"]["id"]
            for item in uploads
        }
    assert identities == {"a" * 8, "a" * 9}
