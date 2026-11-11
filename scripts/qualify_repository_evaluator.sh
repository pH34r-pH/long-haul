#!/usr/bin/env bash
# Hosted ephemeral qualification only; no Fleet/production installation.
set -euo pipefail
: "${RUNNER_TEMP:?}"
root="$RUNNER_TEMP/repository-evaluator-runtime"
mkdir -p "$root"
python - "$root" <<'PY'
import hashlib, json, tarfile, urllib.request, sys
from pathlib import Path
root=Path(sys.argv[1]); pin=json.loads(Path('tests/fixtures/repository-evaluator.json').read_text())
archive=root/'gvisor.tar.bz2'
with urllib.request.urlopen(pin['gvisor_url'], timeout=120) as response, archive.open('wb') as output:
    while chunk := response.read(1048576): output.write(chunk)
actual='sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()
assert actual == pin['environment']['runtime_archive_digest'], 'gVisor archive fingerprint mismatch'
with tarfile.open(archive) as bundle: bundle.extractall(root, filter='data')
for name, expected in pin['environment']['runtime_files']:
    assert 'sha256:'+hashlib.sha256((root/name).read_bytes()).hexdigest() == expected, name
print(json.dumps({'runtime_archive':actual, 'runtime_version':pin['environment']['runtime_version']}))
PY
install_root=/usr/local/lib/long-haul-gvisor
sudo -n mkdir -p "$install_root"
sudo -n cp -a "$root/runsc" "$root/gvisor-bin" "$install_root/"
sudo -n chmod -R a+rX "$install_root"
sudo -n "$install_root/runsc" install --runtime=long-haul-gvisor \
  --download-sidecars=NEVER --require-sidecars=ALWAYS -- --network=none
sudo -n systemctl reload docker
image="$(python -c 'import json; print(json.load(open("tests/fixtures/repository-evaluator.json"))["environment"]["image"])')"
docker pull --platform=linux/amd64 "$image"
# Public immutable-image startup probes retain full launcher errors on CI failure.
for mode in plain init init-expanded-pids; do
  extra=()
  pids=32
  if [[ "$mode" != plain ]]; then extra=(--init); fi
  if [[ "$mode" == init-expanded-pids ]]; then pids=128; fi
  echo "Pinned toolchain startup probe: $mode"
  docker run --rm "${extra[@]}" --runtime=long-haul-gvisor --network=none --read-only \
    --user=65534:65534 --cap-drop=ALL --security-opt=no-new-privileges \
    --cpus=1 --memory=256m --memory-swap=256m --pids-limit="$pids" \
    "$image" /usr/local/bin/python3 -I -S -c 'print("trusted-python-started")' || true
done
export LONG_HAUL_RUNSC="$install_root/runsc" LONG_HAUL_REQUIRE_GVISOR=1
python -m pytest -q -s tests/test_repository_gvisor.py
