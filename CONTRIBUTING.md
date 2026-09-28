# Contributing to Long Haul

Long Haul welcomes focused changes that strengthen the portable runtime, its evidence model, or its experiments without crossing the private Fleet boundary.

## Before changing code

1. Read `docs/architecture.md` and `docs/repository-boundary.md`.
2. Identify the contract or invariant your change touches.
3. Prefer extending an existing adapter, schema, or execution pattern over introducing a parallel abstraction.
4. Keep concrete private vessel state and credentials out of this repository.

## Pull requests

Explain the problem, the invariant/boundary affected, and the evidence that validates the change. Add or update tests for new behavior. Hardware claims require measured evidence; simulated fixtures must remain labeled as such.

For research changes, distinguish a protocol or hypothesis from an observed result. Do not rewrite unsuccessful or dissenting evidence out of the history.

## Security

Do not include credentials, private network details, or exploitable vulnerability details in ordinary issues. Follow `SECURITY.md`.
