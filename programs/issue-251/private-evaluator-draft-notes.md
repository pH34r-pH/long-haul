# Private evaluator draft: remaining security boundaries

This local branch is an experimental prototype for #245. It is not an admitted worker or evidence profile and must not be used for private tasks or merged as complete.

- Existing v1 evaluator and repository-work fingerprints can commit to low-entropy sealed inputs or outputs. Removing those hashes from the worker prompt in #259 is a narrow mitigation, not a complete private-evidence boundary. Historical v1 evidence must remain readable, but newly emitted raw v1 packets must not be assumed public-safe.
- The v2 prototype uses independent keyed commitments for every non-public check, including policy and inspection checks, and emits a redacted summary. Its keys currently exist only in a control-plane Python object. Secure provision, independent generation, access control, durable storage, rotation, replay and destruction are not integrated with an admitted existing secret authority.
- The experimental sink now requires an exact trusted-control registry entry and a current resource-policy retention decision before and after evaluation. The callback remains trusted control code: this does not authenticate its registration, prove durable private persistence, or constrain what the callback does. Integrate a real authenticated private persistence authority before use, and qualify invalid/interrupted attempts, storage failures, retention, revocation and deletion.
- Public event, evaluator, contract, candidate and worker projections must be tested together for low-entropy enumeration and payload disclosure; public identifiers, task/context references and metadata require explicit classification. Binding and safe-summary verification alone do not provide authentication.
- Exercise malicious sink, stale policy, revoked or rotated keys, forged or replayed summaries, changed candidate/environment, denied store access, cleanup failures and crash recovery. Do not treat fake sandbox tests as gVisor qualification.
- Do not emit raw private hashes, hidden inputs, diagnostics, output digests, private paths or key material in public events or model-visible surfaces. Preserve existing public/historical v1 evidence without silently relabelling it private-safe.

See #245 and #252 for acceptance criteria and current program evidence.
