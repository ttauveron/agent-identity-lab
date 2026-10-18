# Reproducible identity and authorization demo

This walkthrough demonstrates user consent, delegated access, user and agent permission denials, and field filtering through LibreChat → Primary Agent → A2A → Finance Agent → Keycloak → Finance API. Run commands from the repository root. Use only the repository's local demo accounts and synthetic data.

## Prepare the lab

**Local lab only:** the static credentials in `keycloak/realm.json`, the defaults in Compose and `.env.example`, and Keycloak `start-dev` must never be reused in a shared environment. Read [the local credential and development-mode limits](local-development.md) before starting.

You need Docker Compose, network access to download images, and an OpenRouter API key for the interactive conversation. Ports 3080, 8080 and 8443 must be free: stop any existing lab using them before starting this dedicated Compose project. The `agent-identity-demo` project has separate database and session volumes; use it consistently in every command below.

If `.env` does not exist, copy `.env.example` to `.env`. Set `OPENROUTER_API_KEY` there and retain the default demo secrets, matching the imported Keycloak realm. Never paste keys or tokens into a chat. Existing `.env` files should be edited rather than overwritten.

```sh
./keycloak/create-local-cert.sh
docker compose -p agent-identity-demo up -d --build
```

Trust `.local-certs/cert.pem` in your browser for `https://keycloak.localhost:8443`. Wait for `keycloak-declarations` to exit with code 0. Confirm that the identity endpoint and application services are ready:

```sh
docker compose -p agent-identity-demo ps -a
curl --fail --silent --show-error --cacert .local-certs/cert.pem \
  --resolve keycloak.localhost:8443:127.0.0.1 \
  https://keycloak.localhost:8443/realms/lab/.well-known/openid-configuration
for service in primary-agent finance-agent finance-api; do
  docker compose -p agent-identity-demo exec -T "$service" python -c \
    'import urllib.request; urllib.request.urlopen("http://localhost:8000/healthz", timeout=2)'
done
```

Each readiness command must succeed; retry after a short wait if a service is still starting. The seed data comes from [db.sql](../db.sql): invoice 1 is Finance's “Laptop fleet”, amount 1200; invoice 2 is HR's “Recruiting”, amount 500. Payments 1 and 2 belong to those departments and start as `pending`. If this project has been used before, stored changes remain: this guide assumes the unmodified seed data. These steps do not reset or delete existing volumes.

## Run the conversation demo

1. Open `http://localhost:3080` in a fresh private browser session. Sign in as **alice / alice-lab**. This login authenticates Alice to the Primary Agent.
2. Select **Agent Lab**, then **finance-lab**. Start a new conversation for each table row. On the first request, Primary returns a Keycloak link: open it, approve delegation to Finance with the same account, then repeat the prompt in the same conversation. This consent authorizes delegation, not every financial operation.
3. Run Alice's prompts below, then close her private session. Open a separate fresh private session, sign in as **bob / bob-lab**, select the same endpoint/model and run Bob's prompts, completing his own delegation consent. Confirm the signed-in account before each group. Expiry or a Primary restart requires new consent.

| User | Prompt | Expected observation | Enforced rule |
| --- | --- | --- | --- |
| Alice | `Read invoice 1. Give its ID, department, description and amount.` | ALLOW: ID 1, department `finance`, description `Laptop fleet`, amount 1200. | Both user and agent may read invoices, and the resource belongs to Alice's department. |
| Alice | `Read invoice 1 and include its bank_account field. Use only the fields returned by the tool.` | The invoice is readable, but no bank account value is returned. The response should say the field is unavailable. | The API omits `bank_account` from its response even for an authorized read. |
| Alice | `Read invoice 2.` | DENY; no HR invoice content should be returned. | Alice has invoice-read permission, but cannot read another department's resource. |
| Alice | `Approve payment 1.` | DENY; payment 1 remains `pending`. | Alice has `payments.approve`, but Finance Agent does not: the delegated scope/capability ceiling cannot authorize approval. |
| Bob | `Read invoice 2.` | DENY, even though the invoice belongs to Bob's HR department. | Bob lacks `invoices.read`; Finance Agent's permission does not supply the missing user permission. |
| Bob | `Read payment 2. Give its ID, department, amount and status.` | ALLOW: ID 2, department `hr`, amount 500, status `pending`. | Bob and Finance Agent may read payments, and the department matches. |

