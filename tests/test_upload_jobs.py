"""Browser jobs use real Core reservations and never treat a job ID as a credential."""

import asyncio
import io
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from minder_publish.errors import PublishError
from minder_publish.server.auth import AuthStore, Principal
from minder_publish.server.config import Config
from minder_publish.server.core import Core
from minder_publish.server.upload_jobs import UploadJobs


@pytest.fixture
def setup(tmp_path):
    config = Config(tmp_path / "data", secret="test-secret-" * 8, testing=True)
    config.limits.update(
        file_bytes=1 << 20,
        upload_payload_bytes=1 << 20,
        site_expanded_bytes=2 << 20,
        content_quota_bytes=16 << 20,
        disk_reserve_bytes=1 << 20,
        disk_reserve_percent=1,
    )
    config.operations.update(export_metadata_bytes=1024, export_buffer_bytes=1024)
    config.storage_budget_bytes = 32 << 20
    core = Core(config).initialize()
    auth = AuthStore(config)
    auth.initialize()
    auth.bootstrap("owner", "a long owner passphrase")
    owner_raw, _ = auth.login("owner", "a long owner passphrase", "127.0.0.1")
    owner = auth.session(owner_raw)
    device = auth.create_token("agent", "materials:manage")
    creator = auth.token(device["token"])
    clock = [time.time()]
    jobs = UploadJobs(core, clock=lambda: clock[0]).initialize()
    yield core, auth, owner, creator, device, jobs, clock
    core.close()


def start(jobs, owner, creator, **intent):
    job = jobs.create({"kind": "file", "title": "Agent result", **intent}, creator)
    upload = jobs.start(job["id"], owner, {"filename": "result.html", "size_bytes": 8})
    return job, upload


async def body(value=b"reviewed"):
    yield value


def test_job_id_never_authorizes_and_other_creator_cannot_read_metadata(setup):
    _, auth, owner, creator, _, jobs, _ = setup
    job = jobs.create({"kind": "file", "title": "Private task"}, creator)
    other = auth.token(auth.create_token("other", "materials:manage")["token"])
    for principal in (None, other, Principal("owner", "forged", "owner")):
        with pytest.raises(PublishError):
            jobs.get(job["id"], principal)
    assert jobs.get(job["id"], creator)["intent"]["title"] == "Private task"
    assert jobs.get(job["id"], owner)["state"] == "pending"
    read_only = auth.token(auth.create_token("reader", "materials:read")["token"])
    with pytest.raises(PublishError):
        jobs.create({"kind": "file"}, read_only)
    with pytest.raises(PublishError):
        jobs.create({"kind": "file"}, owner)


def test_start_uses_owner_upload_and_status_only_observes_real_commit(
    setup, monkeypatch
):
    core, _, owner, creator, _, jobs, _ = setup
    job, upload = start(jobs, owner, creator, summary="A summary")
    assert jobs.get(job["id"], owner)["upload_id"] == upload["id"]
    assert "upload_id" not in jobs.get(job["id"], creator)
    assert jobs.authorize_upload(upload["id"], owner) is True
    with pytest.raises(PublishError):
        jobs.authorize_upload(upload["id"], creator)
    asyncio.run(core.receive_upload(upload["id"], body(), owner.subject))
    core.inspect_upload(upload["id"], owner.subject)
    publication = core.commit_upload(
        upload["id"], owner.subject, authorize=jobs.guard_commit
    )
    monkeypatch.setattr(
        core,
        "commit_upload",
        lambda *args, **kwargs: pytest.fail("status must never commit"),
    )
    result = jobs.get(job["id"], creator)
    assert (
        result["state"] == "published" and result["publication_id"] == publication["id"]
    )
    assert result["stable_url"].endswith("/p/" + publication["id"])
    assert jobs.get(job["id"], owner)["state"] == "published"
    with pytest.raises(PublishError):
        jobs.cancel(job["id"], creator)
    assert core.get_publication(publication["id"])["summary"] == "A summary"


def test_parallel_claim_cas_rolls_back_losing_upload_and_reservation(setup):
    core, auth, owner, creator, _, jobs, _ = setup
    second_raw, _ = auth.login("owner", "a long owner passphrase", "127.0.0.2")
    second = auth.session(second_raw)
    job = jobs.create({"kind": "file"}, creator)
    barrier = threading.Barrier(2)

    def claim(principal):
        barrier.wait(timeout=5)
        try:
            return jobs.start(
                job["id"], principal, {"filename": "result.txt", "size_bytes": 8}
            )
        except PublishError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, principal) for principal in (owner, second)]
        results = [future.result(timeout=15) for future in futures]
    assert sum(value is not None for value in results) == 1
    with core.repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM upload").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM upload WHERE reservation_bytes>0"
            ).fetchone()[0]
            == 1
        )


