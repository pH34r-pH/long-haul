{ pkgs, config, ... }:

let
  longHaul = config.languages.python.import ./. {};
in
{
  name = "long-haul";

  packages = [
    pkgs.llama-cpp
  ];

  languages.python = {
    enable = true;
    package = pkgs.python312;
    venv.enable = true;
    uv = {
      enable = true;
      sync = {
        enable = true;
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
    startupCommand = "set -e; /env/bin/llama-cli --version; /env/bin/python -c 'import long_haul; assert long_haul.Vessel'";
  };

  enterTest = ''
    python -m ruff check src tests
    python -m pytest -q
    python -c 'import long_haul; assert long_haul.Vessel'
    llama-cli --version
  '';
}
