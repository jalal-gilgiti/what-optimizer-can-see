#!/usr/bin/env python3
"""Create or verify a SHA-256 snapshot of pre-existing experiment artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def roots() -> list[Path]:
    return [
        ROOT / "results",
        ROOT / "metadata",
        ROOT / "docs",
        ROOT / "workloads" / "synthetic" / "generated",
        ROOT / "runtime" / "postgresql",
    ]


def create_manifest(output: Path, run_id: str) -> dict[str, Any]:
    entries = []
    for base in roots():
        if not base.exists():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            entries.append({
                "path": str(path.relative_to(ROOT)),
                "size_bytes": path.stat().st_size,
                "sha256": digest(path),
            })
    payload = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "mode": "before",
        "roots": [str(path.relative_to(ROOT)) for path in roots()],
        "file_count": len(entries),
        "entries": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=False)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload


def verify_manifest(before: Path, output: Path, run_id: str) -> dict[str, Any]:
    source = json.loads(before.read_text(encoding="utf-8"))
    entries = []
    changed = []
    missing = []
    for expected in source["entries"]:
        path = ROOT / expected["path"]
        if not path.is_file():
            missing.append(expected["path"])
            entries.append({**expected, "status": "missing", "observed_sha256": None})
            continue
        observed = digest(path)
        status = "unchanged" if observed == expected["sha256"] else "changed"
        if status == "changed":
            changed.append(expected["path"])
        entries.append({
            **expected,
            "status": status,
            "observed_size_bytes": path.stat().st_size,
            "observed_sha256": observed,
        })
    payload = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "mode": "after",
        "source_manifest": str(before.resolve().relative_to(ROOT)),
        "file_count": len(entries),
        "unchanged_count": sum(item["status"] == "unchanged" for item in entries),
        "changed": changed,
        "missing": missing,
        "status": "PASS" if not changed and not missing else "FAIL",
        "entries": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    args = parser.parse_args()
    if args.mode == "create":
        payload = create_manifest(args.output, args.run_id)
    else:
        if args.before is None:
            raise SystemExit("--before is required for verify")
        payload = verify_manifest(args.before, args.output, args.run_id)
    print(json.dumps({key: payload[key] for key in payload if key in {"run_id", "mode", "file_count", "unchanged_count", "status"}}, sort_keys=True))
    if payload.get("status") == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
