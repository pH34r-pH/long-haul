# Public reference infrastructure map

[`main.bicep`](main.bicep), [`modules/`](modules/), `parameters/`, and `cloud-init/` describe reproducible reference infrastructure only. [`mcp/`](mcp/) is a separately packaged public reference service. Shell checks under [`scripts/`](scripts/) validate templates and cost bounds; they do not represent the private Fleet control plane.

Keep credentials, private host/network state, deployment decisions, and active Fleet operations out of this tree. Runtime/documentation changes should route through the public contracts and boundary docs rather than editing deployment workflows.
