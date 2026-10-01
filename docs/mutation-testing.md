# Mutation-guided testing

Long Haul uses language-native mutation tooling and the Stryker / Mutation Testing Elements
`mutation-testing-report-schema` as the machine-readable interchange contract.

## Python engine

The first Python engine is `irradiate==0.4.3`. It writes schema-v2 Mutation Testing Elements JSON
directly, so Long Haul does not maintain a mutmut-to-Stryker adapter or a repository-specific
mutation result format.

Install the optional tooling with:

```sh
python -m pip install -e '.[dev,mutation]'
```

The initial bounded target is the deterministic scheduler:

```sh
irradiate run src/long_haul/scheduler/core.py \
  --report json --output mutation-report.json \
  --verify-survivors
```

The pilot records evidence without a score threshold. A surviving mutant is a prompt to inspect a
behavioral distinction, not an instruction to manufacture a test. Equivalent/noisy mutants should
be recorded rather than chased.

When generation is useful, prefer existing generators or property-testing libraries and independently
re-run mutation testing before accepting generated tests. The Experiment Compiler pilot demonstrated
why: generic generated tests can pass without killing any additional mutants, while a small
survivor-directed test can add real behavioral discrimination.
