import argparse

from .registry import load_vessel


def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("manifest"); args=p.parse_args()
    print(load_vessel(args.manifest).model_dump_json(indent=2))
if __name__ == "__main__": main()
