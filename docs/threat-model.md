# Lab threat model

This local educational PoC demonstrates authenticated delegation and resource authorization. It does not provide a production security baseline. Its design is described in [architecture.md](architecture.md); the deployed boundaries are defined by [compose.yaml](../compose.yaml).

## Assets, actors and assumptions

Assets include user and agent identities, bearer tokens, OAuth client secrets, Keycloak signing keys, financial records and permissions, conversation histories, and audit/trace data. The OpenRouter API key and local TLS private key are also credentials to protect.

Actors include Alice and Bob, authorized OAuth clients such as LibreChat, the Primary Agent, the Finance Agent, Keycloak, the Finance API, and the external LLM provider. Threats include unauthenticated callers, users attempting to cross department or session boundaries, malicious prompts/tool content, token thieves, and compromised containers.

The lab assumes a trusted workstation operator, Docker daemon, service implementations, Keycloak configuration and authorization database. It uses synthetic financial data and static, published demo credentials. A host administrator or attacker controlling Keycloak or the Finance API can bypass the demonstrated controls. Loopback ports do not protect against other local processes or users.

## Trust boundaries

| Boundary | Controls and remaining exposure |
| --- | --- |
| Browser / OAuth client → Primary Agent | LibreChat handles OIDC; the Primary independently validates the upstream JWT. It obtains an agent-only JWT through Keycloak device authorization and requires the approved user to match the chat user. The UI and prompt are not authorization authorities. LibreChat is served over local HTTP on port 3080. |
| Primary → Finance Agent | A2A carries only the Finance-only user bearer token and a separate Primary client token. Finance rejects shared audiences; the Finance-visible user token cannot authenticate at Primary. Finance validates both for its audience and requires the caller's `azp` to identify `primary-agent`. This hop uses HTTP. |
| Finance Agent → Keycloak → Finance API | Keycloak exchanges subject and actor tokens. Finance checks the returned subject and actor; the API validates the delegated JWT and enforces business policy. Token exchange, JWKS retrieval and API calls use internal HTTP. |
| Services → networks and storage | Compose separates direct connectivity, not authorization. Finance API shares networks with Finance Agent, finance-db, Keycloak and the telemetry collector. Multi-network peers are trusted infrastructure. Session volumes and databases are accessible to their services and the host. |
| Agents → external LLM / telemetry | Prompts, conversation history and authorized tool results can reach OpenRouter and its selected model provider. Tokens are handled through request context and HTTP headers, outside model messages. Local traces and audit logs are separate data assets, not tamper-proof evidence. |

## What the lab demonstrates

[JWT verification](../services/common/security.py) checks RS256 signatures against Keycloak's JWKS, issuer, expected audience and expiration, and requires `sub`. Identity comes from signed claims, never from model output.

Keycloak uses user consent for `delegation:client:finance-agent`, a `may_act` claim and the configured delegation permission to authorize token exchange. The issued token retains the human `sub` and records the Finance Agent in `act`.

The [Finance API](../services/finance_api/app.py) requires a delegated `act` naming `finance-agent` with an actor subject, a human username and the operation's scope before accessing the database. It then checks the username/subject pair against employee data and intersects token scope, user permission, agent permission and resource department. User-only or agent-only tokens cannot authorize financial operations. The API omits `bank_account`; payment approval is denied by the agent's capability ceiling.

Primary and Finance sessions are keyed by the verified user subject and conversation context. A stolen conversation ID alone does not grant another user's history. A2A task ownership also uses the verified subject; the endpoint accepts only `SendMessage`. This isolation assumes uncompromised services and storage.

Evidence: [signed-JWT policy tests](../tests/test_acceptance.py), [A2A tests](../tests/test_a2a_transport.py), [Primary session tests](../tests/test_primary_chat.py), and live [Keycloak delegation](../tests/e2e_delegation.py) / [chain integration](../tests/e2e_chain.py). The [native LibreChat integration](../tests/e2e_librechat_ui.cjs) checks login, expiry refresh and the consent conversation. [Destination integration](../tests/e2e_destinations.py) covers another agent, refusal and wrong-user approval; [token-boundary tests](../tests/test_token_boundaries.py) verify claims and grant isolation. The full chain checks replay of the actual Finance-visible token at Primary. Simulated policy tests do not prove real consent or exchange; live integration covers the configured demo realm, not arbitrary deployments.

## Limits and measures for another context

| Threat or limitation | Current lab behavior | Measures to evaluate before broader use |
| --- | --- | --- |
| Bearer-token replay | A valid stolen token can be reused at its intended service until expiration. The Finance-only token is rejected at Primary, but this is not sender binding. A stolen delegated token can call the API without proving possession of the Finance Agent's credentials. Signed `act` identifies the actor at issuance, not the current token holder. There is no replay cache or sender constraint; logout is not an immediate revocation check in offline JWT validation. | TLS throughout, shorter token lifetimes, an explicit revocation strategy, and sender-constrained tokens such as mTLS or DPoP where supported. Assess idempotency separately for repeated writes. |
| Internal interception or impersonation | A2A, finance calls, token endpoints, JWKS, database and telemetry traffic are not protected by end-to-end TLS in Compose. Browser-facing Keycloak TLS terminates at Caddy; its upstream is HTTP. | Authenticated TLS for each hop, protected signing-key discovery, restricted service connectivity and secure certificate management. |
| Compromised services or prompt injection | Model instructions cannot prevent misuse of an authorized operation or disclosure of already available data. Service compromise can expose tokens, secrets and stored history. Policy changes do not erase old conversation results. | Minimize tool capabilities, validate business inputs, consider confirmation for sensitive writes, restrict outbound destinations, and define history retention and permission-change handling. |
| Data, credentials and operations | Demo secrets, Keycloak `start-dev`, unencrypted session files and limited audit records are local conveniences. The lab provides no high availability, comprehensive rate limiting, or verified log redaction across every dependency. | Managed secrets and rotation, hardened identity deployment, storage protection, privacy review of LLM data flows, tested redaction, protected audit collection, and availability controls. |
| Keycloak feature maturity | Compose pins Keycloak 26.8.0 and enables `token-exchange-delegation` and `parameterized-scopes`. Client delegation is a [Preview feature](https://www.keycloak.org/securing-apps/token-exchange); upgrades may change its behavior. | Review upstream support status and changes, test consent, `may_act`/`act`, audiences, scopes and denial paths against the exact proposed version. |

Successful delegation proves that the configured issuer authorized an actor to act for a subject. Successful API authorization proves that the requested operation passed the configured resource policy. Neither establishes fresh human intent for every tool call, prevents bearer replay, or guarantees confidentiality and integrity after a trusted component is compromised.

Primary initiates device authorization for an operator-configured destination, outside LLM execution. Device codes are credentials and remain in its bounded in-memory store; only the user verification link is returned to the UI. Completing a link as another user does not authorize the chat: Primary verifies the signed subject against the independently authenticated caller. Pending and authorized grants are isolated by user, conversation and destination. Lost state or token expiration requires new consent; refresh tokens are discarded. Device authorization does not prove fresh intent for each tool call or provide immediate revocation of an already issued access token. The prototype is single-process and does not migrate previous Primary conversation session keys.
