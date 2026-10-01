# Contract map

[`requirements.cue`](requirements.cue) and [`vessel-profile.cue`](vessel-profile.cue) are durable machine-readable schemas. [`vessel-examples.json`](vessel-examples.json) is public example input and must remain visibly distinguishable from measured private Fleet inventories. [`tools/requirements.py`](../tools/requirements.py) inventories and validates declared native requirements; the tests under [`tools/`](../tools/) verify bounded paths, profiles, and source boundaries.

Schema edits can affect registry loading, requirements CI, packaging, and downstream Fleet intake. Keep historical records readable and make compatibility changes explicit.

```sh
python -m unittest tools.test_requirements tools.test_requirements_boundaries
```
