"""Quick end-to-end smoke: seed → offline demo → eval → offline pipeline (verify)."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    p = subprocess.run(cmd, cwd=cwd, shell=False)
    return p.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="GraphSleuth end-to-end smoke test")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--target", default="ACC-9003")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = sys.executable

    steps = [
        [str(python), "-m", "graphsleuth", "seed", "--tenant", args.tenant],
        [str(python), "-m", "graphsleuth", "demo", "--offline", "--target", args.target],
        [str(python), "-m", "graphsleuth", "eval"],
        [
            str(python),
            "-m",
            "graphsleuth",
            "pipeline",
            "--offline",
            "--verify",
            "--target",
            args.target,
            "--quiet",
        ],
        [str(python), "tests/test_offline.py"],
    ]

    for cmd in steps:
        rc = run(cmd, root)
        if rc != 0:
            print(f"FAIL: rc={rc} for {' '.join(cmd)}", file=sys.stderr)
            return rc
    print("SMOKE OK: seed + demo(offline) + eval + pipeline(offline,verify) + offline tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
