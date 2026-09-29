# Anchorage heterogeneous inference preparation

This document is the executable-research handoff for the Anchorage A→I program tracked in issue #123. It does not replace private Fleet hardware qualification.

## Boundary

Long Haul owns portable experiment semantics, inference profiles, benchmark records, and scheduler behavior. Long Haul Fleet owns the concrete Anchorage host, physical topology, protected execution, and private hardware evidence.

No physical performance result should be interpreted as qualified while Fleet hardware-stabilization issue #778 remains open.

## Preparation order

| Step | Question | Portable preparation |
| --- | --- | --- |
| A | Is the vessel stable and measured? | Consume Fleet-qualified topology and measurements; do not infer from nominal hardware. |
| B | What is the Ryzen CPU envelope? | Sweep generation/batch threads and measured CPU affinity with profile identity preserved. |
| C | Which CPU-native backend/model shape wins? | Compare upstream CPU paths under the same benchmark contract. |
| D | Which CPU work overlaps useful GPU work? | Run paired workloads and charge interference to both lanes. |
| E | Does heterogeneous speculation reduce latency? | Compare no speculation, n-gram, CPU-draft and G1-draft configurations. |
| F | Is CPU expert compute cheaper than moving expert weights? | Use llama.cpp CPU-MoE controls before custom offload logic. |
| G | Can a sparse logical model exceed one GPU's VRAM usefully? | Select the smallest faithful open sparse model and compare simple hybrid placements first. |
| H | What should remain resident on G1? | Measure draft/decision/specialist/hot-expert roles by net task value and transfer cost. |
| I | Do the surviving mechanisms compose? | Integrate only independently qualified RLM, routing, sparse execution and working-set controls. |

## Existing adapter controls

The llama.cpp adapter exposes placement controls through `InferenceProfile.options` rather than adding Anchorage-specific runtime code:

- `threads` → `--threads`
- `threads_batch` → `--threads-batch`
- `cpu_range` → `--cpu-range`
- `cpu_range_batch` → `--cpu-range-batch`
- `cpu_strict` / `cpu_strict_batch`
- `gpu_layers`, `split_mode`, `tensor_split`, `main_gpu`
- `cpu_moe` → `--cpu-moe`
- `n_cpu_moe` → `--n-cpu-moe`
- `spec_type` → `--spec-type` (including n-gram modes that need no draft model)
- `draft_model_path`, `draft_device`, `draft_gpu_layers`
- `draft_threads`, `draft_threads_batch`, `draft_cpu_range`
- `draft_n_max`, `draft_n_min`
- `draft_cpu_moe`, `draft_n_cpu_moe`

These are edge options. The scheduler should consume qualified inference profiles, not embed llama.cpp flags.

## First safe matrix

The checked-in CPU thread-sweep manifest is deliberately small. It is a preparation artifact, not evidence that a particular thread count is correct. Fleet must bind CPU ranges to observed topology before CCD-specific affinity comparisons.

The first physical sequence after Step A should be:

1. CPU thread sweep with a fixed small model and fixed prompt/output budget.
2. Repeat the useful thread points with independently observed CPU affinity.
3. Characterize G0 and G1 separately.
4. Only then run concurrent CPU+GPU conditions.

## Measurements still outside the current portable harness

The current benchmark store captures inference timings and generic residency/utilization/network maps, but the A→I program also needs physical evidence such as sustained memory bandwidth, PCIe bytes/time, thermals and per-resource interference. Fleet should collect those without moving private hardware state into this repository.

Add schema only when a measurement needs portable semantics; do not create fields merely to mirror one monitoring tool.
