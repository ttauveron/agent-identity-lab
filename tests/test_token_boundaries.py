"""Primary-owned consent, destination restriction and user isolation."""
import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from services.common import security
from services.finance_agent import app as finance
from services.primary import app as primary
from services.primary.delegation import Delegations, Destination, Grant

FINANCE = Destination('finance-agent', 'finance-lab', 'http://finance/a2a', 'finance-agent-audience')
REPORTING = Destination('reporting-agent', 'reporting-lab', 'http://reporting/a2a', 'reporting-agent-audience')


@pytest.fixture
def tokens(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(security._jwks, 'get_signing_key_from_jwt', lambda _: SimpleNamespace(key=key.public_key()))

    def signed(audience, **changes):
        return jwt.encode({'iss': security.ISSUER, 'aud': audience, 'sub': 'alice',
                           'exp': time.time() + 300,
                           'may_act': {'client_id': audience, 'sub': 'service-account'}, **changes}, key, algorithm='RS256')

    return signed


def pending(manager, subject='alice', conversation='chat', destination=FINANCE):
    manager.grants[(subject, conversation, destination.client_id)] = Grant(
        time.time() + 300, device_code='PRIVATE-DEVICE-CODE', link='https://consent', next_poll=0)


def responses(monkeypatch, manager, *items):
    queue = list(items)
    calls = []

    async def post(url, data):
        calls.append((url, data))
        assert queue, 'Unexpected identity provider call'
        return queue.pop(0)

    monkeypatch.setattr(manager, '_post', post)
    return calls


@pytest.mark.parametrize('case,status', [
    ('missing', 502), ('malformed', 401), ('expired', 401), ('wrong-audience', 401),
    ('wrong-issuer', 401), ('signature', 401), ('wrong-user', 403), ('shared-audience', 403),
    ('upstream-reused', 401), ('missing-may-act', 403), ('wrong-actor', 403), ('missing-actor-sub', 403), ('wrong-actor-issuer', 403),
])
def test_completed_grant_is_verified_before_use(tokens, monkeypatch, case, status):
    manager = Delegations()
    pending(manager)
    downstream = tokens('finance-agent')
    if case == 'malformed': downstream = 'malformed-token'
    elif case == 'expired': downstream = tokens('finance-agent', exp=time.time() - 60)
    elif case == 'wrong-audience': downstream = tokens('finance-api')
    elif case == 'wrong-issuer': downstream = tokens('finance-agent', iss='https://wrong-issuer.example')
    elif case == 'signature':
        downstream = jwt.encode({'iss': security.ISSUER, 'aud': 'finance-agent', 'sub': 'alice', 'exp': time.time()+300},
                                rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm='RS256')
    elif case == 'wrong-user': downstream = tokens('finance-agent', sub='bob')
    elif case == 'shared-audience': downstream = tokens(['finance-agent', 'primary-agent'])
    elif case == 'upstream-reused': downstream = tokens('primary-agent')
    elif case == 'missing-may-act': downstream = tokens('finance-agent', may_act=None)
    elif case == 'wrong-actor': downstream = tokens('finance-agent', may_act={'client_id':'reporting-agent','sub':'service'})
    elif case == 'missing-actor-sub': downstream = tokens('finance-agent', may_act={'client_id':'finance-agent'})
    elif case == 'wrong-actor-issuer': downstream = tokens('finance-agent',may_act={'client_id':'finance-agent','sub':'service','iss':'https://wrong.example'})
    responses(monkeypatch, manager, (200, {} if case == 'missing' else {'access_token':downstream}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(manager.token('alice', 'chat', FINANCE))
    assert error.value.status_code == status
    assert not manager.grants
    assert downstream not in error.value.detail


def test_consent_pending_denial_slow_down_and_expiry(monkeypatch):
    manager = Delegations()
    calls = responses(monkeypatch, manager,
        (200, {'device_code':'PRIVATE-DEVICE-CODE','user_code':'ABCD-EFGH','expires_in':300,'interval':5,
               'verification_uri_complete':'https://attacker.example'}),
        (400, {'error':'authorization_pending'}), (400, {'error':'slow_down'}),
        (400, {'error':'access_denied','error_description':'PRIVATE-DEVICE-CODE'}))
    token, link = asyncio.run(manager.token('alice', 'chat', FINANCE))
    assert token is None and link == security.ISSUER+'/device?user_code=ABCD-EFGH'
    assert calls[0][1] == {'scope':'openid delegation:client:finance-agent'}
    # Repeated requests respect the provider's polling interval.
    assert asyncio.run(manager.token('alice','chat',FINANCE)) == (None,link)
    assert len(calls)==1
    grant = manager.grants[('alice','chat','finance-agent')]
    grant.next_poll=0
    assert asyncio.run(manager.token('alice','chat',FINANCE)) == (None,link)
    grant.next_poll=0
    assert asyncio.run(manager.token('alice','chat',FINANCE)) == (None,link)
    assert grant.interval==10 and grant.next_poll > time.time()+9
    grant.next_poll=0
    with pytest.raises(HTTPException) as error: asyncio.run(manager.token('alice','chat',FINANCE))
    assert error.value.status_code==403 and 'PRIVATE' not in error.value.detail
    assert not manager.grants


@pytest.mark.parametrize('error', ['expired_token','invalid_grant'])
def test_expired_or_consumed_device_code_is_discarded(monkeypatch,error):
    manager=Delegations(); pending(manager)
    responses(monkeypatch,manager,(400,{'error':error}))
    with pytest.raises(HTTPException): asyncio.run(manager.token('alice','chat',FINANCE))
    assert not manager.grants


def test_two_destinations_and_users_never_share_grants(tokens,monkeypatch):
    manager=Delegations()
    finance=tokens('finance-agent'); reporting=tokens('reporting-agent')
    pending(manager); pending(manager,destination=REPORTING)
    calls=responses(monkeypatch,manager,(200,{'access_token':finance,'refresh_token':'PRIVATE-REFRESH'}),
                    (200,{'access_token':reporting}))
    assert asyncio.run(manager.token('alice','chat',FINANCE))==(finance,None)
    assert asyncio.run(manager.token('alice','chat',REPORTING))==(reporting,None)
    assert asyncio.run(manager.token('alice','chat',FINANCE))==(finance,None)
    assert len(calls)==2
    assert 'PRIVATE-REFRESH' not in repr(manager.grants)
    # Another user or conversation must obtain a fresh authorization.
    responses(monkeypatch,manager,*[(200,{'device_code':'new-private','user_code':'NEWW-CODE','expires_in':300})]*2)
    assert asyncio.run(manager.token('bob','chat',FINANCE))[0] is None
    assert asyncio.run(manager.token('alice','different-chat',FINANCE))[0] is None


def test_restart_expiry_and_capacity_fail_closed(tokens,monkeypatch):
    manager=Delegations(capacity=1)
    manager.grants[('alice','chat','finance-agent')]=Grant(time.time()+300,access_token=tokens('finance-agent'))
    with pytest.raises(HTTPException) as error: asyncio.run(manager.token('bob','chat',FINANCE))
    assert error.value.status_code==503
    manager.grants[('alice','chat','finance-agent')].expires=0
    responses(monkeypatch,manager,(200,{'device_code':'private','user_code':'NEWW-CODE','expires_in':300}))
    assert asyncio.run(manager.token('alice','chat',FINANCE))[0] is None
    restarted=Delegations()
    responses(monkeypatch,restarted,(200,{'device_code':'private','user_code':'NEWW-CODE','expires_in':300}))
    assert asyncio.run(restarted.token('alice','chat',FINANCE))[0] is None


@pytest.mark.parametrize('stream',[False,True])
def test_consent_response_bypasses_llm_and_session_storage(tokens,monkeypatch,stream):
    def forbidden(*_,**__): pytest.fail('Consent must not reach agent, A2A or stored LLM session')
    manager=Delegations()
    responses(monkeypatch,manager,(200,{'device_code':'PRIVATE-DEVICE-CODE','user_code':'ABCD-EFGH','expires_in':300}))
    monkeypatch.setattr(primary,'delegations',manager)
    monkeypatch.setattr(primary,'client',forbidden)
    monkeypatch.setattr(primary,'sessions',SimpleNamespace(load=forbidden,save=forbidden))
    response=TestClient(primary.app).post('/v1/chat/completions',headers={
        'Authorization':'Bearer '+tokens('primary-agent',may_act=None),'X-Conversation-ID':'chat',
        'X-Finance-Authorization':'Bearer untrusted-client-token'},json={'stream':stream,'messages':[{'role':'user','content':'invoice'}]})
    assert response.status_code==200 and 'Authorize finance-agent' in response.text
    assert 'PRIVATE-DEVICE-CODE' not in response.text and 'untrusted-client-token' not in response.text


@pytest.mark.parametrize("destination",[FINANCE,REPORTING])
def test_primary_keeps_upstream_out_of_tool_context(tokens,monkeypatch,destination):
    upstream=tokens('primary-agent',may_act=None); downstream=tokens(destination.client_id)
    monkeypatch.setattr(primary,'destinations',[FINANCE,REPORTING])
    manager=Delegations(); pending(manager,destination=destination)
    responses(monkeypatch,manager,(200,{'access_token':downstream}))
    class Agent:
        def __init__(self,**_): pass
        def create_session(self): return object()
        async def run(self,message,*,session):
            assert primary._subject_token.get()==downstream and downstream!=upstream
            assert primary._destination.get()==destination
            assert 'user_code' not in message
            return SimpleNamespace(text='OK')
    monkeypatch.setattr(primary,'delegations',manager)
    monkeypatch.setattr(primary,'Agent',Agent)
    monkeypatch.setattr(primary,'client',lambda:None)
    monkeypatch.setattr(primary,'sessions',SimpleNamespace(load=lambda *_:None,save=lambda *_:None))
    response=TestClient(primary.app).post('/v1/chat/completions',headers={
        'Authorization':'Bearer '+upstream,'X-Conversation-ID':'chat'},json={'model':destination.model,'messages':[{'role':'user','content':'invoice'}]})
    assert response.status_code==200 and response.json()['choices'][0]['message']['content']=='OK'
    for context in (primary._subject_token,primary._destination):
        with pytest.raises(LookupError): context.get()


def test_unknown_destination_cannot_request_scope(tokens,monkeypatch):
    async def forbidden(*_,**__): pytest.fail('Unknown model must not request OAuth permission')
    monkeypatch.setattr(primary.delegations,'token',forbidden)
    response=TestClient(primary.app).post('/v1/chat/completions',headers={
        'Authorization':'Bearer '+tokens('primary-agent'),'X-Conversation-ID':'chat'},
        json={'model':'attacker-agent','messages':[{'role':'user','content':'invoice'}]})
    assert response.status_code==400


def test_finance_rejects_shared_audience_before_exchange(tokens,monkeypatch):
    def forbidden(*_,**__): pytest.fail('Shared audience must not reach token exchange')
    monkeypatch.setattr(finance,'client_token',forbidden)
    response=TestClient(finance.app).post('/a2a',headers={'Authorization':'Bearer '+tokens(['primary-agent','finance-agent'])},
        json={'method':'SendMessage','params':{}})
    assert response.status_code==403


def test_capacity_reservation_survives_concurrent_requests_and_cancellation(monkeypatch):
    async def run():
        manager=Delegations(capacity=1)
        entered=asyncio.Event()
        never=asyncio.Event()
        async def blocked(*_):
            entered.set()
            await never.wait()
        monkeypatch.setattr(manager,'_post',blocked)
        first=asyncio.create_task(manager.token('alice','chat',FINANCE))
        await entered.wait()
        with pytest.raises(HTTPException) as error:
            await manager.token('bob','chat',FINANCE)
        assert error.value.status_code==503
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        assert not manager.grants
    asyncio.run(run())
