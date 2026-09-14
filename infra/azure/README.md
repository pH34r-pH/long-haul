# Azure generic-x86 reference vessel

This is a deliberately small, CPU-only, resource-group-scoped reference host for the
llama.cpp L0–L10 trace. It is not a fleet substitute, a GPU benchmark rig, or a
control-plane host.

## Default topology and cost guardrail

`Standard_B2as_v2` provides 2 x86 vCPUs and 8 GiB RAM, sufficient for a tiny 135M
GGUF and a conservative llama.cpp build. The template creates one VNet/subnet/NSG,
one VM/NIC, one static Standard public IP for **outbound** access, and a 64 GiB
Standard SSD LRS OS disk. The NSG has no inbound allow rule, including no SSH rule.

The public IP is retained solely so a newly provisioned VM has predictable outbound
access without adding a NAT Gateway. It does not make a management service reachable.

As checked against the Microsoft Retail Prices API on 2026-09-14 for `eastus`, the
default Linux VM is $0.0752/hour, the E6 LRS 64 GiB Standard SSD is $4.80/month, and
a Standard static IPv4 address is $0.005/hour: about **$63.35/month** if left on for
730 hours. This excludes outbound internet data transfer and is an estimate, not a
subscription budget cap. The protected workflow queries the same official API for
the selected region immediately before both what-if and apply, and refuses a VM above
$0.10/hour or a default stack above $80/month.

Microsoft documents that the Retail Prices API is unauthenticated and returns retail
USD prices; see [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices).

## Deployment boundary

The one-time resource group, OIDC federation, and RBAC bootstrap are external
prerequisites. This stack never creates or changes them. The deployment workflow
uses only `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and
`AZURE_RESOURCE_GROUP` configured on the protected `azure-reference` environment.
It verifies the subscription and resource-group target and deploys only at resource
group scope. No secret-based Azure login is used.

Before apply, dispatch **Azure reference vessel** on `main` with `mode=what-if`,
review its price estimate and change set, then separately dispatch `mode=apply`.

## Provisioned software

cloud-init installs public build dependencies, checks out the pinned Long Haul and
llama.cpp commits, builds CPU-only `llama-cli`, downloads the small GGUF, verifies its
SHA-256, and writes `/opt/long-haul/run-reference`. It contains no credentials.

Run that script through a deliberately authenticated management path (for the first
reference run, `az vm run-command invoke` from the protected deployment environment)
and retain the resulting `/opt/long-haul/runs/<timestamp>/reference-report.json`.
The model URL is accompanied by a mandatory content hash; update both together after
reviewing a new artifact.
