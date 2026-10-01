# Discovery map

[`contracts.py`](contracts.py) defines edge protocols. [`linux.py`](linux.py), [`windows.py`](windows.py), and [`parsers.py`](parsers.py) turn platform observations into normalized [`Vessel`](../models.py) resources. Discovery is an evidence-producing edge: it must not make scheduler decisions or infer unsupported capability from a missing probe.

## Invariants

- Unknown, unavailable, and simulated values stay explicit.
- Resource and link identifiers must satisfy the domain model before entering scheduling.
- Probe commands remain bounded and isolated at the edge; core code consumes normalized records.
- Fixture data is not a measurement and must remain labelled as such.

## Focused validation

```sh
python -m pytest -q tests/test_parsers.py tests/test_windows_discovery.py tests/test_reference.py
```
