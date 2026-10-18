## Project

This repository is an educational PoC demonstrating secure AI agent delegation using OAuth 2.0 Token Exchange (RFC 8693), Keycloak, and A2A.

Read `docs/architecture.md` for design decisions and `README.md` for setup and test instructions.

## Architecture

- LibreChat: user interface, not an authorization authority.
- Primary Agent: orchestration and authenticated A2A communication.
- Finance Agent: independent service, OAuth identity, token exchange, and finance tools.
- Finance API: final authorization enforcement using user permissions, agent capabilities, and resource policies.
- Keycloak: identities, delegation, and token issuance.

Preserve independent service boundaries and the existing delegation model.

## Security invariants

- Never trust identities, roles, or permissions supplied by the LLM.
- Never expose tokens, credentials, or secrets to the LLM or logs.
- Validate JWT signatures, issuer, audience, expiration, and relevant delegation claims.
- Authorization must be enforced by the Finance API, never by agent instructions alone.
- The Primary Agent must support different authorized OAuth clients, not just LibreChat.
- Do not pin service-account subject UUIDs.
- Maintain user isolation across conversations and A2A sessions.
- Keep demo credentials clearly separated from real secrets.

## Development

- Prefer simple, idiomatic Python and existing libraries.
- Avoid unnecessary abstractions, frameworks, or new infrastructure.
- Do not introduce authorization gateways, OPA, or additional protocols without a concrete requirement.
- Keep changes focused and update documentation when behavior changes.

## Validation

- Run relevant tests following `README.md`.
- Add positive and negative tests for changes affecting identity, delegation, or authorization.
- For OAuth changes, verify the real Keycloak exchange when possible.
- Report what was tested and what remains unverified.

## Conventions

- Documentation is written in english (./docs/)
