# Long Haul devenv / Nix runtime prototype

This branch is a non-production experiment for #91.

The first question is intentionally small: can standard nixpkgs/devenv provide
Python plus a working CPU `llama-cli` while the repository's existing Python
metadata and tests remain authoritative?

The prototype uses nixpkgs's existing `llama-cpp` package rather than cloning
and compiling llama.cpp in a reference vessel. It runs the existing Python test
suite and the runtime's version probe. It does not download a model or claim a
real inference result.

The Python package currently has version ranges in `pyproject.toml` but no lock
file. For this feasibility pass, pip installs those native project requirements
inside devenv's disposable virtual environment. If this architecture is
accepted, we should choose an established lock path (for example uv/devenv's
standard integration) rather than invent another resolver or copy package
versions into Fleet.

A successful result would justify the next prototype:
1. build a runtime-only OCI/Nix output containing Long Haul + `llama-cli`;
2. deploy that prebuilt artifact to a reference vessel;
3. remove compilers/source clones from vessel bootstrap;
4. separately test one tiny real GGUF smoke if a suitably small reviewed model
   is available.

CUDA/Jetson is explicitly outside this first pass. Nixpkgs already has CPU,
Vulkan, ROCm and CUDA-oriented llama.cpp packaging patterns; Kestrel can remain
on JetPack while we evaluate whether the artifact can run there. We should not
create a custom packaging framework if those established paths are insufficient;
that is a prototype result.
