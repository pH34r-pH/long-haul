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
    package = pkgs.python312;
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
      test ! -x /env/bin/uv
      test ! -x /env/bin/ruff
      test ! -x /env/bin/pytest
      test ! -x /env/bin/cmake
      test ! -x /env/bin/ninja
      test ! -x /env/bin/cc
      test ! -x /env/bin/c++
      /env/bin/llama-cli --version
      /env/bin/python -c 'import long_haul; assert long_haul.Vessel'
    '';
  };

  enterTest = ''
    python -m ruff check src tests
    python -m pytest -q
    python -c 'import long_haul; assert long_haul.Vessel'
    llama-cli --version
  '';
}
