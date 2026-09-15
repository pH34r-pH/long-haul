# Product / fleet repository boundary

Long Haul uses two repositories with different authority.

- **`pH34r-pH/long-haul` (this public repository)** is authoritative for portable product code, schemas/interfaces, runtime and device support, topology/scheduling logic, portable fixtures/tests, and public documentation.
- **`pH34r-pH/long-haul-fleet` (private)** is authoritative for the operator's concrete physical/cloud deployment, privileged self-hosted runners, private networking, provisioning/recovery, and hardware qualification.

The private fleet control plane consumes an explicit immutable commit from this repository for qualification. Public pull-request code must not automatically gain execution on privileged fleet machines.

## Migration principle

Existing deployment-specific material in this repository predates the split. It should be disentangled incrementally rather than deleted wholesale. Generic capability remains public; operator-specific deployment and lifecycle automation moves to the private fleet repository.

Current review targets include the concrete `fleet/anchorage.yaml` and `fleet/kestrel.yaml` manifests, Azure provisioning/lifecycle material under `infra/azure/`, Azure deployment workflows, and hardware-validation procedures. Each must be classified into reusable product capability versus private deployment before migration.
