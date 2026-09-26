{ pkgs, ... }:

{
  packages = [
    pkgs.llama-cpp
  ];

  languages.python = {
    enable = true;
    package = pkgs.python312;
    venv.enable = true;
  };

  enterTest = ''
    python -m pip install -e '.[dev]'
    python -m ruff check src tests
    python -m pytest -q
    llama-cli --version
  '';
}