def test_creator_revocation_prevents_claim_and_prepared_commit(setup):
    core, auth, owner, creator, device, jobs, _ = setup
    pending = jobs.create({"kind": "file"}, creator)
    job, upload = start(jobs, owner, creator)
    asyncio.run(core.receive_upload(upload["id"], body(), owner.subject))
    core.inspect_upload(upload["id"], owner.subject)
    auth.revoke_token(device["id"])
    with pytest.raises(PublishError):
        jobs.start(pending["id"], owner, {"filename": "x.txt", "size_bytes": 1})
    with pytest.raises(PublishError):
        core.commit_upload(upload["id"], owner.subject, authorize=jobs.guard_commit)
    jobs.cleanup()
    assert core.list_publications()["total"] == 0
    with core.repository.connect() as connection:
        assert (
            connection.execute("SELECT SUM(reservation_bytes) FROM upload").fetchone()[
                0
            ]
            == 0
        )


def test_cancel_aborts_only_its_bound_upload_and_expiry_cannot_reopen_it(setup):
    core, _, owner, creator, _, jobs, clock = setup
    first, first_upload = start(jobs, owner, creator)
    second, second_upload = start(jobs, owner, creator)
    assert jobs.cancel(first["id"], creator)["state"] == "cancelled"
    with core.repository.connect() as connection:
        states = dict(connection.execute("SELECT id,state FROM upload"))
    assert (
        states[first_upload["id"]] == "aborted"
        and states[second_upload["id"]] == "planned"
    )
    clock[0] += 3601
    jobs.cleanup()
    with core.repository.connect() as connection:
        assert (
            connection.execute(
                "SELECT state FROM upload WHERE id=?", (second_upload["id"],)
            ).fetchone()[0]
            == "aborted"
        )
        assert connection.execute("SELECT COUNT(*) FROM upload_job").fetchone()[0] == 0
        with pytest.raises(PublishError):
            jobs.guard_commit(connection, second_upload["id"])


def test_intent_has_bounded_nonsecret_fields_and_password_is_chosen_by_owner(setup):
    core, _, owner, creator, _, jobs, _ = setup
    for intent in (
        {"kind": "file", "password": "do-not-store"},
        {"kind": "file", "title": "x" * 201},
        {"kind": "site", "entry_path": "../x"},
        {"kind": "file", "expiry": {"mode": "unknown"}},
    ):
        with pytest.raises(PublishError):
            jobs.create(intent, creator)
    job = jobs.create({"kind": "file", "access": "password"}, creator)
    upload = jobs.start(
        job["id"],
        owner,
        {
            "filename": "protected.txt",
            "size_bytes": 8,
            "password": "owner chosen passphrase",
        },
    )
    with core.repository.connect() as connection:
        record = dict(
            connection.execute(
                "SELECT * FROM upload_job WHERE id=?", (job["id"],)
            ).fetchone()
        )
    assert "owner chosen passphrase" not in json.dumps(record)
    assert "password_hash" not in json.dumps(record)
    assert upload["id"]


def test_job_queue_is_bounded_and_capacity_returns_after_ttl(setup):
    core, _, _, creator, _, _, clock = setup
    jobs = UploadJobs(core, maximum_jobs=2, clock=lambda: clock[0]).initialize()
    jobs.create({"kind": "file"}, creator)
    jobs.create({"kind": "site"}, creator)
    with pytest.raises(PublishError) as failure:
        jobs.create({"kind": "file"}, creator)
    assert failure.value.status == 429
    clock[0] += 3601
    assert jobs.create({"kind": "file"}, creator)["state"] == "pending"


def test_site_job_validates_entry_and_preserves_normal_owner_policy(setup):
    core, _, owner, creator, _, jobs, _ = setup
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("pages/home.html", "<h1>Home</h1>")
        archive.writestr("assets/app.css", "body{color:blue}")
    payload = output.getvalue()
    job = jobs.create(
        {
            "kind": "site",
            "entry_path": "pages/home.html",
            "title": "Agent site",
            "access": "link",
            "expiry": {"mode": "default"},
        },
        creator,
    )
    upload = jobs.start(
        job["id"],
        owner,
        {
            "filename": "site.zip",
            "size_bytes": len(payload),
            "expanded_size_bytes": len(b"<h1>Home</h1>") + len(b"body{color:blue}"),
            "site_routing": "spa",
            "external_resources": "self_only",
            "purge_mode": "never",
        },
    )
    asyncio.run(core.receive_upload(upload["id"], body(payload), owner.subject))
    core.inspect_upload(upload["id"], owner.subject)
    publication = core.commit_upload(
        upload["id"], owner.subject, authorize=jobs.guard_commit
    )
    assert (
        publication["kind"] == "site" and publication["entry_path"] == "pages/home.html"
    )
    assert publication["purge_mode"] == "never" and publication["site_routing"] == "spa"
    assert publication["external_resources"] == "self_only"
    assert jobs.get(job["id"], creator)["publication_id"] == publication["id"]


def test_invalid_core_plan_does_not_claim_job_or_leave_reservation(setup):
    core, _, owner, creator, _, jobs, _ = setup
    job = jobs.create({"kind": "file"}, creator)
    with pytest.raises(PublishError):
        jobs.start(job["id"], owner, {"filename": "file.txt", "size_bytes": 1 << 21})
    assert jobs.get(job["id"], creator)["state"] == "pending"
    with core.repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM upload").fetchone()[0] == 0
    for forbidden in ("publication_id", "expected_revision"):
        with pytest.raises(PublishError):
            jobs.start(
                job["id"],
                owner,
                {"filename": "x.txt", "size_bytes": 1, forbidden: "other"},
            )


