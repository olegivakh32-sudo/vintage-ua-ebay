"""Start an isolated mobile preview without inheriting integration credentials."""

import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5000, type=int)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="vintage-mobile-") as data:
        env = {
            "APP_SECRET": secrets.token_hex(32),
            "XDG_RUNTIME_DIR": data,
            "ALLOW_PAID_AI": "0",
            "ENABLE_LIVE_PUBLISH": "0",
            "ACTIVE_LOT_CHECKPOINT_DIR": str(Path(data) / "checkpoints"),
            "HOLD_QUEUE_DIR": str(Path(data) / "hold"),
        }
        # Keep dependency lookup for workspace-local package installations.
        if "PYTHONPATH" in os.environ:
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        return subprocess.call(
            [sys.executable, "-m", "gunicorn", "--workers", "1", "--threads", "4",
             "--bind", f"{args.host}:{args.port}", "app:app"],
            cwd=Path(__file__).resolve().parent,
            env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
