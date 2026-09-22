# Azure CPU reference vessel

This is a small, reproducible generic-x86 host for Long Haul's llama.cpp L0–L10
reference trace. It is not a GPU benchmark rig, fleet-control-plane host, or
replacement for Anchorage/Kestrel.

## Costed baseline

The only permitted SKU is `Standard_B1ms`: 1 vCPU and 2 GiB RAM. cloud-init enables
2 GiB of ordinary swap and builds llama.cpp with one job, which is slow but sufficient
for the 135M-class Q4 reference model and avoids paying for permanent build headroom.
The VM uses a 32 GiB Standard HDD OS disk and one Standard static public IPv4 address.
Azure requires an explicit outbound path for new VM networking; associating the
least-cost Standard IP to the NIC is materially cheaper than NAT Gateway. The NSG has
no inbound allow rules, so this is not an SSH management path.

Microsoft Retail Prices API data checked on 2026-09-15 for `eastus`:

| Component | Retail price | 730-hour estimate |
| --- | ---: | ---: |
| Linux B1ms | $0.0207/hour | $15.11/month |
| 32 GiB Standard HDD (S4 LRS) | $1.536/month | $1.54/month |
| Standard static IPv4 | $0.005/hour | $3.65/month |
| **Total** |  | **$20.30/month** |

This excludes internet data egress and is not a subscription budget cap. The protected
workflow calls the same official API for the selected region before what-if or apply;
it fails when any required price is absent, VM price exceeds $0.03/hour, or total
exceeds $30/month. See [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices).

## Security and deployment boundary

The resource group, GitHub OIDC federation, and RBAC are owner-managed external
prerequisites. This repository never creates or changes them. The VM's system identity
is intentionally unprivileged. The protected manual workflow uses only environment
variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and
`AZURE_RESOURCE_GROUP`; it requires no client secret and deploys only at resource-group
scope after verifying the configured target. The Bicep entrypoint also has a
deployment-time assertion that `resourceGroup().name` equals
`expectedResourceGroupName`, so a direct or mis-targeted invocation fails rather than
creating the stack elsewhere. It neither enumerates nor references any other resource
group. The adjacent `bicepconfig.json` explicitly enables Bicep's Assertions feature;
CI verifies the resulting ARM `asserts.targetResourceGroupIsExpected` expression, so the
guard cannot degrade into an informational output.

GitHub Environment approval occurs before a job begins. The deliberate operator sequence
is: dispatch `Azure reference deploy` on trusted `main` with `mode=what-if`, approve the
`azure-reference` environment, inspect its cost and Azure change preview, then separately
dispatch `mode=apply` and approve the environment again. The apply run performs a fresh
what-if before it creates anything. This PR does not deploy anything.

cloud-init contains only public URLs, pinned revisions, and a GGUF checksum. It writes
`/opt/long-haul/run-reference`; invoke that later through an authenticated management
path such as protected `az vm run-command invoke` and retain its output directory.

## Pinned Pi qualification

Long Haul's Pi integration is pinned to upstream `@earendil-works/pi-coding-agent` 0.87.0 at commit `e40126f578ccc0a4a21f8d30cd8e96c8ecfe1722`. The protected `pi-upstream-qualification` reference operation prepares that exact source revision under `/opt/long-haul/pi-qualification` and verifies its package version without modifying system Python, llama.cpp, or the model. Node/Pi execution is intentionally a separate qualification slice so the runtime installation can be isolated and rollbackable rather than added implicitly to cloud-init.
