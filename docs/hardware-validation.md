# Hardware-validation runbook

Nothing in this document is a measured result. The fixture topology and fixture
benchmarks are deliberately simulated development inputs. Concrete vessel
manifests and captured measurements belong in private Fleet.

## On a workstation vessel

1. Install the package and run `python -m long_haul show tests/fixtures/vessels/station.yaml`
   to inspect the synthetic example.
   Validate the operator's actual inventory from its private Fleet path.
2. Capture `/proc/meminfo`, `lspci -tv`, `lspci -vv`, and
   `nvidia-smi --query-gpu=name,memory.total,pci.bus_id --format=csv,noheader`.
   Feed captured output through the parser tests before adding it as inventory
   evidence. Confirm each accelerator's host path independently.
3. Record `lsblk --json`, `findmnt --json`, and storage read/write measurements
   for the NVMe. Store observations with `provenance=measured`; do not replace
   any prior observation.
4. For each runtime/profile, collect cold and warm load time, TTFT, prefill and
   decode throughput, residency, host/device traffic, power if available, and
   errors. Name every participating resource explicitly.

## On an ARM64 edge vessel

1. Capture the same Linux inventory and, when applicable, the Jetson
   runtime/memory details. The synthetic example is
   `tests/fixtures/vessels/ship.yaml`.
2. Record whether memory capacity is unified at the runtime level rather than
   assuming GPU memory is independently reservable.
3. Collect the same cold/warm benchmark records for supported profiles.

## Network

From both hosts capture `tailscale status --json` and peer ping output for a
good direct path, a degraded path, a relayed path, and disconnection. Measure
throughput separately. Persist the path mode, RTT, and throughput as measured
link/benchmark evidence. A relayed or unknown path must not authorize
synchronous `PIPELINE`; it can still support independent `POOL` work.

## Expected artifact

Retain captured raw command output alongside a normalized inventory and
append-only benchmark JSONL export in the operator's private Fleet. The
scheduler consumes those normalized records without hardware-specific changes.
