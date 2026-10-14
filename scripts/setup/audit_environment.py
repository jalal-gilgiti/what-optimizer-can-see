#!/usr/bin/env python3
"""Create reproducible human- and machine-readable environment snapshots."""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str]) -> dict[str, Any]:
    executable = shutil.which(command[0])
    if executable is None:
        return {"available": False, "path": None, "output": None}
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        return {
            "available": True,
            "path": executable,
            "exit_code": completed.returncode,
            "output": completed.stdout.strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": True, "path": executable, "error": repr(exc)}


def first_line(result: dict[str, Any]) -> str | None:
    output = result.get("output")
    return output.splitlines()[0] if output else None


def module_version(name: str) -> dict[str, Any]:
    spec = importlib.util.find_spec(name)
    if spec is None:
        return {"available": False, "version": None, "path": None}
    try:
        module = importlib.import_module(name)
        return {
            "available": True,
            "version": getattr(module, "__version__", "unknown"),
            "path": spec.origin,
        }
    except Exception as exc:  # Record broken installations without aborting audit.
        return {
            "available": False,
            "version": None,
            "path": spec.origin,
            "import_error": repr(exc),
        }


def parse_lscpu() -> dict[str, str]:
    result = run(["lscpu"])
    if not result.get("output"):
        return {}
    parsed: dict[str, str] = {}
    for line in result["output"].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            parsed[key.strip()] = value.strip()
    return parsed


def meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            parts = raw.split()
            if parts:
                values[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        pass
    return values


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    lscpu = parse_lscpu()
    logical = os.cpu_count()
    cores_per_socket = lscpu.get("Core(s) per socket")
    sockets = lscpu.get("Socket(s)")
    physical = None
    if cores_per_socket and sockets:
        physical = int(cores_per_socket) * int(sockets)

    version_commands = {
        "python3": ["python3", "--version"],
        "java": ["java", "-version"],
        "javac": ["javac", "-version"],
        "gcc": ["gcc", "--version"],
        "clang": ["clang", "--version"],
        "cmake": ["cmake", "--version"],
        "docker": ["docker", "--version"],
        "podman": ["podman", "--version"],
        "git": ["git", "--version"],
        "postgres_client": ["psql", "--version"],
        "postgres_server": ["postgres", "--version"],
        "duckdb_cli": ["duckdb", "--version"],
        "sqlite_cli": ["sqlite3", "--version"],
        "spark_submit": ["spark-submit", "--version"],
        "monetdb": ["monetdb", "--version"],
        "monetdb_client": ["mclient", "--version"],
        "sqlserver_client": ["sqlcmd", "--version"],
    }
    tools = {name: run(command) for name, command in version_commands.items()}
    modules = {
        name: module_version(name)
        for name in ("duckdb", "pandas", "matplotlib", "numpy", "yaml", "pyspark")
    }

    import sqlite3

    storage = {
        "workspace_df": run(["df", "-B1", str(ROOT)]),
        "workspace_mount": run(["findmnt", "-T", str(ROOT), "-o", "SOURCE,FSTYPE,OPTIONS", "-n"]),
        "block_devices": run(["lsblk", "-d", "-o", "NAME,ROTA,TYPE,SIZE,MODEL,TRAN"]),
    }
    git_commit_result = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    git_commit = first_line(git_commit_result) if git_commit_result.get("exit_code") == 0 else None
    memory = meminfo()

    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "captured_at": now.isoformat(),
        "captured_at_utc": now.astimezone(dt.timezone.utc).isoformat(),
        "timezone": str(now.tzinfo),
        "experiment_root": str(ROOT),
        "git_commit": git_commit,
        "operating_system": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "hostname": platform.node(),
        },
        "cpu": {
            "model": lscpu.get("Model name"),
            "architecture": lscpu.get("Architecture"),
            "logical_cpus": logical,
            "physical_cores_reported": physical,
            "threads_per_core": lscpu.get("Thread(s) per core"),
            "sockets_reported": lscpu.get("Socket(s)"),
            "hypervisor": lscpu.get("Hypervisor vendor"),
            "note": "Virtual-machine topology may not represent host physical topology.",
        },
        "memory": {
            "total_bytes": memory.get("MemTotal"),
            "available_bytes": memory.get("MemAvailable"),
            "swap_total_bytes": memory.get("SwapTotal"),
        },
        "storage": storage,
        "tools": tools,
        "python_modules": modules,
        "sqlite_library_version": sqlite3.sqlite_version,
        "raw_lscpu": lscpu,
    }

    metadata_path = ROOT / "metadata" / "environment.json"
    metadata_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def tool_line(name: str) -> str:
        item = tools[name]
        if not item["available"]:
            return f"- {name}: not found"
        detail = first_line(item) or item.get("error") or "present; version unavailable"
        return f"- {name}: `{detail}` (`{item['path']}`)"

    module_lines = []
    for name, item in modules.items():
        if item["available"]:
            module_lines.append(f"- {name}: `{item['version']}` (`{item['path']}`)")
        else:
            suffix = f"; {item['import_error']}" if item.get("import_error") else ""
            module_lines.append(f"- {name}: unavailable{suffix}")

    mount_output = storage["workspace_mount"].get("output") or "unknown"
    df_output = storage["workspace_df"].get("output") or "unknown"
    doc = f"""# Environment audit

Captured at: `{report['captured_at']}`  
Experiment root: `{ROOT}`  
Git commit: `{git_commit or 'not a Git work tree'}`

## Operating system and compute

- Platform: `{report['operating_system']['platform']}`
- Kernel: `{platform.release()}`
- CPU: `{report['cpu']['model'] or 'unknown'}`
- Logical CPUs: `{logical if logical is not None else 'unknown'}`
- Physical cores reported to guest: `{physical if physical is not None else 'unknown'}`
- Hypervisor: `{report['cpu']['hypervisor'] or 'not reported'}`
- RAM: `{memory.get('MemTotal', 'unknown')}` bytes
- Available RAM at audit: `{memory.get('MemAvailable', 'unknown')}` bytes

The server is virtualized; physical-core and storage-device details describe what the guest can observe, not necessarily the host hardware.

## Storage

Workspace mount:

```text
{mount_output}
```

Workspace capacity at audit:

```text
{df_output}
```

The workspace is on NFS. Local `lsblk` data is retained verbatim in `metadata/environment.json`, but it does not describe the NFS server's physical media.

## Tools

{chr(10).join(tool_line(name) for name in version_commands)}

## Python modules and embedded databases

- SQLite library: `{sqlite3.sqlite_version}` through Python `{platform.python_version()}`
{chr(10).join(module_lines)}

## Interpretation

Availability here means the executable or import was observed. It does not imply that a service starts, a connector works, or an optimizer feature is supported. Spark entry points, for example, may be present while Java is absent. See `metadata/systems.json` for candidate-system classifications.
"""
    (ROOT / "docs" / "environment.md").write_text(doc, encoding="utf-8")
    print(f"wrote {metadata_path}")
    print(f"wrote {ROOT / 'docs' / 'environment.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
