#!/usr/bin/env bash
# Hosted qualification only. Stock image, staged immutable inputs, no registry push.
set -euo pipefail
: "${RUNNER_TEMP:?}" "${GITHUB_REPOSITORY:?}" "${GITHUB_SHA:?}"
: "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}"
root="$RUNNER_TEMP/native-receiver"
assets="$RUNNER_TEMP/native-assets"
evidence="$root/evidence"
mkdir -p "$evidence" "$root/wheels" "$root/site" "$root/output"
python - "$root" "$assets" <<'PY'
import hashlib, json, os, sys
from pathlib import Path
root, assets = map(Path, sys.argv[1:])
pin = json.loads(Path('tests/fixtures/native-receiver.json').read_text())
expected = Path(pin['native_input_pin']).read_bytes()
assert (assets/'inputs.json').read_bytes() == expected
assert (assets/'long-haul-source.txt').read_text().strip() == os.environ['GITHUB_SHA']
model_pin = json.loads(expected)
assert hashlib.sha256((assets/'model.gguf').read_bytes()).hexdigest() == model_pin['model_sha256']
(root/'evidence/input-pin.json').write_text(json.dumps(pin, indent=2)+'\n')
(root/'evidence/asset-manifest.sha256').write_bytes((assets/'SHA256SUMS').read_bytes())
(root/'evidence/source.json').write_text(json.dumps({
    'repository': os.environ['GITHUB_REPOSITORY'], 'commit': os.environ['GITHUB_SHA'],
    'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
    'asset_manifest_sha256': hashlib.sha256((assets/'SHA256SUMS').read_bytes()).hexdigest()
}, indent=2)+'\n')
PY
(cd "$assets"; sha256sum --check --strict SHA256SUMS)
# Actions artifact transport does not preserve executable mode; content stays exact.
chmod 0555 "$assets/bin/"*
mapfile -t dependencies < <(python -c 'import json; print("\n".join(json.load(open("tests/fixtures/native-receiver.json"))["test_dependencies"]))')
python -m pip download --disable-pip-version-check --only-binary=:all: --dest "$root/wheels" "${dependencies[@]}"
(cd "$root/wheels"; sha256sum ./*.whl > SHA256SUMS)
cp "$root/wheels/SHA256SUMS" "$evidence/wheels.sha256"
# Native extensions belong in immutable runtime inputs, not executable scratch.
python -m pip install --disable-pip-version-check --no-index --no-deps --no-compile \
  --target "$root/site" "$root/wheels/"*.whl > "$evidence/site-install.log"
(cd "$root/site"; find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
cp "$root/site/SHA256SUMS" "$evidence/site.sha256"
image="$(python -c 'import json; print(json.load(open("tests/fixtures/native-receiver.json"))["image"])')"
[[ "$image" =~ @sha256:[0-9a-f]{64}$ ]]
docker pull --platform linux/amd64 "$image"
docker image inspect "$image" > "$evidence/image-inspect.json"
cid=''
mounted=0
cleanup() {
  code=$?
  trap - EXIT
  if [[ -n "$cid" ]]; then
    docker inspect "$cid" > "$evidence/container-inspect.json" || code=1
    docker logs "$cid" > "$evidence/container.log" 2>&1 || code=1
    docker rm --force "$cid" > "$evidence/container-removal.txt" || code=1
  fi
  if (( mounted )); then
    cp -a "$root/output/." "$evidence/" || code=1
    sudo -n umount "$root/output" || code=1
  fi
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
sudo -n mount -t tmpfs -o size=64m,nosuid,nodev,noexec tmpfs "$root/output"
mounted=1
sudo -n chown "$(id -u):$(id -g)" "$root/output"
cid="$(docker create --platform linux/amd64 --init --network none --read-only \
  --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges \
  --cpus 1 --memory 1g --memory-swap 1g --pids-limit 64 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=512m \
  --mount "type=bind,src=$assets,dst=/assets,readonly" \
  --mount "type=bind,src=$root/wheels,dst=/wheels,readonly" \
  --mount "type=bind,src=$root/site,dst=/runtime-site,readonly" \
  --mount "type=bind,src=$PWD,dst=/source,readonly" \
  --mount "type=bind,src=$root/output,dst=/out" --workdir /source \
  -e HOME=/tmp/home -e PYTHONDONTWRITEBYTECODE=1 -e PIP_NO_INDEX=1 \
  -e LONG_HAUL_NATIVE_COMPLETION=/assets/bin/llama-completion \
  -e LONG_HAUL_NATIVE_TOKENIZE=/assets/bin/llama-tokenize \
  -e LONG_HAUL_NATIVE_SERVER=/assets/bin/llama-server \
  -e LONG_HAUL_NATIVE_GGUF=/assets/model.gguf \
  -e LONG_HAUL_REFERENCE_TOKENIZER=/assets/tokenizer \
  -e LONG_HAUL_NATIVE_EVIDENCE=/out \
  -e "LONG_HAUL_PARENT_NETNS=$(readlink /proc/self/ns/net)" \
  -e LONG_HAUL_REQUIRE_NATIVE_SMOKE=1 -e LONG_HAUL_REQUIRE_NETNS_SMOKE=1 \
  -e LONG_HAUL_REQUIRE_CONTAINER_SMOKE=1 \
  "$image" bash -c 'set -euo pipefail
    mkdir -p /tmp/home
    (cd /wheels; sha256sum --check --strict SHA256SUMS) > /out/wheel-verification.log
    (cd /runtime-site; sha256sum --check --strict SHA256SUMS) > /out/site-verification.log
    export PYTHONPATH=/source/src:/runtime-site
    python -m pip freeze --path /runtime-site > /out/python-packages.txt
    ldd /assets/bin/llama-server > /out/dynamic-libraries.txt
    python -m pytest -q -s -p no:cacheprovider --basetemp=/tmp/pytest \
      tests/test_llama_native_container.py tests/test_llama_native_network.py \
      tests/test_llama_native_completion.py tests/test_llama_native_server.py \
      --junitxml=/out/junit.xml')"
[[ "$cid" =~ ^[0-9a-f]{64}$ ]]
timeout --signal=TERM --kill-after=10s 300s docker start --attach "$cid" \
  2>&1 | tee "$evidence/attached.log"
