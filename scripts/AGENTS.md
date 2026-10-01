# Script and validation map

- [`build_offline_package.py`](build_offline_package.py) creates the exact-source public package.
- [`check_documentation_artifacts.py`](check_documentation_artifacts.py) classifies changed living Markdown and rejects incidental cache/temp artifacts.
- [`test_documentation_artifacts.py`](test_documentation_artifacts.py) proves descriptive naming, historical/generated boundaries, safe rename/copy parsing, and fixture behavior.
- [`../tools/requirements.py`](../tools/requirements.py) validates declared native requirements.

Keep scripts bounded, deterministic, argument-safe, and free of credentials. Preserve exact source/digest evidence and do not turn the documentation guard into a repository-wide historical rewrite. Artifact prevention applies to added, renamed, and copied paths; ordinary modifications to baseline artifacts are deliberately skipped.

```sh
python scripts/test_documentation_artifacts.py
python -m pytest -q tests/test_offline_package.py
```
