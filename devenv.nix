{ pkgs, config, lib, ... }:

let
  longHaul = config.languages.python.import ./. {};
in
{
  name = "long-haul";

  # One pinned nixpkgs Python is used by both the interactive/test environment
  # and uv2nix package output. The release environment is therefore determined
  # by devenv.lock rather than a second Python-version input.
  packages = lib.optionals (!config.container.isBuilding) [
    pkgs.llama-cpp
  ];

  languages.python = {
    enable = true;
    venv.enable = !config.container.isBuilding;
    uv = {
      enable = !config.container.isBuilding;
      sync = {
        enable = !config.container.isBuilding;
        extras = [ "dev" ];
        arguments = [ "--locked" ];
      };
    };
  };

  outputs."long-haul" = longHaul;

  containers."runtime" = {
    name = "long-haul-runtime";
    copyToRoot = [
      longHaul
      pkgs.llama-cpp
    ];
    entrypoint = [ "/bin/bash" "-lc" ];
    startupCommand = ''
      set -e
      export PATH=/env/bin:/bin:/usr/bin
      ! command -v uv
      ! command -v ruff
      ! command -v pytest
      ! command -v cmake
      ! command -v ninja
      ! command -v cc
      ! command -v c++
      python --version
      llama-cli --version
      python -c 'import long_haul; assert long_haul.Vessel'
    '';
  };

  enterTest = ''
    python --version
    python -m ruff check src tests
    python -m pytest -q
    python -c 'import long_haul; assert long_haul.Vessel'
    llama-cli --version
  '';
}
