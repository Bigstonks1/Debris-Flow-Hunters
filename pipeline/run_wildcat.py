"""Run wildcat on one project by hand (preprocess, assess, export), with per-stage timings.

    python pipeline/run_wildcat.py data/<project> [preprocess|assess|export ...]

pysheds 0.4 calls np.in1d, which NumPy 2 removed. Every call site passes an
already-ravelled array, so np.isin is an exact substitute. The shim has to be
installed before pysheds is imported, which is why this runs wildcat through its
Python API rather than the CLI.
"""

import sys
import time
from pathlib import Path

import numpy as np

if not hasattr(np, "in1d"):
    np.in1d = np.isin

import wildcat  # noqa: E402  - must follow the shim

ROOT = Path(__file__).resolve().parent.parent


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("preprocess", "assess", "export"):
        raise SystemExit("usage: python pipeline/run_wildcat.py data/<project> [preprocess|assess|export ...]")
    project = Path(args.pop(0))
    global PROJECT
    PROJECT = project
    print(f"project: {project}")

    stages = [("preprocess", wildcat.preprocess), ("assess", wildcat.assess),
              ("export", wildcat.export)]
    if args:
        stages = [s for s in stages if s[0] in args]

    timings = []
    for name, fn in stages:
        print(f"\n===== {name} =====", flush=True)
        start = time.perf_counter()
        fn(project)
        elapsed = time.perf_counter() - start
        timings.append((name, elapsed))
        print(f"-- {name}: {elapsed:.1f}s", flush=True)

    print("\n===== timings =====")
    for name, elapsed in timings:
        print(f"  {name:<12} {elapsed:8.1f}s")
    print(f"  {'TOTAL':<12} {sum(t for _, t in timings):8.1f}s")


if __name__ == "__main__":
    main()
