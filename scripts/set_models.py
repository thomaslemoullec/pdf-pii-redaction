#!/usr/bin/env python3
"""Set the model-selection keys in a Terraform tfvars file (used by `make models-write`).

Pure core (:func:`rewrite_tfvars`) + a tiny CLI, so the rewrite is unit-testable without
touching the filesystem. Only the four model keys are ever changed; every other line —
comments, IAP allowlist, project id — is left byte-for-byte.
"""

from __future__ import annotations

import pathlib
import re
import sys

MODEL_KEYS = ("gemini_location", "vision_model", "planner_model", "image_model")


def rewrite_tfvars(text: str, updates: dict[str, str]) -> str:
    """Return ``text`` with each key in ``updates`` set to its value.

    Replaces an existing ``key = "..."`` assignment in place (preserving position and
    every surrounding line/comment), or appends it if the key is absent. Keys not in
    ``updates`` are untouched.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*)=", line)
        if m and m.group(2) in updates:
            seen.add(m.group(2))
            out.append(f'{m.group(2)} = "{updates[m.group(2)]}"')
        else:
            out.append(line)
    missing = [k for k in updates if k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.extend(f'{k} = "{updates[k]}"' for k in missing)
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print("usage: set_models.py TFVARS LOCATION VISION PLANNER IMAGE", file=sys.stderr)
        return 2
    path, loc, vision, planner, image = argv[1:]
    updates = dict(zip(MODEL_KEYS, (loc, vision, planner, image)))
    p = pathlib.Path(path)
    before = p.read_text() if p.exists() else ""
    p.write_text(rewrite_tfvars(before, updates))
    for k, v in updates.items():
        print(f'  {k} = "{v}"')
    print(f"✅ wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
