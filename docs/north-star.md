# Long-Haul north-star architecture

> Status: design north star, 2026-09-29. This document records the intended architectural direction and the boundaries we want to preserve while the implementation evolves. It is not a claim that every component shown here is implemented today.

Long-Haul is converging on a standards-first blackboard/KRR architecture for persistent work across heterogeneous models and hardware. The authoritative state remains the append-only event log. Shared epistemic state, checkpoints, solver encodings, and model context are all rebuildable projections. Models participate as transient clients that propose hypotheses and actions; they do not own canonical institutional or epistemic state.

Related roadmap and design work: #5, #19, #59-#66, #118, #120, #130-#141, and top-level roadmap #136.

## 1. End-to-end north star

```mermaid
flowchart TB
    WC["Mission / WorkContract<br/>objective · scope · budgets · success/failure predicates · authority"]
    CREW["Crew identities / roles / competence<br/>persistent identity · independent initial positions · dissent"]
    DEC["Decision protocol<br/>consent · objection · scoped authority · experiment · escalation"]

    EVENTS[["Long-Haul append-only events<br/><b>AUTHORITATIVE</b><br/>observations · tool actions · evidence · decisions · telemetry · benchmarks"]]

    EPI["Shared epistemic blackboard<br/><b>MATERIALIZED / REBUILDABLE</b><br/>claims · assumptions · evidence · support · contradiction · preferences · subgoals<br/>RDF 1.1 · JSON-LD 1.1 · PROV-O · SHACL · optional RDFS/OWL"]
    CP["WorkCheckpoint<br/><b>MATERIALIZED</b><br/>compact resumable work state"]
    TD["TaskDigest<br/><b>MATERIALIZED</b><br/>bounded task-focused projection"]

    NAV["Model-facing epistemic navigation<br/><b>TRANSIENT</b><br/>query · explain · what-if · propose hypothesis · propose action"]
    ORCH["Reasoning / orchestration loop + scheduler<br/><b>TRANSIENT</b><br/>choose next action/model/tool/vessel · enforce budgets · track progress"]
    EXEC["Execution layer<br/><b>TRANSIENT</b><br/>models · tools · runtime adapters · vessels/resources"]
    EVAL["Independent acceptance evaluator<br/>read-only candidate view · external WorkContract predicates"]
    PACKET["EvidencePacket<br/>PASS / FAIL / UNKNOWN · measurements · artifact fingerprint · provenance"]

    WC --> EVENTS
    CREW --> EVENTS
    DEC --> EVENTS
    EVENTS --> EPI
    EPI --> CP
    EPI --> TD
    CP --> NAV
    TD --> NAV
    NAV --> ORCH
    ORCH --> EXEC
    EXEC --> EVAL
    EVAL --> PACKET
    PACKET --> EVENTS

    EVENTS -. reconstruct .-> CP
    EVENTS -. reconstruct .-> TD
```

### Core invariants

- The event log is the source of truth.
- Model sessions are disposable.
- Epistemic state is rebuildable from authoritative history.
- A model saying “done” is never acceptance evidence.
- Crew identity is independent of model checkpoint, process, embodiment, and session.
- Truth semantics and navigation policy are separate concerns.
- Heterogeneous resources are scheduled from measured topology and validated inference profiles, never fictitious aggregate capacity.
- Durable reasoning state stores claims, evidence, derivations, contradictions, decisions, actions, and outcomes, not unbounded private chain-of-thought.

## 2. Epistemic kernel / reason-maintenance

The intended “kernel” is deliberately small. Standards own representation and provenance; a replaceable reason-maintenance backend owns current-context dependency maintenance; specialized solvers receive compiled projections.

```mermaid
flowchart TB
    EVENTS[["Authoritative Long-Haul events<br/>append-only · replayable · provenance-bearing"]]

    SEM["Standards-based semantic materialization<br/><b>REBUILDABLE</b><br/>RDF 1.1 graph model<br/>JSON-LD 1.1 interchange<br/>PROV-O provenance<br/>SHACL validation<br/>SPARQL 1.1 compatible"]

    subgraph LIVE["Live current-context reason maintenance"]
      direction LR
      POS["Support for P<br/>evidence · assumptions · justifications"]
      NEG["Support for ¬P<br/>evidence · assumptions · justifications"]
      STATUS["Derived epistemic status<br/><br/>SUPPORTED ONLY<br/>REFUTED ONLY<br/>BOTH / CONFLICT<br/>NEITHER / UNKNOWN"]
      DEP["Dependency manager<br/>multiple supports · directed invalidation · cascading retraction · explanations"]
      POS --> STATUS
      NEG --> STATUS
      POS --> DEP
      NEG --> DEP
    end

    BACK["Replaceable backend<br/>initial candidate: CLIPS 6.4.2<br/>reference/oracles: Drools JTMS/defeasible + LTMS<br/>future native Rust backend"]
    PROJ["Compiled projections<br/><b>NOT AUTHORITATIVE</b>"]
    SAT["SAT / CNF<br/>consistency · entailment · unsat cores"]
    SMT["SMT-LIB<br/>richer theories · models · what-if"]
    ASP["clingo / ASP<br/>bounded hypothetical worlds · non-monotonic search"]
    DLOG["Datalog / XSB / dataflow<br/>recursive relational inference"]
    AIG["AIG / AIGER<br/>bulk Boolean evaluation<br/>future packed SIMD: scalar · AVX2 · AVX-512 · NEON"]

    EVENTS --> SEM
    SEM --> LIVE
    LIVE --> BACK
    LIVE -. compile .-> PROJ
    PROJ --> SAT
    PROJ --> SMT
    PROJ --> ASP
    PROJ --> DLOG
    PROJ --> AIG

    BACK -. rebuildable implementation detail .-> LIVE
```

### Reason-maintenance semantics

For a proposition (P), positive and negative support are maintained independently. Contradiction is preserved and localized instead of “resolved” by silently deleting one side.

| Support for P | Support for ¬P | Model-facing status |
| --- | --- | --- |
| yes | no | supported only |
| no | yes | refuted only |
| yes | yes | both / conflict |
| no | no | neither / unknown |

This permits paraconsistent behavior at the Long-Haul semantic layer while keeping the live dependency engine simple and replaceable.

### Backend strategy

The current research direction is:

1. **CLIPS 6.4.2** as the first small embedded backend candidate for logical support, multiple justifications, dependency inspection, and automatic retraction.
2. **Drools/KIE JTMS + defeasible belief systems** as a mature semantic/conformance reference.
3. **LTMS** as a readable textbook-style oracle for JTMS/LTMS/ATMS behavior.
4. **SAT/SMT/ASP** for bounded hypothetical contexts instead of forcing every possible world into the live TMS.
5. **AIG/AIGER** for an optimized Boolean projection, eventually backed by native packed/SIMD evaluation.
6. No backend-private IDs, clauses, or graph objects become durable Long-Haul domain identity.

See #130, #131, #132, #138, and #139.

## 3. Tiny-model reasoning and session lifecycle

Long-Haul should externalize the temporal structure that tiny models struggle to maintain internally. Each invocation receives a focused view of shared state and performs one bounded transition.

```mermaid
flowchart TB
    WC["WorkContract<br/>goal · scope · constraints · predicates"]
    TD["Current TaskDigest<br/>relevant claims · open unknowns · evidence · subgoal"]
    PRIME["Optional reasoning primer<br/>small mode prefix: INVESTIGATE · DEBUG · PLAN · VERIFY"]

    ORIENT["1 · ORIENT<br/>inspect focused state<br/>identify current subgoal / discriminating unknown"]
    PROPOSE["2 · PROPOSE<br/>one hypothesis, query, tool action, or bounded experiment"]
    EXEC["3 · EXECUTE<br/>tool/runtime adapter enforces permissions, budgets, timeout"]
    OBS["4 · OBSERVE<br/>capture actual result as authoritative event"]
    UPDATE["5 · UPDATE EPISTEMIC STATE<br/>add support/conflict · enable/retract assumptions · recompute dependents"]
    VERIFY["6 · INDEPENDENT VERIFY<br/>evaluate candidate fingerprint against WorkContract predicates"]
    NEXT["7 · NEXT STEP / TERMINATE<br/>continue · retry · escalate · accept · reject · human intervention"]

    WC --> ORIENT
    TD --> ORIENT
    PRIME --> ORIENT
    ORIENT --> PROPOSE --> EXEC --> OBS --> UPDATE --> VERIFY --> NEXT
    NEXT -->|continue| ORIENT

    subgraph HANDOFF["Cross-model handoff: same epistemic state, no transcript dependency"]
      direction TB
      TINY["tiny local model<br/>cheap exploration / hypothesis generation"]
      LOCAL["larger local model<br/>deeper analysis / synthesis"]
      REMOTE["Anchorage or remote stronger model<br/>only when measured escalation policy warrants it"]
      TINY -. escalate .-> LOCAL -. escalate .-> REMOTE
    end

    TD -. same projection .-> TINY
    TD -. same projection .-> LOCAL
    TD -. same projection .-> REMOTE
```

### Separation of truth and navigation

**Truth semantics** answer:
- What is supported, refuted, both, or unknown?
- What depends on what?
- What changed when an assumption was retracted?
- What evidence supports this conclusion?

**Navigation policy** answers:
- Which unknown is worth investigating next?
- Which observation would discriminate among live hypotheses?
- Which model/tool/vessel should perform that investigation?
- Is escalation worth the additional latency, memory, or cost?

Navigation may eventually be learned. It must never silently rewrite truth semantics. See #133, #134, #140, and #141.

## 4. Fleet execution, hardware, and evidence loop

Execution remains topology-aware and model/runtime-neutral. Execution modes describe orchestration; resident/offloaded/split/streaming/parallel behavior belongs to validated inference profiles.

```mermaid
flowchart TB
    MISS["Mission requirements<br/>goals · constraints · budgets · safety/cooperation"]
    COMP["Crew competence history<br/>measured outcomes · calibration · domain trust"]
    PROF["Validated inference profiles<br/>artifact · quantization/adapters · runtime · strategy · participating resources"]
    EPI["Current epistemic needs<br/>next subgoal · uncertainty · capability need · information gap"]
    TOPO["Measured fleet topology<br/>CPU/GPU/memory/storage · load · links · bandwidth/latency · benchmarks"]

    FILTER["Hard eligibility filters<br/>safety/cooperation · scope/authority · runtime validation · typed resource fit · topology/link policy"]
    RANK["Rank eligible plans<br/>mission fit · epistemic value · continuity/optionality · efficiency"]
    PLAN["ExecutionPlan<br/>crew · artifact · runtime · resources · vessel(s) · budgets"]

    subgraph MODES["Execution modes — orchestration only"]
      direction LR
      LOCAL["LOCAL<br/>one vessel"]
      POOL["POOL<br/>independent jobs"]
      PIPE["PIPELINE<br/>one model/workflow spans resources"]
      COMPOSE["COMPOSE<br/>specialist crew/models cooperate"]
    end

    ADAPTER["Runtime strategy / adapter<br/>resident · CPU · GPU offload · split/tensor split · streaming · parallel<br/>llama.cpp · coding/tool harnesses · domain tools"]

    subgraph VESSELS["Heterogeneous vessels"]
      direction LR
      K["Kestrel-class edge vessel<br/>ARM64 CPU · shared/unified memory · constrained GPU/memory budget"]
      A["Anchorage-class workstation<br/>high-thread-count CPU · CUDA GPU resources · system RAM · NVMe · fast LAN"]
      R["Additional / remote vessels<br/>other local systems · cloud/remote servers · measured network links"]
    end

    EXEC["Transient execution<br/>run plan · capture logs/artifacts/observations · enforce limits"]
    RESULT["ExecutionResult<br/>timings · tokens · resource usage · artifacts · observations · failures"]
    EVAL["Independent acceptance evaluator<br/>read-only candidate · exact fingerprint · external predicates"]
    PACKET["EvidencePacket<br/>PASS / FAIL / UNKNOWN · measurements · evaluator/runtime identity · provenance"]
    EVENTS[["Append results/evidence to authoritative events"]]
    PROGRESS["Progress / no-progress monitor<br/>repetition · regression · intervention"]
    RECOVER["Bounded recovery / checkpoint / escalation<br/>retry · alternate model/vessel · reduce scope · request human input"]

    MISS --> FILTER
    COMP --> FILTER
    PROF --> FILTER
    EPI --> FILTER
    TOPO --> FILTER
    FILTER --> RANK --> PLAN --> MODES --> ADAPTER --> VESSELS --> EXEC --> RESULT --> EVAL --> PACKET --> EVENTS
    EVENTS --> PROGRESS --> RECOVER
    RECOVER -. new plan / next step .-> FILTER
```

### North-star metrics

Optimize for verified progress rather than raw token speed:

- externally verified task success;
- wall time;
- tokens in/out;
- energy where measurable;
- peak resident memory/context;
- repeated-action rate;
- successful self-correction;
- calibration;
- verifier disagreement;
- escalation depth/frequency;
- performance per resident GiB;
- topology/resource utilization.

## 5. Persistence and security boundary

Persist:
- normalized claims and current status;
- evidence references and exact event provenance;
- assumptions and support relations;
- contradiction/conflict state;
- decisions, actions, and externally verifiable outcomes;
- artifact fingerprints;
- benchmark/competence/calibration evidence.

Do not persist by default:
- raw hidden chain-of-thought or model scratchpads;
- provider-private reasoning blocks;
- secret-bearing tool payloads;
- full prompts when compact structured state suffices;
- solver-internal CNF/AIG/backend representations;
- transient model/session context.

See #135.

## 6. Implementation principle

> Simple implementation first does not mean disposable architecture first.

Reference implementations may prioritize clarity and iteration speed. Known optimized destinations should still have explicit seams from the beginning. We do not need profiling to learn that a packed native Boolean kernel can outperform Python object traversal; profiling is for choosing among native representations, SIMD widths, memory layouts, incremental strategies, CPU/GPU crossover points, and other genuine design alternatives.

The standards-based semantic representation and conformance corpus should allow implementations to move from Python/reference code to C/Rust/native SIMD without migrating Long-Haul's durable state.
