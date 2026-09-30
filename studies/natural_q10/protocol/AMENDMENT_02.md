# Pre-measurement implementation amendment 02

The second invocation exported and loaded the frozen SF1 projection, then
stopped when PostgreSQL parsed the deterministic sample-construction query.
No estimate, experimental plan, semantic result, or timing was produced.

PostgreSQL requires the relation alias before its `TABLESAMPLE` clause. The
query was changed from `lineitem TABLESAMPLE ... l` to the syntactically
equivalent `lineitem l TABLESAMPLE ...`. No protocol or experimental parameter
changed.
