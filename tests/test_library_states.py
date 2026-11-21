"""Library state selection is applied before search totals and pagination."""

import pytest
from test_http import publish


async def test_library_states_filter_search_and_pagination(application, client):
    core = application.state.core
    active, headers = await publish(application, client)
    paused, _ = await publish(application, client)
    expired, _ = await publish(application, client)
    removed, _ = await publish(application, client)
    core.pause(paused['id'], True)
    core.pause(expired['id'], True)
    core.expire(expired['id'])
    core.expire(removed['id'])
    core.patch_publication(removed['id'], {'purge_mode': 'immediate'})
    core.sweep()

    result = await client.get('/api/v1/publications', params={
        'states': 'active,paused', 'q': 'DESCRIPTION', 'limit': 1,
    }, headers=headers)
    assert result.status_code == 200
    data = result.json()['data']
    assert data['total'] == 2 and len(data['items']) == 1
    assert data['items'][0]['library_status'] in ('active', 'paused')
    page = await client.get('/api/v1/publications', params={
        'states': 'active,paused', 'q': 'DESCRIPTION', 'limit': 1, 'offset': 1,
    }, headers=headers)
    assert {data['items'][0]['id'], page.json()['data']['items'][0]['id']} == {active['id'], paused['id']}

    for state, identifier in [('expired', expired['id']), ('deleted', removed['id'])]:
        result = await client.get('/api/v1/publications', params={
            'states': state, 'q': 'DESCRIPTION',
        }, headers=headers)
        assert result.status_code == 200
        data = result.json()['data']
        assert data['total'] == 1
        assert data['items'][0]['id'] == identifier
        assert data['items'][0]['library_status'] == state

    # Existing clients that omit the new filter retain the complete result set.
    result = await client.get('/api/v1/publications', headers=headers)
    assert result.json()['data']['total'] == 4
    # A paused expired record retains the old status filter's precedence.
    result = await client.get('/api/v1/publications?status=paused', headers=headers)
    assert result.json()['data']['total'] == 2
    result = await client.get('/api/v1/publications?states=', headers=headers)
    assert result.json()['data']['total'] == 0


@pytest.mark.parametrize('states', ['unknown', 'active,unknown', 'active,', ',active', 'ACTIVE'])
async def test_library_states_reject_unknown_values(application, client, states):
    _, headers = await publish(application, client)
    response = await client.get('/api/v1/publications', params={'states': states}, headers=headers)
    assert response.status_code == 400
    assert response.json()['code'] == 'invalid_library_states'


async def test_library_manual_deletion_pending_precedes_pause_and_expiry(application, client):
    core = application.state.core
    material, headers = await publish(application, client)
    lease = core.acquire_lease(material['current_revision'])
    core.pause(material['id'], True)
    core.delete_publication(material['id'])
    response = await client.get('/api/v1/publications?states=active,paused', headers=headers)
    assert response.json()['data']['total'] == 0
    response = await client.get('/api/v1/publications?states=deleted', headers=headers)
    assert response.json()['data']['items'][0]['id'] == material['id']
    core.release_lease(lease)
    core.sweep()
    response = await client.get('/api/v1/publications?states=deleted', headers=headers)
    assert response.json()['data']['total'] == 0
