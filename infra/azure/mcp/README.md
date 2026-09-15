# Azure Functions MCP authentication spike

This directory tracks the first remotely reachable Long Haul MCP deployment (#31).

## Security boundary

The deployment deliberately separates three identities:

1. **Long Haul MCP Server app registration** — the Entra resource/API protected by Azure Functions EasyAuth. It exposes the delegated Long Haul MCP scope.
2. **ChatGPT Long Haul Client app registration** — an OAuth client authorized/preauthorized to request the MCP server scope. This client receives no Azure RBAC.
3. **Function App managed identity** — the workload identity used by the MCP implementation for downstream Azure provider operations. During the authentication spike it receives no reference-vessel RBAC.

The caller's OAuth token authenticates the caller to Long Haul. It is never passed through as an Azure management credential.

## Phase 1 acceptance gate

Before granting the Function managed identity any reference-vessel permissions:

- deploy the Flex Consumption Function App in `westus2`;
- require Entra authentication for the MCP endpoint;
- expose only harmless identity/capability inspection;
- connect ChatGPT using the dedicated client registration;
- complete OAuth interactively;
- confirm the Function sees an authenticated Entra principal;
- confirm anonymous access is rejected;
- confirm refresh/reconnect works.

Only after that gate passes should the Azure reference adapter receive narrowly scoped VM operator permissions.

## Manual Entra bootstrap values

The IaC intentionally does not create tenant-level app registrations. Create two single-tenant app registrations manually:

### Long Haul MCP Server

- expose one delegated API scope (recommended name: `longhaul.access`);
- record its application/client ID;
- use its Application ID URI as the MCP resource/audience.

### ChatGPT Long Haul Client

- configure the ChatGPT-provided OAuth callback URI once known;
- request the MCP server delegated scope plus the OIDC scopes required by the client;
- preauthorize/consent the client for the Long Haul MCP server scope where appropriate;
- do not assign this service principal Azure RBAC.

Store non-secret IDs/URIs as protected GitHub environment variables. If a client secret is required by the final ChatGPT OAuth configuration, store it only as an environment secret; prefer PKCE/public-client behavior if the integration supports it.
