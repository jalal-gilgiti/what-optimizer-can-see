/*
 * E5-DC decision-consequence bridge.
 *
 * Exposes procedural work to the PostgreSQL planner through SupportRequestCost,
 * under two representations:
 *
 *   M1_GLOBAL       one global work figure for every segment (collapsed)
 *   M2_CONDITIONED  the true per-segment work figure (query-conditioned)
 *
 * The only difference between the two modes is the number reported to the
 * planner. Data, statistics, indexes and GUCs are identical. No hints, no
 * forced paths, no disabled path types.
 *
 * The UDF itself counts its own invocations and accumulates the true work it
 * performs, so the consequence of the planner's decision is measured exactly
 * rather than inferred from the plan shape.
 */
#include "postgres.h"

#include <time.h>

#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/nodeFuncs.h"
#include "nodes/pathnodes.h"
#include "nodes/supportnodes.h"
#include "optimizer/cost.h"
#include "optimizer/optimizer.h"
#include "utils/builtins.h"
#include "utils/guc.h"

PG_MODULE_MAGIC;

void _PG_init(void);
void _PG_fini(void);

PG_FUNCTION_INFO_V1(e5dc_work);
PG_FUNCTION_INFO_V1(e5dc_cost_support);
PG_FUNCTION_INFO_V1(e5dc_reset_metrics);
PG_FUNCTION_INFO_V1(e5dc_metrics);

enum { M1_GLOBAL = 0, M2_CONDITIONED = 1 };

static int representation_mode = M1_GLOBAL;
static int work_units_per_cost_unit = 400;

/*
 * Reported work units. These are set from the generated data by the runner so
 * that M2 reports the truth and M1 reports the row-weighted global mean. M1 is
 * therefore the best single number a collapsed representation could report,
 * not a strawman.
 */
static int units_global = 505;
static int units_cheap = 10;
static int units_costly = 1000;

/* Exact execution-side accounting. */
static uint64 udf_calls = 0;
static uint64 udf_work_units = 0;
static uint64 support_calls = 0;
static uint64 support_elapsed_ns = 0;
static uint64 support_cheap_seen = 0;
static uint64 support_costly_seen = 0;
static uint64 support_unknown_seen = 0;

static const struct config_enum_entry mode_options[] = {
	{"M1_GLOBAL", M1_GLOBAL, false},
	{"M2_CONDITIONED", M2_CONDITIONED, false},
	{NULL, 0, false}
};

typedef struct
{
	const char *segment;
} SegmentWalkerContext;

/*
 * Find the segment constant the query is restricted to. Only 'CHEAP' and
 * 'COSTLY' are recognised; every other text constant in the query (customer
 * tier, region name) is ignored, so unrelated literals cannot be mistaken for
 * the segment.
 */
static bool
segment_walker(Node *node, void *context)
{
	SegmentWalkerContext *ctx = (SegmentWalkerContext *) context;

	if (node == NULL || ctx->segment != NULL)
		return false;

	if (IsA(node, Const))
	{
		Const *c = (Const *) node;

		if (!c->constisnull && c->consttype == TEXTOID)
		{
			char *v = TextDatumGetCString(c->constvalue);

			if (strcmp(v, "CHEAP") == 0 || strcmp(v, "COSTLY") == 0)
				ctx->segment = pstrdup(v);
			pfree(v);
		}
	}
	return expression_tree_walker(node, segment_walker, context);
}

static const char *
segment_from_root(PlannerInfo *root)
{
	SegmentWalkerContext ctx;

	ctx.segment = NULL;
	if (root && root->parse && root->parse->jointree)
		segment_walker((Node *) root->parse->jointree->quals, &ctx);
	return ctx.segment ? ctx.segment : "UNKNOWN";
}

static const char *
mode_name(void)
{
	return representation_mode == M2_CONDITIONED ? "M2_CONDITIONED" : "M1_GLOBAL";
}

void
_PG_init(void)
{
	DefineCustomEnumVariable("e5dc.mode",
							 "Procedural representation exposed to the planner",
							 NULL, &representation_mode, M1_GLOBAL, mode_options,
							 PGC_USERSET, 0, NULL, NULL, NULL);
	DefineCustomIntVariable("e5dc.work_units_per_cost_unit",
							"Work-to-cost conversion factor K",
							NULL, &work_units_per_cost_unit, 400, 1, 100000000,
							PGC_USERSET, 0, NULL, NULL, NULL);
	DefineCustomIntVariable("e5dc.units_global",
							"Work units M1 reports for every segment",
							NULL, &units_global, 505, 1, 100000000,
							PGC_USERSET, 0, NULL, NULL, NULL);
	DefineCustomIntVariable("e5dc.units_cheap",
							"Work units M2 reports for the CHEAP segment",
							NULL, &units_cheap, 10, 1, 100000000,
							PGC_USERSET, 0, NULL, NULL, NULL);
	DefineCustomIntVariable("e5dc.units_costly",
							"Work units M2 reports for the COSTLY segment",
							NULL, &units_costly, 1000, 1, 100000000,
							PGC_USERSET, 0, NULL, NULL, NULL);
	EmitWarningsOnPlaceholders("e5dc");
}

