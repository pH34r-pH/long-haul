# Fleet boundary

Long Haul and Long Haul Fleet are intentionally separate repositories.

## Public Long Haul owns

- provider-neutral runtime code and interfaces;
- topology/capability schemas;
- scheduler semantics;
- work contracts and evidence models;
- fixture-backed discovery and tests;
- requirements contracts and public documentation.

## Private Fleet owns

- concrete physical and cloud vessel definitions;
- privileged deployment state;
- Azure infrastructure and identities;
- private networking and RBAC;
- self-hosted runner configuration;
- protected release promotion and rollback;
- hardware qualification evidence that should not be public.

Fleet consumes exact immutable public revisions rather than maintaining a divergent copy of Long Haul.

## Why the boundary exists

A public pull request should be able to exercise the runtime without automatically acquiring credentials or physical-fleet access. Conversely, operational changes to private infrastructure should not redefine portable runtime semantics.

Read [docs/repository-boundary.md](https://github.com/pH34r-pH/long-haul/blob/main/docs/repository-boundary.md) and [docs/fleet-source-contract.md](https://github.com/pH34r-pH/long-haul/blob/main/docs/fleet-source-contract.md).
