# Hardware-validation runbook

Nothing in this document is a measured result. The fixture topology and fixture
benchmarks are deliberately simulated development inputs.

## On Anchorage

1. Install the package and run `python -m long_haul fleet/anchorage.yaml` to
   validate the resulting normalized inventory.
2. Capture `/proc/meminfo`, `lspci -tv`, `lspci -vv`, and
   `nvidia-smi --query-gpu=name,memory.total,pci.bus_id --format=csv,noheader`.
   Feed captured output through the parser tests before adding it as inventory
   evidence. Confirm the host path for `ANC-G0` and `ANC-G1` independently.
3. Record `lsblk --json`, `findmnt --json`, and storage read/write measurements
   for the NVMe. Store observations with `provenance=measured`; do not replace
   any prior observation.
4. For each runtime/profile, collect cold and warm load time, TTFT, prefill and
   decode throughput, residency, host/device traffic, power if available, and
   errors. Name every participating resource explicitly.

## On Kestrel

1. Capture the same Linux inventory and the Jetson runtime/memory details.
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

Commit or otherwise retain captured raw command output alongside a normalized
inventory and append-only benchmark JSONL export. The scheduler then consumes
those normalized records without hardware-specific changes.