def test_current_owner_session_binding_cannot_be_taken_over_and_expiry_denies_commit(
    setup,
):
    core, auth, owner, creator, _, jobs, clock = setup
    job, upload = start(jobs, owner, creator)
    second_raw, _ = auth.login("owner", "a long owner passphrase", "127.0.0.2")
    second = auth.session(second_raw)
    assert jobs.get(job["id"], second)["claimed_by_current_session"] is False
    with pytest.raises(PublishError):
        jobs.authorize_upload(upload["id"], second)
    asyncio.run(core.receive_upload(upload["id"], body(), owner.subject))
    core.inspect_upload(upload["id"], owner.subject)
    with auth.connection(write=True) as connection:
        connection.execute(
            "UPDATE auth_sessions SET expires_at=? WHERE token_hash=?",
            (clock[0] - 1, owner.id),
        )
    with pytest.raises(PublishError):
        core.commit_upload(upload["id"], owner.subject, authorize=jobs.guard_commit)
    jobs.cleanup()
    assert core.list_publications()["total"] == 0
    with core.repository.connect() as connection:
        assert (
            connection.execute(
                "SELECT reservation_bytes FROM upload WHERE id=?", (upload["id"],)
            ).fetchone()[0]
            == 0
        )


def test_oauth_creator_family_scope_resource_and_revocation_are_checked_inside_guard(
    setup,
):
    from minder_publish.server.oauth.store import OAuthStore

    core, auth, owner, _, _, jobs, clock = setup
    oauth = OAuthStore(core.config).initialize()
    family = "a" * 32
    with auth.connection(write=True) as connection:
        connection.execute(
            "INSERT INTO oauth_family VALUES(?,?,?,?,?,?,?,NULL)",
            (
                family,
                "client",
                '["materials:manage"]',
                "owner",
                oauth.resource,
                int(clock[0]),
                int(clock[0] + 3600),
            ),
        )
    creator = Principal("oauth", family, "materials:manage")
    job, upload = start(jobs, owner, creator)
    asyncio.run(core.receive_upload(upload["id"], body(), owner.subject))
    core.inspect_upload(upload["id"], owner.subject)
    with auth.connection(write=True) as connection:
        connection.execute(
            "UPDATE oauth_family SET revoked_at=? WHERE id=?", (int(clock[0]), family)
        )
    with pytest.raises(PublishError) as failure:
        core.commit_upload(upload["id"], owner.subject, authorize=jobs.guard_commit)
    assert failure.value.code == "upload_job_creator_revoked"
    assert core.list_publications()["total"] == 0
    with pytest.raises(PublishError):
        jobs.get(job["id"], creator)


def test_unknown_job_schema_is_rejected_and_regular_upload_is_unaffected(setup):
    core, _, owner, _, _, jobs, _ = setup
    upload = core.create_upload(
        {"kind": "file", "filename": "regular.txt", "size_bytes": 8}, owner.subject
    )
    assert jobs.authorize_upload(upload["id"], owner) is False
    with core.repository.connect(immediate=True) as connection:
        connection.execute("UPDATE upload_job_schema_version SET version=999")
    with pytest.raises(PublishError) as failure:
        UploadJobs(core).initialize()
    assert failure.value.code == "unknown_upload_job_schema"


@pytest.mark.parametrize("operation", ["cancel", "expire", "revoke"])
async def test_active_receiver_retains_reservation_until_exit_then_cleans_its_bytes(
    setup, operation
):
    core, auth, owner, creator, device, jobs, clock = setup
    job, upload = start(jobs, owner, creator)
    waiting = asyncio.Event()
    proceed = asyncio.Event()

    async def delayed_body():
        yield b"revi"
        waiting.set()
        await proceed.wait()
        yield b"ewed"

    receiver = asyncio.create_task(
        core.receive_upload(upload["id"], delayed_body(), owner.subject)
    )
    await asyncio.wait_for(waiting.wait(), timeout=5)
    try:
        if operation == "cancel":
            jobs.cancel(job["id"], creator)
        else:
            if operation == "expire":
                clock[0] += 3601
            else:
                auth.revoke_token(device["id"])
            jobs.cleanup()
        with core.repository.connect() as connection:
            record = connection.execute(
                "SELECT state,reservation_bytes FROM upload WHERE id=?", (upload["id"],)
            ).fetchone()
        assert record["state"] == "aborting" and record["reservation_bytes"] > 0
    finally:
        proceed.set()
    with pytest.raises(PublishError):
        await asyncio.wait_for(receiver, timeout=5)
    jobs.cleanup()
    with core.repository.connect() as connection:
        record = connection.execute(
            "SELECT state,reservation_bytes FROM upload WHERE id=?", (upload["id"],)
        ).fetchone()
    assert record["state"] == "aborted" and record["reservation_bytes"] == 0
    assert not core.storage.stage(upload["id"]).exists()
