"""Browser handoff crosses real device, owner, HTTP and Core boundaries."""

from test_http import sign_in


async def test_browser_handoff_publishes_once_and_creator_sees_only_result(
    application, client
):
    token = application.state.auth.create_token("assistant", "materials:manage")
    headers = {"Authorization": "Bearer " + token["token"]}
    created = await client.post(
        "/api/v1/upload-jobs",
        headers=headers,
        json={
            "kind": "file",
            "title": "My PDF",
            "summary": "A browser handoff",
            "access": "private",
        },
    )
    assert created.status_code == 201, created.text
    job = created.json()["data"]
    assert (await client.get("/upload/" + job["id"])).status_code == 200
    assert (await client.get("/api/v1/upload-jobs/" + job["id"])).status_code == 401
    owner = await sign_in(client)
    body = b"%PDF-1.4\nA private handoff test"
    plan = {
        "kind": "file",
        "filename": "result.pdf",
        "size_bytes": len(body),
        "title": "My PDF",
        "access": "private",
    }
    response = await client.post(
        f"/api/v1/upload-jobs/{job['id']}/start", headers=owner, json=plan
    )
    assert response.status_code == 201, response.text
    upload = response.json()["data"]["id"]
    assert (
        await client.put(
            f"/api/v1/uploads/{upload}/content", headers=owner, content=body
        )
    ).status_code == 200
    # A job creator has no access to the owner's upload body or staging identifier.
    state = (
        await client.get(f"/api/v1/upload-jobs/{job['id']}", headers=headers)
    ).json()["data"]
    assert "upload_id" not in state and "owner_id" not in state
    assert (
        await client.post(f"/api/v1/uploads/{upload}/inspect", headers=owner, json={})
    ).status_code == 200
    response = await client.post(
        f"/api/v1/uploads/{upload}/commit", headers=owner, json={}
    )
    assert response.status_code == 200, response.text
    material = response.json()["data"]
    recovery = await client.get(f"/api/v1/uploads/{upload}")
    assert recovery.status_code == 200, recovery.text
    recovered = recovery.json()["data"]
    assert (
        recovered["state"] == "committed"
        and recovered["stable_url"] == material["stable_url"]
    )
    assert set(recovered) == {
        "id",
        "state",
        "expires_at",
        "publication_id",
        "link_name",
        "stable_url",
    }
    denied = await client.get(f"/api/v1/uploads/{upload}", headers=headers)
    assert (
        denied.status_code == 404
    )  # The creator cannot inspect the owner's staging upload.
    result = (
        await client.get(f"/api/v1/upload-jobs/{job['id']}", headers=headers)
    ).json()["data"]
    assert (
        result["state"] == "published"
        and result["stable_url"] == material["stable_url"]
    )
    assert (
        await client.delete(f"/api/v1/upload-jobs/{job['id']}", headers=headers)
    ).status_code == 409
    assert (
        await client.get(
            f"/api/v1/publications/{material['id']}/download", headers=headers
        )
    ).content == body


async def test_creator_revocation_blocks_normal_commit_route(application, client):
    token = application.state.auth.create_token("assistant", "materials:manage")
    creator = {"Authorization": "Bearer " + token["token"]}
    job = (
        await client.post("/api/v1/upload-jobs", headers=creator, json={"kind": "file"})
    ).json()["data"]
    owner = await sign_in(client)
    response = await client.post(
        f"/api/v1/upload-jobs/{job['id']}/start",
        headers=owner,
        json={"kind": "file", "filename": "report.txt", "size_bytes": 4},
    )
    upload = response.json()["data"]["id"]
    assert (
        await client.put(
            f"/api/v1/uploads/{upload}/content", headers=owner, content=b"test"
        )
    ).status_code == 200
    application.state.auth.revoke_token(token["id"])
    response = await client.post(
        f"/api/v1/uploads/{upload}/commit", headers=owner, json={}
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "upload_job_closed"
    assert application.state.core.list_publications()["total"] == 0
