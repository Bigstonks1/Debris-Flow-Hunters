"""Import every module of the app and pipeline: catches missing packages and broken code.

    uv run python tests/check_imports.py
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDERS = ("pipeline", "app", "map", "tests/observed")


def main():
    failed = []
    for folder in FOLDERS:
        sys.path.insert(0, str(ROOT / folder))
        for path in sorted((ROOT / folder).glob("*.py")):
            try:
                importlib.import_module(path.stem)
                print(f"ok    {folder}/{path.name}")
            except BaseException as e:      # SystemExit too: a module must not exit on import
                failed.append(path)
                print(f"FAIL  {folder}/{path.name}: {type(e).__name__}: {e}")
        sys.path.pop(0)
    if failed:
        raise SystemExit(f"{len(failed)} module(s) failed to import")


if __name__ == "__main__":
    main()
