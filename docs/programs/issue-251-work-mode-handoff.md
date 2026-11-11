# Work Mode handoff — complete issue #251

**Program:** [Long-Haul #251 — Modern repository alignment program](https://github.com/pH34r-pH/long-haul/issues/251)  
**Operational control:** [Long-Haul #252 — Work Mode execution control](https://github.com/pH34r-pH/long-haul/issues/252)  
**Status at handoff:** governance alignment merged across all modern repositories; substantive P0/P1 implementation remains.  
**Development posture:** general development pause; #251 is the default engineering priority.

## Work Mode startup instruction

Use the following as the governing instruction for the Work Mode session:

> Complete Long-Haul #251 end to end under the development pause. Start from #252 and the checked-in execution state. Reconcile every child issue against current source before implementing it. Work topologically through shared platform primitives, research/evidence repositories, private products, publication systems, and protocol/hardware systems. Implement P0 behavior with executable evidence; implement P1 wherever technically possible. Open focused PRs, iterate until checks pass, merge when permitted, close child issues with exact evidence, and update #252 plus the execution-state file after every material transition. Preserve all existing privacy, security, physical-safety, human-publication, and protected-deployment gates. Do not begin unrelated work or create a replacement platform when existing Long-Haul, Fleet, Experiment Compiler, repository-native CI, or maintained standards can satisfy the requirement.

## Mission outcome

The handoff is complete only when the project family has progressed from **declared governance alignment** to **demonstrated stabilization**:

- every P0 item is implemented and executable;
- every P1 item is implemented or has a narrow evidence-backed disposition;
- source, scientific, package, device, deployment, and publication states remain separate;
- personal and sensitive repositories enforce retention, deletion, and derived-data handling;
- research and corpus repositories retain rights, use-policy, partition, and contamination evidence;
- product and control-plane repositories eliminate ambiguous privileged paths and retain promotion/recovery receipts;
- all child issues contain closeout evidence;
- #241 and #251 can close without relying on model self-report or prose-only compliance.

## Current known-good baseline

The cross-repository governance baseline is already merged. Every active modern repository has:

- `governance/repository-profile.json`;
- a workflow pinning the common validator to Long-Haul commit `28b9afbd17faa2baea9f2fb302abe7db71ee9882`;
- explicit authority, privacy, resource-use, evidence, promotion, and stabilization declarations.

Do not repeat this alignment work. Consume it as policy and use each repository’s stabilization issue as the starting contract.

`hermes-lite` is retired. Pi is the only supported first-party agent harness. Nacre Recall is Pi-native and subordinate to Long-Haul identity, mission, policy, checkpoint, candidate, and evaluator state.

## Development-pause contract

The pause exists to maximize throughput and eliminate conflicting changes.

### Allowed

- work directly required by #251, #252, or a linked stabilization issue;
- fixes to CI, dependencies, infrastructure, or repository health that block this program;
- urgent security, data-loss, or outage response;
- exact supporting refactors required to establish the issue’s acceptance evidence;
- test, fixture, documentation, migration, and evidence work needed for closure.

### Paused

- unrelated feature work;
- opportunistic product expansion;
- new research branches not required by a linked issue;
- broad aesthetic or architectural rewrites;
- autonomous production publication or deployment;
- training experiments not admitted by the shared resource-policy and corpus-governance gates;
- work on retired repositories.

### Exception receipt

Record any pause exception in #252 with:

1. why it could not wait;
2. affected repository and exact source state;
3. PR or operational action;
4. validation and evidence;
5. effect on the #251 critical path.

## Work Mode operating rules

### Reconcile before implementation

For each repository:

1. Fetch the stabilization issue and every materially related issue or PR.
2. Inspect the current default branch, governance profile, CI, architecture, tests, workflows, and current dependencies.
3. Search for partial or already-complete implementation; do not duplicate it.
4. Compare issue assumptions with current reality and update the issue where stale.
5. Record the baseline commit in the implementation PR.
6. Identify the smallest complete vertical slice that proves the required behavior.

### Prefer existing mechanisms

Before writing custom infrastructure, prefer:

- repository-native language and test frameworks;
- GitHub reusable workflows, environments, OIDC, attestations, and SARIF;
- Long-Haul `WorkContract`, checkpoints, evidence, resource policy, and execution fabric;
- Experiment Compiler package/attempt/finalization semantics;
- Fleet protected promotion and recovery paths;
- Specified Development schemas and validators once qualified;
- established formats such as ODRL, PROV-O, DCAT, DQV, DPV, Croissant, SPDX, in-toto, SARIF, GEDCOM/GLX, Lean, LSP, Tree-sitter, SCIP, and platform-native accessibility/UI trees.

Create new durable infrastructure only after documenting why maintained existing mechanisms cannot meet the acceptance criteria.

### PR discipline

- Keep one active implementation PR per repository unless disjoint work is demonstrably independent.
- One PR should represent one independently reviewable acceptance unit.
- Avoid cross-repository mega-PRs.
- Include baseline commit, issue gap, changed authority/evidence boundary, commands, results, negative fixtures, and residual limitations.
- Repair quality and test failures instead of suppressing checks without evidence.
- Merge only when repository checks and relevant integration checks pass.

### Closure discipline

A child issue is not complete because its policy or design is documented. Close only after the required behavior is executable.

Every closeout comment must include:

- PR and merge commit;
- exact source, environment, dependency, evaluator, model, device, or browser identities that matter;
- commands and checks run;
- negative, adversarial, failure, and recovery cases;
- evidence/receipt/artifact locations and fingerprints;
- deployment, publication, physical-device, or independent-review status;
- remaining external gate and exact operator procedure, if any;
- confirmation that unrelated feature scope was not added;
- link to #252.

## Concurrency model

Use at most **four active repository lanes** and at most **one active PR per repository**.

Recommended lanes after Phase 1 foundations are stable:

- **Lane A — platform/evidence:** Long-Haul, Experiment Compiler, Compiled Experiments, Fleet, Specified Development.
- **Lane B — research/corpora:** DSL, Knowledge, Tone, Theorem Library, Research Notes, Discursive-Affective Epistemology.
- **Lane C — product/privacy:** Nacre, Personal Algorithm, Family Tree, Jam Day, Wardriver, Society.
- **Lane D — publication/protocol/physical:** Portfolio, Book, Socials, ARCC, Blackbox, Longband, Home Bot.

Do not parallelize two changes that modify a shared schema, evidence contract, deployment surface, or candidate package until the first contract is merged and pinned.

## Topological execution plan

### Phase 0 — Program bootstrap

1. Fetch #251 and #252.
2. Read `programs/issue-251/execution-state.json`.
3. Reconcile all linked child issues and open PRs.
4. Record current default-branch commits and CI status in the execution state.
5. Confirm no unrelated development branch is being merged during the pause.
6. Post the initial active-lane checkpoint to #252.

### Phase 1 — Shared platform gate

#### 1A. Long-Haul repository-work foundation

Order:

1. #242 — repository work extension and reproducible context manifest.
2. #243 — public/sealed evaluator and exact-candidate evidence.
3. #244 — Pi harness adapter and privacy-preserving action trajectories.
4. #245 — adversarial repository security profile.
5. #64 P0 — deterministic hermetic repository fixture.
6. #64 P1 — one real Pi repository task with frozen candidate and independent evaluation.

Gate: no downstream repository may claim code-agent or trajectory qualification before this vertical slice is accepted.

#### 1B. Experiment lifecycle and deployment primitives

May proceed in parallel after the relevant contract shapes are stable:

- Experiment Compiler #150 — reusable compile/verify/execute/finalize/publication components and repository-agent profiles.
- Long-Haul Fleet #1612 — reusable execution, promotion, recovery, monitoring, and common receipt semantics.
- Specified Development #13 — minimal executable bundle validator and compact human review surface.
- Compiled Experiments #7 — independent clean rebuild and release verification after Compiler support is available.

Phase 1 exit:

- repository work has exact task, context, candidate, evaluator, trajectory, and sandbox identities;
- the first real task is reproducibly evaluated;
- Compiler/Fleet/Specified Development expose reusable rather than repository-specific primitives;
- release packages can be independently verified.

### Phase 2 — Research, claims, proofs, and corpora

Execute with shared profile and evidence primitives:

1. Domain Scaling Lab #842.
2. Knowledge #42.
3. Tone #18.
4. Theorem Library #20.
5. Research Notes #31.
6. Discursive-Affective Epistemology #37.

Critical rules:

- freeze exact source and use policy;
- keep external text untrusted;
- freeze train/development/test ancestry before fitting;
- preserve negative, conflicting, stale, superseded, and retracted states;
- do not let model judges become sole ground truth;
- connect claims and publications to exact Compiler/evaluator evidence.

Phase 2 exit:

- admitted sources carry exact policy and provenance;
- training and publication eligibility are machine-auditable;
- theorem/proof and empirical applicability remain distinct;
- articles and claims resolve to exact evidence or are explicitly unverified;
- documentation-only research cannot imply implemented or deployed capability.

### Phase 3 — Private products and personal data

Recommended order:

1. Nacre Recall #24 — source admission and deterministic memory scorecard.
2. The Personal Algorithm #92 — source trust, replay, immutable policy candidates.
3. Family Tree #44 — privacy policy, validation, reversible merges/exports.
4. Jam Day #25 — disposable RSVP E2E, retention/deletion, timezone contract.
5. Blue Swallow Wardriver #548 — workflow split, replayable scan/outbox state, device evidence.
6. Blue Swallow Society #131 — private cutover, auth/authz E2E, migration/live probes.

Dependencies:

- Society must consume Fleet promotion/recovery semantics and Wardriver-owned private deployment authority.
- Wardriver physical evidence must remain separate from emulator/source evidence.
- Nacre and Personal Algorithm must consume shared resource policy instead of local broad flags.
- Family Tree and Jam Day must propagate deletion to derived indexes/exports without preserving deleted personal payloads.

Phase 3 exit:

- personal and sensitive records have enforceable purpose, retention, deletion, and derivation behavior;
- state machines have replay and negative fixtures;
- authentication and authorization are tested end to end;
- device/emulator/source/deployment states remain separate;
- learned or generated policy/memory candidates cannot self-promote.

### Phase 4 — Publication, distribution, and recovery

1. Portfolio #141.
2. Book Development #5.
3. Socials #19.
4. Agent-Readable Calling Card #17.
5. Blackbox #6.

Rules:

- successful build is not publication authority;
- every outward factual claim resolves to an exact approved source revision;
- private source paths and identities remain excluded from public candidates;
- account, professional/activist identity, and credential contexts remain isolated;
- recipes, redirects, and transformations receive adversarial fixtures and exact manifests;
- Blackbox remains a one-way recovery mirror and cannot write back to Notion automatically.

Phase 4 exit:

- one machine-readable contract governs each publication surface;
- human/operator promotion remains explicit;
- release candidates carry source, rights, transformation, browser/build, and review evidence;
- no public artifact silently uses training- or publication-ineligible source material.

### Phase 5 — Protocol and embodied systems

1. Longband #76.
2. Home Bot #2.

Longband:

- executable state machine;
- Python/Rust accepted and rejected vectors;
- adversarial replay/fork/downgrade/metadata tests;
- independent security review checkpoint staged and documented.

Home Bot:

- exact mission/world/robot/controller/physics/seed/evaluator receipts;
- replayable observation/action trajectories;
- separate simulator, hardware-in-loop, and physical states;
- emergency stop, watchdog, collision envelope, and grants outside agent authority.

Phase 5 exit:

- protocol and simulator claims are executable and reproducible;
- physical claims require exact external evidence;
- no model, repository text, or simulated success can authorize physical actuation.

### Phase 6 — Full system qualification

1. Complete #64’s controlled task corpus, matched arms, ablations, and net-value analysis.
2. Complete #246 only from rights-compatible, independently evaluated trajectories.
3. Complete #247 using browser, Android, simulator, and physical-evidence profiles created by aligned repositories.
4. Reconcile and close #241 after all children and benchmark evidence are complete.
5. Run governance validation and default-branch CI across all repositories.
6. Search for remaining P0/P1 issues, open alignment PRs, stale governance pins, unauthorized deployment paths, and unresolved external gates.
7. Produce the final #251 evidence ledger and close #252, then #251.

## External checkpoints

Work Mode should prepare and continue past external checkpoints instead of stopping the whole program.

### Physical/device

Likely repositories: Wardriver, Home Bot, possibly Fleet/Kestrel.

Required staging:

- ready-to-run workflow or command;
- exact APK/firmware/controller/source identity;
- device, OS/API, permissions, sensor/radio, and connection prerequisites;
- expected observations and failure criteria;
- evidence collection path;
- cleanup and rollback instructions.

### Protected deployment/secrets

Likely repositories: Fleet, Society, Jam Day, Portfolio.

Required staging:

- candidate and exact source identity;
- protected environment and caller identity required;
- preflight and live probes;
- rollback/revocation procedure;
- no secret values in Git or issue comments.

### Human publication/account action

Likely repositories: Book, Socials, Portfolio, research publications.

Required staging:

- final candidate fingerprint;
- exact source and claim references;
- rights/disclosure review;
- human checklist;
- publication receipt template.

### Rights, consent, or independent review

Likely repositories: Tone, Family Tree, Longband, research corpora.

Required staging:

- exact affected assets;
- unresolved decision and why automation is insufficient;
- safe default while unresolved;
- expected reviewer identity and retained decision record.

## Central progress protocol

After each repository closure or shared-platform batch, post to #252:

```markdown
## Checkpoint — <repository or batch>

- Baseline commit:
- Issues closed:
- PRs merged:
- Acceptance evidence:
- Negative/adversarial coverage:
- Default-branch checks:
- External checkpoint staged:
- New dependency/risk:
- Execution-state update:
- Next active lane(s):
```

Do not post command-by-command narration. The journal should record material state transitions, evidence, and blockers.

## Final evidence ledger

The final #251 closeout must contain:

- all child issue dispositions;
- all merge commits;
- exact governance-validator revision used by each repository;
- default-branch CI status;
- retained evidence/receipt paths and fingerprints;
- personal-data retention/deletion coverage;
- corpus rights/partition/contamination coverage;
- deployment, publication, physical, and independent-review status;
- remaining external operator actions, if any;
- confirmation that the development pause introduced no unrelated features or replacement platforms.
