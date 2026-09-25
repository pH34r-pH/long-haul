# Phase 1 requirements contract

`.fleet/requirements.json` is data validated by `contracts/requirements.cue`.
Native manifests remain the dependency/version authority; each tool names its
source files. Profiles distinguish phases and concrete OS/architecture/ABI.
Android/bionic must not be conflated with Linux/glibc. Entry points are
informational strings: the validator never executes them or installs anything.

```sh
cue vet -c contracts/requirements.cue .fleet/requirements.json -d '#Requirements'
python3 tools/requirements.py --root /path/to/checked-out/repo --output requirements.json
python3 -m unittest discover -s tools -p test_requirements.py -v
```

Use CUE v0.17.0 and Python 3.11+. The Python helper adds bounded native-file
fingerprints, extracts native dependency/version declarations, and rejects
missing/escaping files, duplicate declarations and undeclared tool sources.
It does not solve package versions, grant authority or claim software is installed.
A deployer must allowlist tools, select compatible native environments, and run
its trusted service-user functional probes. Unknown versions/capabilities are
not installation or admission success.

## Reuse by sibling repositories

Check out an explicitly reviewed full commit of this public repository into a
separate tooling directory. Run its validator against the candidate's local
`.fleet/requirements.json`; do not execute a validator obtained from the
candidate repository on a privileged host. Keep source and tooling commit IDs
with the output digests. Private deployments independently verify source identity;
a self-reported declaration is not authority to install privileged commands.

Native lockfiles select exact transitive versions. A requirement change updates
the native file and references it here, not a copied Fleet version list. Schema
changes require reviewing and advancing the consumer's one tooling revision.

## Vessel model integration

`contracts/vessel-profile.cue` is companion capability/provenance metadata keyed
by existing `models.Vessel.id`. Existing `class_name` (station/ship/light_craft),
resource IDs, typed capacities and measured links remain authoritative. This
change does not replace or relax `Vessel.resource_references_exist`, runtime
validation depth, scheduler feasibility, consent or benchmark requirements.

Custom, published-consumer and published-devboard describe provenance, not OS
or trust. `extension.*` allows future classes; form and roles remain orthogonal.
The global contract names shared semantics, not universal Linux packages.
Published references, observed facts and qualified capability evidence remain
separate. Quirks contain exact selector facts, reference, workaround description
and retest criteria; they are data, never automatic privileged instructions.

`#Admission` rejects unknown/declared-only capabilities and fixture-only evidence.
It expresses necessary evidence, not sufficient authorization: the deployment
must additionally check evidence freshness, environment binding, consent and
workload validation depth. The four examples are **synthetic**; none proves a
real device is qualified. Actual inventories and measurements stay in private Fleet.

This is the contract/provenance slice of #86. Real platform probes, measured
quirk handling and private provisioner integration remain separate Phase 1 gates.
No Azure migration, history retention system or cost optimizer is included.
