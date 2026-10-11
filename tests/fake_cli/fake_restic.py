#!/usr/bin/env python3
"""Поддельный restic: дописывает вызов в FAKE_RESTIC_LOG и список файлов бэкапа."""

import json
import os
import sys
from pathlib import Path

entry = {"argv": sys.argv[1:], "repository": os.environ.get("RESTIC_REPOSITORY")}
if sys.argv[1:2] == ["backup"]:
    root = Path(sys.argv[2])
    entry["files"] = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
with open(os.environ["FAKE_RESTIC_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
sys.exit(int(os.environ.get("FAKE_RESTIC_EXIT", "0")))
