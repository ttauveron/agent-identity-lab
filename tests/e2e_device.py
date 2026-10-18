"""Real browser-side approval of a Primary-owned device request; no secrets printed."""
import re
import time
from urllib.parse import urljoin

from e2e_delegation import ISSUER, Forms


def consent(client, link, username, password, *, accept=True):
    assert link.startswith(ISSUER+'/device?user_code='), 'Untrusted consent link'
    response=client.get(link)
    consent_seen=False
    for _ in range(20):
        if response.status_code==302:
            response=client.get(urljoin(ISSUER,response.headers['location']))
            continue
        assert response.status_code==200, 'Browser authorization failed'
        parser=Forms(); parser.feed(response.text)
        if not parser.forms:
            break
        item=parser.forms[0]
        fields=dict(item['inputs'])
        if 'authenticate' in item['action']:
            fields.update(username=username,password=password)
        elif 'consent' in item['action']:
            consent_seen=True
            fields.pop('cancel',None); fields.pop('accept',None)
            fields['accept' if accept else 'cancel']='Yes'
        else:
            raise AssertionError('Unexpected device authorization form')
        response=client.post(urljoin(ISSUER,item['action']),data=fields)
    assert consent_seen,'Explicit delegation consent missing'


def authorization_link(response):
    assert response.status_code==200, 'Primary authorization request failed'
    text=response.json()['choices'][0]['message']['content']
    link=re.search(re.escape(ISSUER)+r'/device\?user_code=[A-Za-z0-9-]+',text)
    return link.group(0) if link else None


def finish(primary, headers, body):
    # Respect Keycloak's default five-second poll interval.
    time.sleep(5.1)
    response=primary.post('/v1/chat/completions',headers=headers,json=body)
    assert response.status_code==200,'Authorized Primary request failed'
    assert not authorization_link(response),'Authorization still pending'
    return response
