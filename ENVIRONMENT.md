# Frozen execution environment

This is an index of recorded environments, not a claim that one environment applies identically to every mechanism.

| Component | Frozen value | Primary record |
|---|---|---|
| OS | Ubuntu 22.04.5 LTS | `metadata/e5-graceful-20260830T064548Z/graceful_environment.json` |
| Kernel | Linux 5.15.0-186-generic x86_64 | same; base details in `metadata/environment.json` |
| CPU | 14 reported Intel Xeon Gold 6226R cores in VMware guest | `metadata/environment.json` |
| RAM | 168,789,504,000 bytes recorded | `metadata/environment.json` |
| Python | 3.10.12 | per-study environment manifests |
| SQLite library | 3.37.2 | `metadata/environment.json`; Task 11 environment manifest |
| DuckDB controlled studies | 1.5.5 | `metadata/duckdb_environment.json` |
| DuckDB UDFBench ports | 1.0.0 | Task 2/11 environment manifests |
| PostgreSQL | 14.24, Ubuntu package `14.24-0ubuntu0.22.04.1` | `metadata/postgresql-20260829T084826Z/postgresql_environment.json` |
| PL/Python | `plpython3u`; package/context tied to PostgreSQL 14.24 and Python 3.10.12 | PostgreSQL environment/function metadata and setup report |
| PostgreSQL JIT | disabled | PostgreSQL and Task 11 environment manifests |
| PostgreSQL parallel gather/workers | disabled/zero | same |
| DuckDB official-small threads | 1 | `metadata/task11_udfbench_scale_validation/environment_manifest.json` |

## Source and data identities

| Artifact | Identity |
|---|---|
| UDFBench | commit `8da987566590e2534f24cd97bcfaa9157c24962c` |
| UDFBench official-small | DOI `10.5281/zenodo.14260428`; v1.0.0; archive MD5 `1f97ebe9d5041fa6d031c95e9691c3ed` |
| PRISM | commit `689902595ba2ec8f86cbd5eb952b5fde09906e50`; required DuckDB commit `dfb68aa3e8b504c1d19555f97035a5a00a1e2df4` |
| GRACEFUL | commit `df053f641722102f9f9a07162e58665a9ecfb40b` |
| GRACEFUL checkpoint | epoch 60; SHA-256 `105f756a80111c281de207f69d03bc4913574e1fa6bee8b01aea5906d0f70969` |
| GRACEFUL label normalizer | SHA-256 `15a4ff9117ad50bbb46a676869060ccdf5bdb2218abaf36db94550a55b5dc070` |
| GRACEFUL feature statistics | SHA-256 `d9ac1ee473e836a21a57588594c1f529a3989a896929353a7da40f5ecbc84375` |
| R3/MONSOON identity | repository commit `685f446790707fcbb0ce0e5b4b073e116a498eda`; optimizer source absent |

## Reproduction controls

PostgreSQL must use an isolated project-local cluster, private socket, non-default port, disabled JIT, and disabled parallel gather/workers. GRACEFUL requires its pinned Python environment and CPU configuration recorded in `graceful_environment.json`; set `GRACEFUL_DATA_ROOT` to the externally retrieved released data/checkpoint tree. PRISM requires a Unix-like C++20/CMake build. Exact package versions absent from the frozen record are intentionally not guessed.
