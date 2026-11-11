"""Hosted trusted acquisition: locked npm, no lifecycle scripts, maintained verification."""
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from long_haul.adapters.pi_assets import (
    INTEGRITY,
    LOCK,
    PACKAGE,
    SOURCE,
    VERSION,
    runtime_fingerprint,
)
from long_haul.work.repository_models import content_digest


def command(argv, root, environment):
    result = subprocess.run(argv, cwd=root, env=environment, capture_output=True, timeout=300, check=False)
    if result.returncode:
        # Never echo network/config errors that might contain caller credentials.
        raise RuntimeError("trusted npm step failed: " + argv[1])
    return result


def main(root):
    root.mkdir(parents=True, exist_ok=False)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(Path("tools/pi-runtime") / name, root / name)
    assert content_digest((root / "package-lock.json").read_bytes()) == LOCK
    home = root.parent / "pi-acquisition-home"
    home.mkdir()
    (home / "user.npmrc").write_text("")
    (home / "global.npmrc").write_text("")
    npm = shutil.which("npm")
    environment = {"PATH": str(Path(npm).parent) + os.pathsep + os.defpath, "HOME": str(home),
                   "npm_config_userconfig": str(home / "user.npmrc"),
                   "npm_config_globalconfig": str(home / "global.npmrc"),
                   "npm_config_cache": str(home / "cache"), "npm_config_ignore_scripts": "true",
                   "npm_config_update_notifier": "false", "CI": "true"}
    command([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org/"], root, environment)
    audit = command([npm, "audit", "signatures", "--registry=https://registry.npmjs.org/"], root, environment)
    npm_version = command([npm, "--version"], root, environment).stdout.decode().strip()
    endpoint = "https://registry.npmjs.org/-/npm/v1/attestations/@earendil-works%2fpi-coding-agent@1.5.2"
    with urllib.request.urlopen(endpoint, timeout=30) as response:
        bundle = response.read(1048576)
    subjects = []
    for attestation in json.loads(bundle)["attestations"]:
        statement = json.loads(base64.b64decode(attestation["bundle"]["dsseEnvelope"]["payload"]))
        if statement.get("predicateType") != "https://slsa.dev/provenance/v1":
            continue
        dependencies = statement["predicate"]["buildDefinition"]["resolvedDependencies"]
        assert any(item.get("digest", {}).get("gitCommit") == SOURCE
                   and item["uri"].startswith("git+https://github.com/earendil-works/pi@") for item in dependencies)
        subject = statement["subject"][0]
        assert subject["digest"]["sha512"] == base64.b64decode(INTEGRITY.removeprefix("sha512-")).hex()
        subjects.append(subject)
    assert len(subjects) == 1
    receipt = {"package": PACKAGE, "version": VERSION, "source": SOURCE, "lock": LOCK,
               "npm": npm_version, "lifecycle_scripts": "disabled", "signature_verifier": "npm audit signatures",
               "audit_exit_code": audit.returncode, "audit_stdout_digest": content_digest(audit.stdout),
               "attestation_bundle_digest": content_digest(bundle), "subject": subjects[0],
               "runtime_tree": runtime_fingerprint(root), "scope": "acquisition; not worker or model qualification"}
    output = root.parent / "pi-acquisition-receipt.json"
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
