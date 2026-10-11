#!/usr/bin/env python3
"""Поддельный CLI агента для тестов адаптеров.

FAKE_RECORD — куда записать argv и переменные окружения; FAKE_OUTPUT — файл со строками stdout;
FAKE_STDERR — текст для stderr; FAKE_EXIT — код выхода; FAKE_SLEEP — пауза перед выходом.
"""

import json
import os
import sys
import time
from pathlib import Path

record = os.environ.get("FAKE_RECORD")
if record:
    env_keys = ("DAYFORGE_MCP_TOKEN", "MAX_THINKING_TOKENS")
    Path(record).write_text(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "env": {k: os.environ.get(k) for k in env_keys},
                "cwd": os.getcwd(),
            }
        )
    )
output = os.environ.get("FAKE_OUTPUT")
if output:
    for line in Path(output).read_text().splitlines():
        print(line, flush=True)
if os.environ.get("FAKE_STDERR"):
    print(os.environ["FAKE_STDERR"], file=sys.stderr, flush=True)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
