# Third-party components

Third-party source trees, binaries, checkpoints, and large benchmark payloads
are not redistributed in this archive. The recorded identities and hashes
allow them to be obtained from their canonical sources.

| Component | Canonical source / identity | License or distribution note |
|---|---|---|
| UDFBench | `https://github.com/athenarc/UDFBench`, commit prefix `8da987` | GPL-3.0 upstream; official-small data DOI `10.5281/zenodo.14260428` (CC-BY-4.0 metadata recorded in `metadata/task11_udfbench_scale_validation/`) |
| PRISM | `https://github.com/SamArch27/PRISM`, commit `689902595ba2ec8f86cbd5eb952b5fde09906e50`; modified DuckDB `https://github.com/hkulyc/duckdb.git`, commit `dfb68aa3e8b504c1d19555f97035a5a00a1e2df4` | obtain from upstream and preserve its notices; identities are in `metadata/task4_prism_strategic_opacity/` |
| TPC-H DBGen | bundled in the pinned modified DuckDB source above; `extension/tpch/dbgen/LICENSE` | TPC EULA applies; the artifact supplies an acquisition/build/generation script and does not redistribute generated rows |
| GRACEFUL | canonical GRACEFUL repository, commit `df053f641722`; checkpoint SHA-256 prefix `105f756a8011` | Apache-2.0 source; checkpoint and normalizer hashes are in the GRACEFUL metadata |
| PostgreSQL | PostgreSQL 14.24 | install through the operating-system package channel |
| DuckDB | versions 1.5.5 and 1.0.0 as recorded by each study | install from the recorded requirements or upstream release |

No third-party paper PDFs, local virtual environments, database clusters, or
vendor binaries are included.
