"""Real Primary-owned consent → A2A → Keycloak → Finance API ALLOW/DENY."""
import json
import secrets

import httpx

from e2e_delegation import login
from e2e_device import authorization_link, consent, finish


def main():
    users={}
    for username in ('alice','bob'):
        with httpx.Client(verify='/src/.local-certs/cert.pem',timeout=30) as identity:
            users[username]=login(identity,username,username+'-lab',
                scope='openid profile email primary-audience',audience='primary-agent')[0]
    conversations={name:'e2e-'+secrets.token_hex(16) for name in users}
    with httpx.Client(base_url='http://primary-agent:8000',timeout=90) as primary:
        def ask(username,tool,resource_id):
            headers={'Authorization':'Bearer '+users[username],'X-Conversation-ID':conversations[username]}
            body={'messages':[{'role':'user','content':json.dumps({'tool':tool,'id':resource_id})}]}
            response=primary.post('/v1/chat/completions',headers=headers,json=body)
            link=authorization_link(response)
            if link:
                with httpx.Client(verify='/src/.local-certs/cert.pem',timeout=30) as browser:
                    consent(browser,link,username,username+'-lab')
                response=finish(primary,headers,body)
            return response.json()['choices'][0]['message']['content']
        allowed=ask('alice','read_invoice',1)
        try: invoice=json.loads(allowed)
        except ValueError: raise AssertionError('Full-chain ALLOW did not return an invoice') from None
        assert invoice['id']==1 and invoice['department']=='finance','Unexpected allowed resource'
        assert 'bank_account' not in invoice,'Sensitive field exposed'
        assert ask('alice','read_invoice',2)=='DENY: finance policy','Department DENY missing'
        assert ask('alice','approve_payment',1)=='DENY: finance policy','Agent capability DENY missing'
        assert ask('bob','read_invoice',2)=='DENY: finance policy','User permission DENY missing'
    print('Live Primary → A2A → Keycloak → Finance API checks passed (ALLOW and DENY)')


if __name__=='__main__': main()
