{ pkgs, config, lib, ... }:

let
  longHaul = config.languages.python.import ./. {};
in
{
  name = "long-haul";

  # Development-shell conveniences stay outside the runtime container. The
  # container copies only the explicit runtime roots below.
  packages = lib.optionals (!config.container.isBuilding) [
    pkgs.llama-cpp
  ];

  languages.python = {
    enable = true;
    # Use one explicit Python selection for both the interactive shell and
    # uv2nix package output. Setting only 'package' leaves import() on the
    # ambient nixpkgs Python.
    version = "3.12";
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
      ! command -v uv
      ! command -v ruff
      ! command -v pytest
      ! command -v cmake
      ! command -v ninja
      ! command -v cc
      ! command -v c++
      python --version | grep -E '^Python 3\.12\.'
      llama-cli --version
      python -c 'import long_haul; assert long_haul.Vessel'
    '';
  };

  enterTest = ''
    python --version | grep -E '^Python 3\.12\.'
    python -m ruff check src tests
    python -m pytest -q
    python -c 'import long_haul; assert long_haul.Vessel'
    llama-cli --version
  '';
}
