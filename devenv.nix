{ pkgs, config, ... }:

let
  longHaul = config.languages.python.import ./. {};
in
{
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

  enterTest = ''
    python -m ruff check src tests
    python -m pytest -q
    python -c 'import long_haul; assert long_haul.Vessel'
    llama-cli --version
  '';
}
