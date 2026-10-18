#!/usr/bin/env python3
import os
import zipfile
from pathlib import Path

root = Path("/home/workdir/artifacts")
out = root / "qr-claim-rewards-bot.zip"
skip = {".pyc", ".zip"}
skip_dirs = {"__pycache__", ".pytest_cache", "rendered", ".git"}

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            if name.endswith(".pyc") or name.endswith(".zip") or name == "make_zip.py":
                continue
            path = Path(dirpath) / name
            arc = path.relative_to(root)
            zf.write(path, arc)

print(f"Created {out} size={out.stat().st_size}")
