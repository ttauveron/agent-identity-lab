'use strict';
// Unmodified pinned LibreChat server, native OIDC login/refresh, Keycloak,
// Primary/A2A/Finance API. No token or credential is printed, including failures.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const imageRequire = require('node:module').createRequire('/app/api/strategies/openidStrategy.js');
const undici = imageRequire('undici');
const issuer = process.env.OPENID_ISSUER;
const redirect = 'http://localhost:3080/oauth/openid/callback';
const cookies = new Map();
const decode = value => value.replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/&quot;/g, '"');

async function browser(url, options = {}) {
  url = new URL(url);
  if (!cookies.has(url.hostname)) cookies.set(url.hostname, new Map());
  const jar = cookies.get(url.hostname);
  const response = await undici.fetch(url, { redirect: 'manual', ...options, headers: {
    'user-agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36', ...options.headers, cookie: [...jar].map(([k, v]) => `${k}=${v}`).join('; '),
  }});
  for (const line of response.headers.getSetCookie()) {
    const pair = line.split(';')[0];
    jar.set(pair.slice(0, pair.indexOf('=')), pair.slice(pair.indexOf('=') + 1));
  }
  return response;
}

// Demo admin is used only by this isolated integration test to shorten expiry.
async function shortTokenLifetime() {
  const master = issuer.replace('/realms/lab','/realms/master');
  let response = await undici.fetch(master+'/protocol/openid-connect/token',{method:'POST',body:new URLSearchParams({
    grant_type:'password',client_id:'admin-cli',username:'admin',password:'lab-admin'})});
  assert.equal(response.status,200,'Test admin login failed');
  const authorization='Bearer '+(await response.json()).access_token;
  const base=issuer.replace('/realms/lab','/admin/realms/lab/clients');
  response=await undici.fetch(base+'?clientId=librechat',{headers:{authorization}});
  assert.equal(response.status,200,'Test client lookup failed');
  const original=(await response.json())[0];
  response=await undici.fetch(base+'/'+original.id,{method:'PUT',headers:{authorization,'content-type':'application/json'},
    body:JSON.stringify({...original,attributes:{...original.attributes,'access.token.lifespan':'125'}})});
  assert.equal(response.status,204,'Test expiry configuration failed');
  return async () => {
    const auth=await undici.fetch(master+'/protocol/openid-connect/token',{method:'POST',body:new URLSearchParams({
      grant_type:'password',client_id:'admin-cli',username:'admin',password:'lab-admin'})});
    assert.equal(auth.status,200,'Test restoration login failed');
    const freshAuthorization='Bearer '+(await auth.json()).access_token;
    const restored=await undici.fetch(base+'/'+original.id,{method:'PUT',headers:{authorization:freshAuthorization,'content-type':'application/json'},
      body:JSON.stringify({...original,attributes:{...original.attributes,'access.token.lifespan':original.attributes?.['access.token.lifespan'] ?? null}})});
    assert.equal(restored.status,204,'Test expiry restoration failed');
    const check=await undici.fetch(base+'/'+original.id,{headers:{authorization:freshAuthorization}});
    assert.equal(check.status,200,'Test restoration verification failed');
    assert.equal((await check.json()).attributes?.['access.token.lifespan'] ?? null,original.attributes?.['access.token.lifespan'] ?? null,
      'Client token lifetime was not restored');
  };
}
let stage="login";
async function main() {
  let response = await browser('http://localhost:3080/oauth/openid');
  for (let i=0;i<15;i++) {
    if (response.status===302) {
      const target=response.headers.get('location');
      response=await browser(new URL(target,'http://localhost:3080'));
      if (!response.headers.get('location') && target==='http://localhost:3080') break;
      continue;
    }
    const page=await response.text();
    if (page.includes('<title>LibreChat')) break;
    const form=[...page.matchAll(/<form\b([^>]*)>([\s\S]*?)<\/form>/g)].find(m=> /action=["'][^"']*(?:authenticate|consent|required-action)/.test(m[1]));
    assert.ok(form,'Native login form missing');
    const action=decode(/action=["']([^"']*)/.exec(form[1])[1]);
    const fields=new URLSearchParams();
    for (const input of form[2].matchAll(/<input\b([^>]*)>/g)) {
      const name=/name=["']([^"']*)/.exec(input[1]);const value=/value=["']([^"']*)/.exec(input[1]);
      if(name) fields.set(name[1],value?decode(value[1]):'');
    }
    if(action.includes('authenticate')) {fields.set('username','alice');fields.set('password','alice-lab');}
    else {fields.delete('cancel');fields.set('accept','Yes');}
    response=await browser(new URL(action,issuer),{method:'POST',body:fields});
  }
  await new Promise(resolve=>setTimeout(resolve,126000));
  stage='refresh';
  response=await browser('http://localhost:3080/api/auth/refresh',{method:'POST'});
  assert.equal(response.status,200,'Native refresh request failed');
  const refreshText=await response.text();
  assert.ok(refreshText.startsWith('{'),'Refresh response missing');
  const session=JSON.parse(refreshText);
  assert.ok(session.token,'Missing native app auth token');
  stage='model-catalog';
  response=await browser('http://localhost:3080/api/models',{headers:{Authorization:'Bearer '+session.token}});
  assert.equal(response.status,200,'Native model catalog failed');
  const models=await response.json();
  assert.ok(models['Agent Lab']?.includes('finance-lab'),'Finance destination missing from native catalog');
  assert.ok(models['Agent Lab']?.includes('reporting-lab'),'Second destination missing from native catalog');
  const conversationId=crypto.randomUUID();
  async function chat() {
    const start=await browser('http://localhost:3080/api/agents/chat/Agent%20Lab',{method:'POST',
      headers:{'content-type':'application/json',Authorization:'Bearer '+session.token},
      body:JSON.stringify({endpoint:'Agent Lab',endpointType:'custom',model:'finance-lab',
       text:JSON.stringify({tool:'read_invoice',id:1}),conversationId,
       parentMessageId:'00000000-0000-0000-0000-000000000000',messageId:crypto.randomUUID(),isContinued:false})});
    assert.equal(start.status,200,'Native chat start failed');
    const startText=await start.text();
    assert.ok(startText.startsWith('{'),'Native chat rejected');
    const job=JSON.parse(startText);
    assert.ok(job.streamId,'Native chat stream missing');
    const stream=await browser(`http://localhost:3080/api/agents/chat/stream/${job.streamId}`,{
      headers:{Authorization:'Bearer '+session.token}});
    assert.equal(stream.status,200,'Native stream failed');
    return stream.text();
  }
  stage='consent-request';
  let text=await chat();
  const link=text.match(/https:\/\/keycloak\.localhost:8443\/realms\/lab\/device\?user_code=[A-Za-z0-9-]+/);
  assert.ok(link,'Primary consent link missing from native conversation');
  assert.ok(!text.includes('Laptop fleet'),'Finance ran before consent');
  response=await browser(link[0]);
  let consentSeen=false;
  for(let i=0;i<15;i++) {
    if(response.status===302) {response=await browser(new URL(response.headers.get('location'),issuer));continue;}
    const page=await response.text();
    const form=[...page.matchAll(/<form\b([^>]*)>([\s\S]*?)<\/form>/g)][0];
    if(!form) break;
    const action=decode(/action=["']([^"']*)/.exec(form[1])[1]);
    const fields=new URLSearchParams();
    for(const input of form[2].matchAll(/<input\b([^>]*)>/g)) {
      const name=/name=["']([^"']*)/.exec(input[1]);const value=/value=["']([^"']*)/.exec(input[1]);
      if(name) fields.set(name[1],value?decode(value[1]):'');
    }
    if(action.includes('authenticate')) {fields.set('username','alice');fields.set('password','alice-lab');}
    else if(action.includes('consent')) {consentSeen=true;fields.delete('cancel');fields.set('accept','Yes');}
    else assert.fail('Unexpected device authorization form');
    response=await browser(new URL(action,issuer),{method:'POST',body:fields});
  }
  assert.ok(consentSeen,'Explicit delegation consent missing');
  await new Promise(resolve=>setTimeout(resolve,5100));
  stage='authorized-chat';
  text=await chat();
  assert.ok(text.includes('Laptop fleet'),'Authorized invoice missing from native chat');
  console.log('PASS: native LibreChat OIDC callback, expiry refresh, Primary-owned consent and chat through A2A/Finance');
}
shortTokenLifetime().then(async restore=>{try{await main();}finally{await restore();}}).catch(e=>{console.error('Native flow failed',stage,e instanceof assert.AssertionError?e.message:e.name);process.exitCode=1});