Natural-language wording is not fixed. The Finance tool reports denials as `DENY: finance policy`, but the LLM may paraphrase them. A chat refusal alone does not prove that the requested tool ran; a fabricated bank account is also not evidence that the API exposed a field. Use the deterministic checks below for exact assertions. Do not accept a successful approval or another user's data as normal wording variation.

As the lab operator, verify that the denied approval did not change stored state:

```sh
docker compose -p agent-identity-demo exec -T finance-db \
  psql -U finance -d finance -c 'SELECT id, status FROM payments ORDER BY id;'
```

Expected: both payments remain `pending`. This operator database access is a verification step, not an authorization path available to the LLM.

## Verify exact outcomes without an LLM

The existing [chain test](../tests/e2e_chain.py) performs real Alice/Bob login and consent, Primary HTTP calls, authenticated A2A, Keycloak exchange and Finance API calls. A [test-only override](../tests/compose.e2e.yaml) replaces tool selection with a deterministic driver; it does not test LibreChat or LLM behavior.

Stop this demo's services before switching modes. The override is for tests only and must not remain active for the interactive walkthrough. A dummy key is sufficient here:

```sh
docker compose -p agent-identity-demo stop
OPENROUTER_API_KEY=test-placeholder docker compose -p agent-identity-demo \
  -f compose.yaml -f tests/compose.e2e.yaml up -d --build keycloak-tls primary-agent
```

Repeat the readiness checks above before running both scripts:

```sh
OPENROUTER_API_KEY=test-placeholder docker compose -p agent-identity-demo \
  -f compose.yaml -f tests/compose.e2e.yaml run --rm --no-deps \
  -v "$PWD":/src:ro -w /src -e PYTHONPATH=/src \
  -e LIBRECHAT_CLIENT_SECRET=lab-librechat-secret \
  finance-agent python tests/e2e_delegation.py
OPENROUTER_API_KEY=test-placeholder docker compose -p agent-identity-demo \
  -f compose.yaml -f tests/compose.e2e.yaml run --rm --no-deps \
  -v "$PWD":/src:ro -w /src -e PYTHONPATH=/src \
  -e LIBRECHAT_CLIENT_SECRET=lab-librechat-secret \
  finance-agent python tests/e2e_chain.py
```

Expected exit code: 0 for each script, with these final messages:

```text
Live Keycloak delegation and Finance API checks passed
Live Primary → A2A → Keycloak → Finance API checks passed (ALLOW and DENY)
```

The direct test asserts an authorized API read (HTTP 200) with no `bank_account`, department and approval denials (403), and rejection of user-only and actor-only tokens. The chain test asserts Alice's authorized invoice read and omitted field, plus exact `DENY: finance policy` results for Alice's department/agent denials and Bob's invoice denial. Bob's allowed payment-2 read is an interactive case; these scripts do not assert it. No token needs to be copied or displayed.

To return to the interactive mode, restore the normal agent commands and restart LibreChat using the real key in `.env`:

```sh
docker compose -p agent-identity-demo up -d --build --force-recreate \
  primary-agent finance-agent librechat
```

## Compare failures and finish

Missing consent, login failure or an unavailable service is a setup failure, not a policy DENY. Check the certificate trust, `keycloak-declarations` exit status, demo realm/secret consistency and readiness checks. If only conversations fail, check the OpenRouter key and whether the test override is still active. If an expected authorization result differs, run the deterministic tests before attributing it to LLM wording; do not weaken policy to make the demo pass.

Finish with `docker compose -p agent-identity-demo stop`. This preserves the demo's data and sessions.

## Lab limits

The static realm credentials, Compose fallback secrets and Keycloak `start-dev` are for a trusted local workstation only; never reuse them in a shared environment. Changing `.env` alone does not update imported Keycloak client secrets or make this a hardened deployment. See [local development limits](local-development.md). Delegation and API policy checks do not establish fresh human intent for each tool call, prevent bearer-token replay, or secure internal HTTP transport.