void
_PG_fini(void)
{
}

/*
 * The measured UDF. Performs loop_count units of real arithmetic work and
 * records both the invocation and the work performed.
 */
Datum
e5dc_work(PG_FUNCTION_ARGS)
{
	int64 value = PG_GETARG_INT64(0);
	int64 loops = PG_GETARG_INT64(1);
	int64 acc = value;
	int64 i;

	if (loops < 0)
		loops = 0;

	for (i = 0; i < loops; i++)
	{
		acc = acc + ((acc ^ (i + 1)) % 7) - 3;
		if (acc < 0)
			acc = -acc;
	}

	udf_calls++;
	udf_work_units += (uint64) loops;

	PG_RETURN_INT64(acc % 1000);
}

Datum
e5dc_cost_support(PG_FUNCTION_ARGS)
{
	Node *raw = (Node *) PG_GETARG_POINTER(0);

	if (IsA(raw, SupportRequestCost))
	{
		SupportRequestCost *request = (SupportRequestCost *) raw;
		const char *segment = segment_from_root(request->root);
		int units = units_global;
		struct timespec a, b;
		uint64 elapsed;

		clock_gettime(CLOCK_MONOTONIC, &a);

		if (representation_mode == M2_CONDITIONED)
		{
			if (strcmp(segment, "CHEAP") == 0)
				units = units_cheap;
			else if (strcmp(segment, "COSTLY") == 0)
				units = units_costly;
		}

		if (strcmp(segment, "CHEAP") == 0)
			support_cheap_seen++;
		else if (strcmp(segment, "COSTLY") == 0)
			support_costly_seen++;
		else
			support_unknown_seen++;

		request->startup = 0.0;
		request->per_tuple = ((Cost) units) / ((Cost) work_units_per_cost_unit);

		clock_gettime(CLOCK_MONOTONIC, &b);
		elapsed = ((uint64) (b.tv_sec - a.tv_sec) * UINT64CONST(1000000000))
			+ ((uint64) b.tv_nsec - (uint64) a.tv_nsec);
		support_calls++;
		support_elapsed_ns += elapsed;

		elog(LOG, "E5DC_SUPPORT|pid=%d|mode=%s|segment=%s|work_units=%d|K=%d|per_tuple=%.9f|elapsed_ns=%llu",
			 MyProcPid, mode_name(), segment, units, work_units_per_cost_unit,
			 request->per_tuple, (unsigned long long) elapsed);

		PG_RETURN_POINTER(request);
	}
	PG_RETURN_POINTER(NULL);
}

Datum
e5dc_reset_metrics(PG_FUNCTION_ARGS)
{
	udf_calls = 0;
	udf_work_units = 0;
	support_calls = 0;
	support_elapsed_ns = 0;
	support_cheap_seen = 0;
	support_costly_seen = 0;
	support_unknown_seen = 0;
	PG_RETURN_VOID();
}

Datum
e5dc_metrics(PG_FUNCTION_ARGS)
{
	char *payload = psprintf("{\"udf_calls\":%llu,\"udf_work_units\":%llu,"
							 "\"support_calls\":%llu,\"support_elapsed_ns\":%llu,"
							 "\"segment_seen\":{\"CHEAP\":%llu,\"COSTLY\":%llu,\"UNKNOWN\":%llu},"
							 "\"mode\":\"%s\",\"K\":%d,"
							 "\"units\":{\"global\":%d,\"cheap\":%d,\"costly\":%d}}",
							 (unsigned long long) udf_calls,
							 (unsigned long long) udf_work_units,
							 (unsigned long long) support_calls,
							 (unsigned long long) support_elapsed_ns,
							 (unsigned long long) support_cheap_seen,
							 (unsigned long long) support_costly_seen,
							 (unsigned long long) support_unknown_seen,
							 mode_name(), work_units_per_cost_unit,
							 units_global, units_cheap, units_costly);

	PG_RETURN_TEXT_P(cstring_to_text(payload));
}
