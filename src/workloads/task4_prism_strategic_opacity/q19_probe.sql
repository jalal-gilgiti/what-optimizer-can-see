.timer off
SET threads=1;
LOAD 'runtime/prism/task4-prism-20260903T022253Z/generated_extensions/q19/udf1.duckdb_extension';
CREATE OR REPLACE MACRO q19conditions(pcontainer, lqty, psize, shipmode, shipinst, pbrand) AS q19conditions_outlined_0(lqty::DECIMAL(15,2), pbrand, pcontainer, psize::INTEGER, shipinst, shipmode);
.mode csv
.headers off
.print TASK4_RESULT|tpch_q19|PRISM_SELECTIVE
SELECT SUM(l_extendedprice*(1-l_discount)) FROM lineitem JOIN part ON l_partkey=p_partkey WHERE q19conditions(p_container,l_quantity,p_size,l_shipmode,l_shipinstruct,p_brand)=1;
.print TASK4_RESULT|tpch_q19|FULL_NATIVE_SQL
SELECT SUM(l_extendedprice*(1-l_discount)) FROM lineitem,part WHERE (p_partkey=l_partkey AND p_brand='Brand#12' AND p_container IN ('SM CASE','SM BOX','SM PACK','SM PKG') AND l_quantity>=1 AND l_quantity<=11 AND p_size BETWEEN 1 AND 5 AND l_shipmode IN ('AIR','AIR REG') AND l_shipinstruct='DELIVER IN PERSON') OR (p_partkey=l_partkey AND p_brand='Brand#23' AND p_container IN ('MED BAG','MED BOX','MED PKG','MED PACK') AND l_quantity>=10 AND l_quantity<=20 AND p_size BETWEEN 1 AND 10 AND l_shipmode IN ('AIR','AIR REG') AND l_shipinstruct='DELIVER IN PERSON') OR (p_partkey=l_partkey AND p_brand='Brand#34' AND p_container IN ('LG CASE','LG BOX','LG PACK','LG PKG') AND l_quantity>=20 AND l_quantity<=30 AND p_size BETWEEN 1 AND 15 AND l_shipmode IN ('AIR','AIR REG') AND l_shipinstruct='DELIVER IN PERSON');
.timer on
.print TASK4_TRIAL|tpch_q19|PRISM_SELECTIVE|probe
SELECT SUM(l_extendedprice*(1-l_discount)) FROM lineitem JOIN part ON l_partkey=p_partkey WHERE q19conditions(p_container,l_quantity,p_size,l_shipmode,l_shipinstruct,p_brand)=1;
.timer off
