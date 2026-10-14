.timer off
SET threads=1;
LOAD 'runtime/prism/task4-prism-20260903T022253Z/generated_extensions/q6/udf1.duckdb_extension';
CREATE OR REPLACE MACRO q6conditions(shipdate, discount, qty) AS q6conditions_outlined_0(discount::DECIMAL(12,2), qty::INTEGER, shipdate);
.mode csv
.headers off
.print TASK4_RESULT|tpch_q6|PRISM_SELECTIVE
SELECT SUM(l_extendedprice*l_discount) FROM lineitem WHERE q6conditions(l_shipdate,l_discount,l_quantity)=1;
.print TASK4_RESULT|tpch_q6|FULL_NATIVE_SQL
SELECT SUM(l_extendedprice*l_discount) FROM lineitem WHERE l_shipdate>=DATE '1994-01-01' AND l_shipdate<DATE '1994-01-01'+INTERVAL '1' YEAR AND l_discount BETWEEN .06-.01 AND .06+.01 AND l_quantity<24;
.timer on
.print TASK4_TRIAL|tpch_q6|PRISM_SELECTIVE|probe
SELECT SUM(l_extendedprice*l_discount) FROM lineitem WHERE q6conditions(l_shipdate,l_discount,l_quantity)=1;
.timer off
