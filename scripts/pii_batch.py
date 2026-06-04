#!/usr/bin/env python3
"""Thin wrapper around the batch shard runner — kept for `python scripts/pii_batch.py`.

The real entry point is ``pdf_anonymiser.batch_runner.main`` (also reachable as
``pdf-anonymise batch``); this lets you run a shard from a source checkout without
installing the package.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pdf_anonymiser.batch_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
