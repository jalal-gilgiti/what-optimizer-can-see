#include "postgres.h"
#include <time.h>
#include "fmgr.h"
#include "miscadmin.h"
#include "nodes/nodeFuncs.h"
#include "nodes/pathnodes.h"
#include "nodes/supportnodes.h"
#include "optimizer/cost.h"
#include "optimizer/optimizer.h"
#include "optimizer/paths.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/lsyscache.h"

PG_MODULE_MAGIC;
void _PG_init(void);
void _PG_fini(void);
PG_FUNCTION_INFO_V1(task15b_cost_support);
PG_FUNCTION_INFO_V1(task15b_reset_metrics);
PG_FUNCTION_INFO_V1(task15b_metrics);

enum { M1_GLOBAL = 0, M2_CONDITIONED = 1 };
static int representation_mode = M1_GLOBAL;
static int work_units_per_cost_unit = 400;
static set_rel_pathlist_hook_type previous_hook = NULL;
static uint64 support_calls = 0, support_elapsed_ns = 0, path_hook_calls = 0;
static double seq_startup=-1, seq_total=-1, seq_rows=-1;
static double idx_startup=-1, idx_total=-1, idx_rows=-1;
static double bmp_startup=-1, bmp_total=-1, bmp_rows=-1;

static const struct config_enum_entry mode_options[] = {
    {"M1_GLOBAL", M1_GLOBAL, false},
    {"M2_CONDITIONED", M2_CONDITIONED, false},
    {NULL, 0, false}
};

typedef struct { const char *population; } WalkerContext;
static bool population_walker(Node *node, void *context) {
    WalkerContext *ctx=(WalkerContext *)context;
    if (node==NULL || ctx->population!=NULL) return false;
    if (IsA(node, Const)) {
        Const *c=(Const *)node;
        if (!c->constisnull && c->consttype==TEXTOID) {
            char *v=TextDatumGetCString(c->constvalue);
            if (strcmp(v,"LOW")==0 || strcmp(v,"HIGH")==0) ctx->population=pstrdup(v);
            pfree(v);
        }
    }
    return expression_tree_walker(node,population_walker,context);
}
static const char *population_from_root(PlannerInfo *root) {
    WalkerContext ctx; ctx.population=NULL;
    if (root && root->parse && root->parse->jointree)
        population_walker(root->parse->jointree->quals,&ctx);
    return ctx.population ? ctx.population : "UNKNOWN";
}
static const char *mode_name(void) { return representation_mode==M2_CONDITIONED ? "M2_CONDITIONED" : "M1_GLOBAL"; }

static void path_hook(PlannerInfo *root, RelOptInfo *rel, Index rti, RangeTblEntry *rte) {
    ListCell *cell; Path seq;
    if (previous_hook) previous_hook(root,rel,rti,rte);
    if (!rte || rte->rtekind!=RTE_RELATION || !get_rel_name(rte->relid) || strcmp(get_rel_name(rte->relid),"e3_data")!=0) return;
    path_hook_calls++;
    memset(&seq,0,sizeof(seq)); seq.type=T_Path; seq.pathtype=T_SeqScan; seq.parent=rel; seq.pathtarget=rel->reltarget;
    cost_seqscan(&seq,root,rel,NULL); seq_startup=seq.startup_cost; seq_total=seq.total_cost; seq_rows=seq.rows;
    foreach(cell,rel->pathlist) {
        Path *p=(Path *)lfirst(cell);
        if (p->pathtype==T_IndexScan || p->pathtype==T_IndexOnlyScan) {
            if (idx_total<0 || p->total_cost<idx_total) { idx_startup=p->startup_cost; idx_total=p->total_cost; idx_rows=p->rows; }
        } else if (p->pathtype==T_BitmapHeapScan) {
            if (bmp_total<0 || p->total_cost<bmp_total) { bmp_startup=p->startup_cost; bmp_total=p->total_cost; bmp_rows=p->rows; }
        }
    }
}

void _PG_init(void) {
    DefineCustomEnumVariable("task15b.mode","Representation mode",NULL,&representation_mode,M1_GLOBAL,mode_options,PGC_USERSET,0,NULL,NULL,NULL);
    DefineCustomIntVariable("task15b.work_units_per_cost_unit","Conversion scale",NULL,&work_units_per_cost_unit,400,1,100000000,PGC_USERSET,0,NULL,NULL,NULL);
    EmitWarningsOnPlaceholders("task15b"); previous_hook=set_rel_pathlist_hook; set_rel_pathlist_hook=path_hook;
}
void _PG_fini(void) { set_rel_pathlist_hook=previous_hook; }

Datum task15b_cost_support(PG_FUNCTION_ARGS) {
    Node *raw=(Node *)PG_GETARG_POINTER(0);
    if (IsA(raw,SupportRequestCost)) {
        SupportRequestCost *request=(SupportRequestCost *)raw;
        const char *population=population_from_root(request->root);
        int units=505; struct timespec a,b; uint64 elapsed;
        clock_gettime(CLOCK_MONOTONIC,&a);
        if (representation_mode==M2_CONDITIONED) {
            if (strcmp(population,"LOW")==0) units=10;
            else if (strcmp(population,"HIGH")==0) units=1000;
        }
        request->startup=0.0;
        request->per_tuple=((Cost)units)/((Cost)work_units_per_cost_unit);
        clock_gettime(CLOCK_MONOTONIC,&b);
        elapsed=((uint64)(b.tv_sec-a.tv_sec)*UINT64CONST(1000000000))+((uint64)b.tv_nsec-(uint64)a.tv_nsec);
        support_calls++; support_elapsed_ns+=elapsed;
        elog(LOG,"TASK36_SUPPORT|pid=%d|mode=%s|population=%s|work_units=%d|scale=%d|per_tuple=%.9f|elapsed_ns=%llu",
             MyProcPid,mode_name(),population,units,work_units_per_cost_unit,request->per_tuple,(unsigned long long)elapsed);
        PG_RETURN_POINTER(request);
    }
    PG_RETURN_POINTER(NULL);
}
Datum task15b_reset_metrics(PG_FUNCTION_ARGS) {
    support_calls=support_elapsed_ns=path_hook_calls=0;
    seq_startup=seq_total=seq_rows=idx_startup=idx_total=idx_rows=bmp_startup=bmp_total=bmp_rows=-1;
    PG_RETURN_VOID();
}
Datum task15b_metrics(PG_FUNCTION_ARGS) {
    char *payload=psprintf("{\"support_calls\":%llu,\"support_elapsed_ns\":%llu,\"path_hook_calls\":%llu,\"scale\":%d,"
        "\"seq\":{\"startup\":%.9f,\"total\":%.9f,\"rows\":%.3f},"
        "\"index\":{\"startup\":%.9f,\"total\":%.9f,\"rows\":%.3f},"
        "\"bitmap\":{\"startup\":%.9f,\"total\":%.9f,\"rows\":%.3f}}",
        (unsigned long long)support_calls,(unsigned long long)support_elapsed_ns,(unsigned long long)path_hook_calls,work_units_per_cost_unit,
        seq_startup,seq_total,seq_rows,idx_startup,idx_total,idx_rows,bmp_startup,bmp_total,bmp_rows);
    PG_RETURN_TEXT_P(cstring_to_text(payload));
}
