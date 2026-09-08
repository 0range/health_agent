from datetime import UTC, datetime, timedelta

import httpx
import pytest

from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.whoop.client import WhoopAuthorizationRequired, WhoopClient
from health_agent.whoop.oauth import WHOOP_SCOPES, WhoopOAuth
from health_agent.whoop.repository import register_authorized_connection
from health_agent.whoop.sync import sync_whoop
from health_agent.whoop.tokens import TokenStore, WhoopToken


@pytest.mark.parametrize('failure', ['timeout', 503, 429, 403, 'invalid_json'])
def test_temporary_refresh_failure_preserves_grant_and_next_sync_recovers(session, tmp_path, failure):
    connection = register_authorized_connection(session, DEFAULT_PROFILE_ID, 'main', 123, WHOOP_SCOPES)
    store = TokenStore(tmp_path / 'tokens')
    store.save(str(DEFAULT_PROFILE_ID), 'main', WhoopToken('old-access', 'old-refresh', datetime.now(UTC) - timedelta(hours=1), WHOOP_SCOPES))
    calls = []

    def tokens(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == 'timeout':
                raise httpx.ConnectTimeout('private transport details', request=request)
            if failure == 'invalid_json':
                return httpx.Response(200, text='not json')
            return httpx.Response(failure, text='private response body')
        return httpx.Response(200, json={'access_token':'new-access','refresh_token':'new-refresh','expires_in':3600,'scope':' '.join(WHOOP_SCOPES)})

    def data(request):
        assert request.headers['Authorization'] == 'Bearer new-access'
        if request.url.path.endswith('/profile/basic'):
            return httpx.Response(200, json={'user_id':123,'email':'test@example.test','first_name':'Test','last_name':'User'})
        if request.url.path.endswith('/measurement/body'):
            return httpx.Response(200, json={'height_meter':1.8,'weight_kilogram':80,'max_heart_rate':190})
        return httpx.Response(200, json={'records':[]})

    client = WhoopClient(WhoopOAuth('client','secret','http://127.0.0.1:8765/callback',http_client=httpx.Client(transport=httpx.MockTransport(tokens))), store, str(DEFAULT_PROFILE_ID), 'main', http_client=httpx.Client(transport=httpx.MockTransport(data)))
    first = sync_whoop(session, DEFAULT_PROFILE_ID, 'main', client)
    assert first.status == 'failed' and first.safe_error_code == 'sync_failed'
    assert connection.auth_status == 'connected'
    assert store.load(str(DEFAULT_PROFILE_ID),'main').refresh_token == 'old-refresh'
    session.commit()
    second = sync_whoop(session, DEFAULT_PROFILE_ID, 'main', client)
    assert second.status == 'succeeded'
    assert store.load(str(DEFAULT_PROFILE_ID),'main').refresh_token == 'new-refresh'
    assert len(calls) == 2


def test_explicit_rejected_refresh_still_requires_login(tmp_path):
    store = TokenStore(tmp_path / 'tokens')
    store.save('test', 'main', WhoopToken('a', 'r', datetime.now(UTC) - timedelta(hours=1), WHOOP_SCOPES))
    oauth = WhoopOAuth('client','secret','http://127.0.0.1:8765/callback', http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400,json={'error':'invalid_grant','error_description':'private details'}))))
    client=WhoopClient(oauth,store,'test','main')
    with pytest.raises(WhoopAuthorizationRequired) as caught:
        client.get_object('/v2/user/profile/basic')
    assert 'private details' not in str(caught.value)
