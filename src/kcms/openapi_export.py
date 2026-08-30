"""Exports the backend-owned OpenAPI artifact with deterministic bytes."""

from __future__ import annotations

import json
from pathlib import Path

from kcms.app import create_app

ARTIFACT_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


def render_artifact() -> str:
    schema = create_app().openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_artifact() -> Path:
    ARTIFACT_PATH.write_text(render_artifact(), encoding="utf-8")
    return ARTIFACT_PATH


if __name__ == "__main__":
    print(f"wrote {write_artifact()}")
