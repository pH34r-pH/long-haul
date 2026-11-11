# Repository governance profile

This repository owns the reusable validator for the modern pH34r-pH project family. Each active repository declares one compact, machine-readable profile at `governance/repository-profile.json` and runs the validator from GitHub Actions.

The profile is an operational projection of the Long-Haul Resource Policy Profile. It does not replace repository-specific specifications, privacy notices, licenses, evidence packages, deployment policy, or legal review.

## Required dimensions

Every repository identifies:

- its repository profile and lifecycle status;
- source, task, acceptance, and deployment authority;
- default data classification and personal-data behavior;
- independent permissions for reading, indexing, search, model augmentation, evaluation, training, derivation, publication, distribution, retention, and deletion;
- independent acceptance and candidate/promotion behavior;
- exact-source, fingerprint, and provenance evidence expectations;
- the invariant that external text and model output cannot grant authority or approve themselves; and
- a prioritized stabilization register.

The validator deliberately rejects broad flags such as `ai_allowed`. A source may be searchable without being eligible for model augmentation, training, publication, or distribution.

## Repository profiles

- `research-source`: research programs, source corpora, hypotheses, and claims.
- `reproducibility-archive`: immutable experiment, proof, note, and publication artifacts.
- `product-data`: products that process user, device, personal, or operational data.
- `control-plane`: privileged infrastructure, identity, deployment, and fleet control.
- `prototype-device-protocol`: bounded prototypes, protocols, simulations, and hardware-facing systems.
- `publication-distribution`: outward-facing books, portfolio sites, social publishing, and event sites.
- `private-recovery-mirror`: one-way private recovery or archival mirrors that are not active authority.

## Enforcement boundary

A valid profile proves only that the repository has explicitly declared its governance posture. Runtime tools, Long-Haul WorkContracts, Cedar/MCP policy, deployment credentials, independent evaluators, and human approval remain the enforcement boundary.

Repositories call the validator by pinning the composite action to an exact Long-Haul commit:

```yaml
- uses: pH34r-pH/long-haul/.github/actions/repository-governance@<commit>
```

The pin must be updated through ordinary review when the shared profile changes.
