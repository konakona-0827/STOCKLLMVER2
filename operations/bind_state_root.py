"""Bind this checkout to the existing account; never copy credentials or rows."""
import argparse
from pathlib import Path
from runtime_paths import bind_state_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True)
    args = parser.parse_args()
    print(bind_state_root(Path(__file__).resolve().parents[1], args.state_root))


if __name__ == '__main__':
    main()
