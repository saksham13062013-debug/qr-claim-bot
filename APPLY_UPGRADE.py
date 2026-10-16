#!/usr/bin/env python3
"""
Apply latest QR Claim Bot upgrades into an existing project directory.
Usage:
  python APPLY_UPGRADE.py /path/to/your/project
Or run from project root:
  python APPLY_UPGRADE.py .
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# This script only works when the full source tree is present next to it
# (as in the artifacts folder). Prefer full zip when available.

SRC = Path(__file__).resolve().parent
FILES = [
    "app/bot.py",
    "app/api.py",
    "app/main.py",
    "app/models.py",
    "app/db.py",
    "app/config.py",
    "app/services/withdrawal_service.py",
    "app/services/claim_service.py",
    "app/services/notification_service.py",
    "app/static/js/admin.js",
    "app/static/js/login.js",
    "app/templates/admin.html",
    "requirements.txt",
]


def main() -> None:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    if not (dest / "app").is_dir():
        print("ERROR: target must be project root containing app/")
        sys.exit(1)
    copied = 0
    for rel in FILES:
        s = SRC / rel
        d = dest / rel
        if not s.exists():
            print(f"SKIP missing source: {rel}")
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        print(f"OK {rel}")
        copied += 1
    print(f"\nCopied {copied} files into {dest}")
    print("Redeploy on Railway after this.")


if __name__ == "__main__":
    main()
