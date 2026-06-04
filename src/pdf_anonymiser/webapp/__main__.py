"""``python -m pdf_anonymiser.webapp`` — serve the review UI on :8080."""

from __future__ import annotations

import os

import uvicorn

from .app import create_app

if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )
