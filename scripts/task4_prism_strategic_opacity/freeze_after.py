#!/usr/bin/env python3
"""Compare Task-4 protected artifacts with the pre-execution manifest."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
META = ROOT / "metadata/task4_prism_strategic_opacity"
BEFORE = META / "frozen_artifacts_before.json"
AFTER = META / "frozen_artifacts_after.json"

EXPECTED_PREFIXES = (
    "docs/task4_prism_",
    "results/task4_prism_strategic_opacity/",
    "metadata/task4_prism_strategic_opacity/",
    "workloads/task4_prism_strategic_opacity/",
    "scripts/task4_prism_strategic_opacity/",
    "raw/task4_prism_strategic_opacity/",
    "plans/task4_prism_strategic_opacity/",
)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    st = path.stat()
    return {"sha256": h.hexdigest(), "size_bytes": st.st_size, "mode": oct(st.st_mode & 0o777)}


def iter_protected(roots):
    seen = set()
    for relroot in roots:
        target = ROOT / relroot
        if target.is_file():
            candidates = [target]
        elif target.is_dir():
            candidates = (p for p in target.rglob("*") if p.is_file() and not p.is_symlink())
        else:
            candidates = []
        for path in candidates:
            rel = path.relative_to(ROOT).as_posix()
            if rel not in seen:
                seen.add(rel)
                yield rel, path


def main():
    before = json.loads(BEFORE.read_text())
    baseline = {x["path"]: x for x in before["files"]}
    unchanged, changed, missing = [], [], []
    for rel, old in baseline.items():
        path = ROOT / rel
        if not path.is_file():
            missing.append(rel)
            continue
        new = digest(path)
        if new == {k: old[k] for k in ("sha256", "size_bytes", "mode")}:
            unchanged.append(rel)
        else:
            changed.append({"path": rel, "before": {k: old[k] for k in ("sha256", "size_bytes", "mode")}, "after": new})

    expected_additions, unexpected_additions = [], []
    for rel, path in iter_protected(before["protected_roots"]):
        if rel in baseline or rel.endswith("frozen_artifacts_before.json") or rel.endswith("frozen_artifacts_after.json"):
            continue
        item = {"path": rel, **digest(path)}
        if rel.startswith(EXPECTED_PREFIXES):
            expected_additions.append(item)
        else:
            unexpected_additions.append(item)

    result = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "before_manifest": "metadata/task4_prism_strategic_opacity/frozen_artifacts_before.json",
        "protected_before": len(baseline),
        "unchanged": len(unchanged),
        "changed": len(changed),
        "missing": len(missing),
        "expected_task4_additions": len(expected_additions),
        "unexpected_additions_in_protected_paths": len(unexpected_additions),
        "verification": "PASS" if not changed and not missing and not unexpected_additions else "FAIL",
        "changed_files": changed,
        "missing_files": missing,
        "unexpected_additions": unexpected_additions,
        "expected_additions": expected_additions,
    }
    AFTER.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: result[k] for k in ("protected_before", "unchanged", "changed", "missing", "expected_task4_additions", "unexpected_additions_in_protected_paths", "verification")}, indent=2))


if __name__ == "__main__":
    main()
