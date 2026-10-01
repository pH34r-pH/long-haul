# Documentation map

- [`architecture.md`](architecture.md) is the current MVP architecture and invariants.
- [`north-star.md`](north-star.md) is explicitly design direction; it is not proof that every component exists.
- [`repository-boundary.md`](repository-boundary.md) and [`fleet-source-contract.md`](fleet-source-contract.md) define the public/private handoff.
- [`experiments.md`](experiments.md) and hardware-validation documents distinguish hypotheses, measured results, fixtures, and qualification claims.
- [`wiki/`](wiki/) is the canonical source copied to the GitHub Wiki by CI; keep it navigational and link back to executable contracts.

Do not erase unsuccessful, dissenting, or immutable scientific/qualification history. When a plan is superseded, link the actual replacement only when the repository or issue history proves the relationship. For a documentation-only change, run `git diff --check` and check every changed relative link from the file containing it.
