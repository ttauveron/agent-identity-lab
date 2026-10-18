# Local development credentials and Keycloak mode

## Before starting

**This Compose stack is for a local lab on a trusted workstation only. Never reuse its static credentials, default secrets or Keycloak `start-dev` configuration in a shared environment.** This includes shared development servers, team demos, staging and production. Binding published ports to `127.0.0.1` does not make the credentials safe for other local users or processes.

For the local walkthrough, follow [the identity demo](identity-demo.md). Use the synthetic accounts and financial data supplied by the repository. Copy `.env.example` only if `.env` does not already exist, then supply your own `OPENROUTER_API_KEY` for interactive conversations. The OpenRouter key is a real secret; the supplied lab passwords and client secrets are public demo fixtures. Keep `.env` and `.local-certs/` out of Git, logs and model messages.

## Where the demo credentials come from

| Source | Local-only settings |
| --- | --- |
| [keycloak/realm.json](../keycloak/realm.json) | Static passwords for Alice and Bob, and OAuth client secrets for LibreChat, Primary Agent and Finance Agent. These are imported as literal values, not read from `.env`. |
| [.env.example](../.env.example) | Published example values for Keycloak administration, database passwords, OAuth client secrets, and LibreChat signing, session and credential-encryption settings. Only the OpenRouter API key is left empty. |
| [compose.yaml](../compose.yaml) | Fallback credentials when environment values are absent or empty, plus `start-dev --features=token-exchange-delegation,parameterized-scopes --import-realm` for Keycloak. |
| [integration tests](../tests/e2e_delegation.py) | Login and exchange checks using the repository's demo accounts; the walkthrough also passes the demo LibreChat client secret explicitly. |

Changing `FINANCE_CLIENT_SECRET`, `PRIMARY_CLIENT_SECRET` or `LIBRECHAT_CLIENT_SECRET` in `.env` changes what the services use; it does **not** update the corresponding client in Keycloak. Changing `.env` does not replace Alice's or Bob's imported passwords either. Values must match the configured realm for the demo to work. An existing Keycloak database also persists its configuration: [startup import skips realms that already exist](https://www.keycloak.org/server/importExport#_importing_a_realm_during_startup), so editing the import file or restarting Compose does not update an already imported realm. Database volumes likewise retain initialized credentials and data.

For this lab, retaining the consistent demo fixtures is intentional. It makes the consent and delegation tests repeatable; it does not make the credentials private or suitable for another environment.

## Lab limits

Keycloak runs in [development mode](https://www.keycloak.org/server/configuration), which is intended for development and testing. The stack also uses internal HTTP, a local self-signed certificate, static client and database credentials, and fixed LibreChat signing/encryption values. It is not a hardened identity deployment. Token validation and Finance API authorization still apply, but they do not protect against someone using known credentials or controlling a trusted service or host.

Do not publish this configuration through a tunnel, expose its ports to a shared network, or use it with real financial records. Changing only the administrator password, adding browser-facing TLS or copying the Compose file to another server does not remove these limits.

For a different context, design and validate a separate deployment: unique identities and credentials provisioned outside source control, secret rotation and storage, a configured Keycloak production startup rather than `start-dev`, protected service transports and databases, and reviewed network and access policies. Keep those credentials separate from the demo realm and fixtures. These are requirements to assess, not a deployment supported or validated by this repository. See [the threat model](threat-model.md) for the remaining trust assumptions and controls the lab does not demonstrate.
