"""Historical upload outcomes are subject-bound and disclose no publication payload."""

import asyncio
from datetime import timedelta

import pytest
import test_core_domain as domain

from minder_publish.errors import PublishError
from minder_publish.server.core import iso, now

core = domain.core


def test_status_read_only_redacted_and_historical_commit(core):
    upload = core.create_upload(
        {
            "kind": "file",
            "filename": "secret-name.txt",
            "size_bytes": 1,
            "title": "private title",
            "access": "password",
            "password": "private password",
        },
        "first",
    )
    with core.repository.connect() as connection:
        before = tuple(
            connection.execute(
                "SELECT * FROM upload WHERE id=?", (upload["id"],)
            ).fetchone()
        )
    assert core.upload_status(upload["id"], "first") == {
        "id": upload["id"],
        "state": "planned",
        "expires_at": upload["expires_at"],
    }
    with core.repository.connect() as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM upload WHERE id=?", (upload["id"],)
                ).fetchone()
            )
            == before
        )
    with pytest.raises(PublishError) as error:
        core.upload_status(upload["id"], "second")
    assert error.value.status == 404
    asyncio.run(core.receive_upload(upload["id"], domain.chunks(b"x"), "first"))
    assert core.upload_status(upload["id"], "first")["state"] == "uploaded"
    publication = core.commit_upload(upload["id"], "first")
    result = core.upload_status(upload["id"], "first")
    assert set(result) == {"id", "state", "expires_at", "publication_id", "link_name", "stable_url"}
    assert (
        result["state"] == "committed" and result["publication_id"] == publication["id"]
    )
    assert result["stable_url"] == core.config.base_url + "/p/" + publication["id"]
    core.delete_publication(publication["id"])
    assert core.upload_status(upload["id"], "first") == result
    with core.repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM revision").fetchone()[0] == 1
        connection.execute("DELETE FROM publication WHERE id=?", (publication["id"],))
    assert core.upload_status(upload["id"], "first") == result


def test_unfinished_expiry_and_missing_status_are_ambiguous(core):
    upload = core.create_upload(
        {"kind": "file", "filename": "x", "size_bytes": 0}, "first"
    )
    with core.repository.connect(immediate=True) as connection:
        connection.execute(
            "UPDATE upload SET deadline=? WHERE id=?",
            (iso(now() - timedelta(seconds=1)), upload["id"]),
        )
    with pytest.raises(PublishError) as error:
        core.upload_status(upload["id"], "first")
    assert error.value.status == 410
    with pytest.raises(PublishError) as error:
        core.upload_status("missing", "first")
    assert error.value.status == 404
