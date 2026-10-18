"""Real Primary-owned consent: second destination, denial and cross-user approval.

The reporting fixture covers issuance, not a reporting A2A implementation.
"""
import secrets
import time

import httpx

from e2e_delegation import login
from e2e_device import authorization_link, consent, finish


def reporting_fixture():
    with httpx.Client(base_url='http://keycloak:8080',timeout=30) as admin:
        response=admin.post('/realms/master/protocol/openid-connect/token',data={
            'grant_type':'password','client_id':'admin-cli','username':'admin','password':'lab-admin'})
        assert response.status_code==200,'Test admin login failed'
        admin.headers['Authorization']='Bearer '+response.json()['access_token']
        def client(name):
            result=admin.get('/admin/realms/lab/clients',params={'clientId':name})
            assert result.status_code==200,'Test client lookup failed'
            return result.json()
        finance=client('finance-agent')[0]
        if not client('reporting-agent'):
            reporting={k:v for k,v in finance.items() if k!='id'}
            reporting.update(clientId='reporting-agent',secret='lab-reporting-secret')
            assert admin.post('/admin/realms/lab/clients',json=reporting).status_code==201,'Test client creation failed'
        reporting=client('reporting-agent')[0]
        system=client('admin-permissions')[0]
        root='/admin/realms/lab/clients/'+system['id']+'/authz/resource-server'
        policies=admin.get(root+'/policy/client')
        assert policies.status_code==200,'Test policy lookup failed'
        policy=next(p for p in policies.json() if p['name']=='finance-agent-can-delegate')
        policy['clients']=[finance['id'],reporting['id']]
        assert admin.put(root+'/policy/client/'+policy['id'],json=policy).status_code in (200,201,204),'Test policy update failed'


def main():
    reporting_fixture()
    with httpx.Client(verify='/src/.local-certs/cert.pem',timeout=30) as identity:
        alice=login(identity,'alice','alice-lab',scope='openid profile email primary-audience',audience='primary-agent')[0]
    with httpx.Client(base_url='http://primary-agent:8000',timeout=90) as primary:
        def request(model='finance-lab'):
            headers={'Authorization':'Bearer '+alice,'X-Conversation-ID':'destinations-'+secrets.token_hex(16)}
            body={'model':model,'messages':[{'role':'user','content':'{"tool":"read_invoice","id":1}'}]}
            response=primary.post('/v1/chat/completions',headers=headers,json=body)
            link=authorization_link(response)
            assert link,'New conversation must require consent'
            return headers,body,link
        # Same Primary client, same protocol, a different configured agent.
        headers,body,link=request('reporting-lab')
        with httpx.Client(verify='/src/.local-certs/cert.pem',timeout=30) as browser:
            consent(browser,link,'alice','alice-lab')
        response=finish(primary,headers,body)
        assert response.json()['choices'][0]['message']['content']=='REPORTING: authorized','Wrong destination token'
        # Reject a grant approved by Bob for an independently authenticated Alice.
        headers,body,link=request()
        with httpx.Client(verify='/src/.local-certs/cert.pem',timeout=30) as browser:
            consent(browser,link,'bob','bob-lab')
        time.sleep(5.1)
        response=primary.post('/v1/chat/completions',headers=headers,json=body)
        assert response.status_code==403,'Cross-user device authorization accepted'
        # Reject explicit refusal before any agent call.
        headers,body,link=request()
        with httpx.Client(verify='/src/.local-certs/cert.pem',timeout=30) as browser:
            consent(browser,link,'alice','alice-lab',accept=False)
        time.sleep(5.1)
        response=primary.post('/v1/chat/completions',headers=headers,json=body)
        assert response.status_code==403,'Refused delegation accepted'
    print('Live Primary-owned consent passed: second destination, cross-user rejection and refusal')


if __name__=='__main__': main()
