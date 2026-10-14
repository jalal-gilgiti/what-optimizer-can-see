/* Planner-support and exact-call instrumentation for the frozen PRISM/TPC-H Q10 study. */
#include "postgres.h"

#include <time.h>

#include "utils/datetime.h"
#include "fmgr.h"
#include "nodes/supportnodes.h"
#include "optimizer/cost.h"
#include "optimizer/optimizer.h"
#include "utils/builtins.h"
#include "utils/date.h"
#include "utils/guc.h"

PG_MODULE_MAGIC;

void _PG_init(void);

PG_FUNCTION_INFO_V1(q10_probe);
PG_FUNCTION_INFO_V1(q10_support);
PG_FUNCTION_INFO_V1(q10_reset_metrics);
PG_FUNCTION_INFO_V1(q10_metrics);

enum { MODE_R0 = 0, MODE_R1 = 1, MODE_R2 = 2 };

static int representation_mode = MODE_R0;
static double sampled_procost = 100.0;
static double sampled_selectivity = 0.01;
static double mapping_multiplier = 1.0;
static uint64 udf_calls = 0;
static uint64 support_cost_calls = 0;
static uint64 support_selectivity_calls = 0;
static uint64 support_elapsed_ns = 0;

static const struct config_enum_entry mode_options[] = {
	{"R0_OPAQUE", MODE_R0, false},
	{"R1_SCALAR", MODE_R1, false},
	{"R2_SAMPLED", MODE_R2, false},
	{NULL, 0, false}
};

void
_PG_init(void)
{
	DefineCustomEnumVariable("q10dc.mode", "Q10 representation mode", NULL,
							 &representation_mode, MODE_R0, mode_options,
							 PGC_USERSET, 0, NULL, NULL, NULL);
	DefineCustomRealVariable("q10dc.sampled_procost", "Sampled scalar procost", NULL,
							 &sampled_procost, 100.0, 0.000001, 100000000.0,
							 PGC_USERSET, 0, NULL, NULL, NULL);
	DefineCustomRealVariable("q10dc.sampled_selectivity", "Sampled Q10 selectivity", NULL,
							 &sampled_selectivity, 0.01, 0.0, 1.0,
							 PGC_USERSET, 0, NULL, NULL, NULL);
	DefineCustomRealVariable("q10dc.mapping_multiplier", "Cost mapping multiplier K_B", NULL,
							 &mapping_multiplier, 1.0, 0.000001, 1000000.0,
							 PGC_USERSET, 0, NULL, NULL, NULL);
	EmitWarningsOnPlaceholders("q10dc");
}

Datum
q10_probe(PG_FUNCTION_ARGS)
{
	DateADT order_date = PG_GETARG_DATEADT(0);
	BpChar *flag = PG_GETARG_BPCHAR_PP(1);
	char *bytes = VARDATA_ANY(flag);
	int length = VARSIZE_ANY_EXHDR(flag);
	DateADT start = date2j(1993, 10, 1) - POSTGRES_EPOCH_JDATE;
	DateADT end = date2j(1994, 1, 1) - POSTGRES_EPOCH_JDATE;
	bool result = length > 0 && bytes[0] == 'R' && order_date >= start && order_date < end;

	udf_calls++;
	PG_FREE_IF_COPY(flag, 1);
	PG_RETURN_BOOL(result);
}

Datum
q10_support(PG_FUNCTION_ARGS)
{
	Node *raw = (Node *) PG_GETARG_POINTER(0);
	struct timespec a, b;
	uint64 elapsed;

	if (representation_mode != MODE_R2)
		PG_RETURN_POINTER(NULL);

	clock_gettime(CLOCK_MONOTONIC, &a);
	if (IsA(raw, SupportRequestCost))
	{
		SupportRequestCost *request = (SupportRequestCost *) raw;
		request->startup = 0.0;
		request->per_tuple = sampled_procost * mapping_multiplier * cpu_operator_cost;
		support_cost_calls++;
	}
	else if (IsA(raw, SupportRequestSelectivity))
	{
		SupportRequestSelectivity *request = (SupportRequestSelectivity *) raw;
		request->selectivity = sampled_selectivity;
		support_selectivity_calls++;
	}
	else
		PG_RETURN_POINTER(NULL);

	clock_gettime(CLOCK_MONOTONIC, &b);
	elapsed = ((uint64) (b.tv_sec - a.tv_sec) * UINT64CONST(1000000000))
		+ ((uint64) b.tv_nsec - (uint64) a.tv_nsec);
	support_elapsed_ns += elapsed;
	PG_RETURN_POINTER(raw);
}

Datum
q10_reset_metrics(PG_FUNCTION_ARGS)
{
	udf_calls = 0;
	support_cost_calls = 0;
	support_selectivity_calls = 0;
	support_elapsed_ns = 0;
	PG_RETURN_VOID();
}

Datum
q10_metrics(PG_FUNCTION_ARGS)
{
	char *payload = psprintf(
		"{\"udf_calls\":%llu,\"support_cost_calls\":%llu,"
		"\"support_selectivity_calls\":%llu,\"support_elapsed_ns\":%llu}",
		(unsigned long long) udf_calls,
		(unsigned long long) support_cost_calls,
		(unsigned long long) support_selectivity_calls,
		(unsigned long long) support_elapsed_ns);
	PG_RETURN_TEXT_P(cstring_to_text(payload));
}
