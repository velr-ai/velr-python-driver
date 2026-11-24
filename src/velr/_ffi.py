# velrpy/src/velrpy/_ffi.py
from __future__ import annotations
import os
import sys
import ctypes
from pathlib import Path
from contextlib import contextmanager, suppress
import importlib.resources as ir
from cffi import FFI

ffi = FFI()

# ---------------- C declarations (drop-in replacement) ----------------
ffi.cdef(r"""
/* --------------------------------------------------------------------- */
/* Basic fixed-width aliases used by the public header                    */
/* --------------------------------------------------------------------- */
typedef signed long long int64_t;
typedef signed int       int32_t;
typedef unsigned int     uint32_t;
typedef unsigned long long uint64_t;
typedef unsigned char    uint8_t;

/* --------------------------------------------------------------------- */
/* Opaque handles                                                        */
/* --------------------------------------------------------------------- */
typedef struct velr_db            velr_db;
typedef struct velr_stream        velr_stream;
typedef struct velr_table         velr_table;
typedef struct velr_rows          velr_rows;
typedef struct velr_tx            velr_tx;
typedef struct velr_sp            velr_sp;
typedef struct velr_stream_tx     velr_stream_tx;
typedef struct velr_explain_trace velr_explain_trace;
typedef struct velr_query_params  velr_query_params;
typedef struct velr_query_control velr_query_control;

/* --------------------------------------------------------------------- */
/* Status codes                                                          */
/* --------------------------------------------------------------------- */
typedef enum velr_code {
  VELR_OK     = 0,
  VELR_EARG   = -1,
  VELR_EUTF   = -2,
  VELR_ESTATE = -3,
  VELR_EERR   = -4,
  VELR_ECANCELLED = -5,
  VELR_EDEADLINE = -6
} velr_code;

/* --------------------------------------------------------------------- */
/* Cells / strings                                                       */
/* --------------------------------------------------------------------- */
typedef enum velr_cell_type {
  VELR_NULL   = 0,
  VELR_BOOL   = 1,
  VELR_INT64  = 2,
  VELR_DOUBLE = 3,
  VELR_TEXT   = 4,
  VELR_JSON   = 5
} velr_cell_type;

typedef struct velr_cell {
  velr_cell_type  ty;
  int64_t         i64_;
  double          f64_;
  const uint8_t*  ptr;
  size_t          len;
} velr_cell;

typedef struct velr_strview {
  const uint8_t* ptr;
  size_t         len;
} velr_strview;

typedef struct velr_query_options {
  int      has_max_result_rows;
  size_t   max_result_rows;
  const velr_query_params* params;
} velr_query_options;

velr_query_control* velr_query_control_new(uint64_t timeout_ns);
void velr_query_control_cancel(velr_query_control* control);
void velr_query_control_free(velr_query_control* control);

typedef struct velr_param_map_entry {
  size_t       row_index;
  velr_strview key;
  velr_cell    value;
} velr_param_map_entry;

typedef enum velr_migration_status {
  VELR_MIGRATION_ALREADY_CURRENT = 0,
  VELR_MIGRATION_MIGRATED        = 1
} velr_migration_status;

typedef struct velr_migration_report {
  int32_t                from_version;
  int32_t                to_version;
  velr_migration_status status;
  size_t                 step_count;
  char*                  steps;
} velr_migration_report;

typedef enum velr_vector_embedding_purpose {
  VELR_VECTOR_EMBEDDING_INDEX_ENTITY = 0,
  VELR_VECTOR_EMBEDDING_QUERY        = 1
} velr_vector_embedding_purpose;

typedef enum velr_vector_entity_kind {
  VELR_VECTOR_ENTITY_NONE         = 0,
  VELR_VECTOR_ENTITY_NODE         = 1,
  VELR_VECTOR_ENTITY_RELATIONSHIP = 2
} velr_vector_entity_kind;

typedef enum velr_property_value_type {
  VELR_PROPERTY_NULL            = 0,
  VELR_PROPERTY_BOOL            = 1,
  VELR_PROPERTY_INT64           = 2,
  VELR_PROPERTY_DOUBLE          = 3,
  VELR_PROPERTY_STRING          = 4,
  VELR_PROPERTY_DATE            = 5,
  VELR_PROPERTY_LOCAL_TIME      = 6,
  VELR_PROPERTY_ZONED_TIME      = 7,
  VELR_PROPERTY_LOCAL_DATETIME  = 8,
  VELR_PROPERTY_ZONED_DATETIME  = 9,
  VELR_PROPERTY_DURATION        = 10,
  VELR_PROPERTY_POINT           = 11,
  VELR_PROPERTY_GEOMETRY        = 12,
  VELR_PROPERTY_GEOGRAPHY       = 13,
  VELR_PROPERTY_LIST            = 14,
  VELR_PROPERTY_VECTOR          = 15,
  VELR_PROPERTY_BYTES           = 16
} velr_property_value_type;

typedef enum velr_storage_value_type {
  VELR_STORAGE_NULL   = 0,
  VELR_STORAGE_INT64  = 1,
  VELR_STORAGE_DOUBLE = 2,
  VELR_STORAGE_TEXT   = 3,
  VELR_STORAGE_BLOB   = 4
} velr_storage_value_type;

typedef struct velr_vector_embedding_field {
  int                      has_name;
  velr_strview             name;
  velr_property_value_type value_type;
  velr_storage_value_type  storage_type;
  int64_t                  i64_;
  double                   f64_;
  velr_strview             bytes;
  velr_strview             json;
  velr_strview             display;
} velr_vector_embedding_field;

typedef struct velr_vector_embedding_input {
  velr_strview                       index_name;
  size_t                             dimensions;
  velr_vector_embedding_purpose      purpose;
  velr_vector_entity_kind            entity_kind;
  int                                has_entity_id;
  int64_t                            entity_id;
  const velr_vector_embedding_field* fields;
  size_t                             field_count;
} velr_vector_embedding_input;

typedef velr_code (*velr_vector_embedder_callback)(
    void* user_data,
    const velr_vector_embedding_input* inputs,
    size_t input_count,
    size_t dimensions,
    float* out_vectors,
    char* err_buf,
    size_t err_buf_len);

typedef void (*velr_vector_embedder_free_callback)(void* user_data);

/* --------------------------------------------------------------------- */
/* EXPLAIN metadata PODs                                                 */
/* --------------------------------------------------------------------- */
typedef struct velr_explain_plan_meta {
  velr_strview plan_id;
  velr_strview cypher;
  size_t       step_count;
} velr_explain_plan_meta;

typedef struct velr_explain_step_meta {
  size_t       step_no;
  velr_strview group_id;
  velr_strview op_index;
  velr_strview phase;
  velr_strview title;
  velr_strview source;
  velr_strview note;
  size_t       statement_count;
} velr_explain_step_meta;

typedef struct velr_explain_stmt_meta {
  velr_strview stmt_id;
  velr_strview kind;
  velr_strview sql;
  velr_strview note;
  size_t       sqlite_plan_count;
} velr_explain_stmt_meta;

/* --------------------------------------------------------------------- */
/* Memory helpers                                                        */
/* --------------------------------------------------------------------- */
void velr_string_free(char* s);
char* velr_error_details_json(const char* error);
void velr_free(uint8_t* p, size_t len);

/* --------------------------------------------------------------------- */
/* DB                                                                    */
/* --------------------------------------------------------------------- */
velr_code velr_open(const char* path_or_null, velr_db** out_db, char** out_err);
typedef struct velr_extension_config {
    const char* path;
    size_t memory_limit_bytes;
} velr_extension_config;
velr_code velr_open_with_extensions(
    const char* path_or_null,
    const velr_extension_config* extensions,
    size_t extension_count,
    velr_db** out_db,
    char** out_err);
velr_code velr_create(
    const char* path_or_null,
    const char* const* inline_edge_properties,
    size_t inline_edge_properties_count,
    velr_db** out_db,
    char** out_err);
velr_code velr_inline_edge_properties_json(velr_db* db, char** out_json, char** out_err);
velr_code velr_open_existing_readonly(const char* path, velr_db** out_db, char** out_err);
void      velr_close(velr_db* db);
velr_code velr_register_vector_embedder(
    velr_db* db,
    velr_strview name,
    velr_vector_embedder_callback callback,
    void* user_data,
    velr_vector_embedder_free_callback free_user_data,
    char** out_err);

velr_query_params* velr_query_params_new(void);
void velr_query_params_free(velr_query_params* params);
velr_code velr_query_params_set_null(
    velr_query_params* params,
    velr_strview name,
    char** out_err);
velr_code velr_query_params_set_bool(
    velr_query_params* params,
    velr_strview name,
    int value,
    char** out_err);
velr_code velr_query_params_set_i64(
    velr_query_params* params,
    velr_strview name,
    int64_t value,
    char** out_err);
velr_code velr_query_params_set_f64(
    velr_query_params* params,
    velr_strview name,
    double value,
    char** out_err);
velr_code velr_query_params_set_text(
    velr_query_params* params,
    velr_strview name,
    velr_strview value,
    char** out_err);
velr_code velr_query_params_set_json(
    velr_query_params* params,
    velr_strview name,
    velr_strview json_value,
    char** out_err);
velr_code velr_query_param_row_map_required_columns(
    velr_strview cypher,
    velr_strview param_name,
    char** out_json,
    char** out_err);
velr_code velr_query_params_set_row_maps(
    velr_query_params* params,
    velr_strview name,
    size_t row_count,
    const velr_param_map_entry* entries,
    size_t entry_count,
    char** out_err);
velr_code velr_query_params_set_row_map_columns(
    velr_query_params* params,
    velr_strview name,
    size_t row_count,
    const velr_strview* colnames,
    size_t col_count,
    const velr_cell* cells,
    const uint8_t* present,
    char** out_err);
velr_code velr_query_params_set_row_map_packed_columns(
    velr_query_params* params,
    velr_strview name,
    size_t row_count,
    const velr_strview* colnames,
    size_t col_count,
    const velr_cell_type* cell_types,
    const int64_t* i64_values,
    const double* f64_values,
    const size_t* bytes_offsets,
    const size_t* bytes_lens,
    const uint8_t* bytes,
    size_t bytes_len,
    const uint8_t* present,
    char** out_err);
velr_code velr_query_params_set_row_map_packed_columns_u32(
    velr_query_params* params,
    velr_strview name,
    size_t row_count,
    const velr_strview* colnames,
    size_t col_count,
    const velr_cell_type* cell_types,
    const int64_t* i64_values,
    const double* f64_values,
    const uint32_t* bytes_offsets,
    const uint32_t* bytes_lens,
    const uint8_t* bytes,
    size_t bytes_len,
    const uint8_t* present,
    char** out_err);
velr_code velr_query_params_set_row_map_packed_columns_u8_u32(
    velr_query_params* params,
    velr_strview name,
    size_t row_count,
    const velr_strview* colnames,
    size_t col_count,
    const uint8_t* cell_types,
    const int64_t* i64_values,
    const double* f64_values,
    const uint32_t* bytes_offsets,
    const uint32_t* bytes_lens,
    const uint8_t* bytes,
    size_t bytes_len,
    const uint8_t* present,
    char** out_err);

velr_code velr_schema_version(velr_db* db, int32_t* out_version, char** out_err);
velr_code velr_current_schema_version(velr_db* db, int32_t* out_version, char** out_err);
velr_code velr_needs_migration(velr_db* db, int* out_needs_migration, char** out_err);
velr_code velr_migrate(velr_db* db, velr_migration_report* out_report, char** out_err);
velr_code velr_vacuum(velr_db* db, char** out_err);
void      velr_migration_report_clear(velr_migration_report* report);

/* --------------------------------------------------------------------- */
/* Exec outside tx                                                       */
/* --------------------------------------------------------------------- */
velr_code velr_exec_start(
    velr_db* db,
    const char* cypher,
    velr_stream** out_stream,
    char** out_err);

velr_code velr_exec_start_with_options(
    velr_db* db,
    const char* cypher,
    const velr_query_options* opts,
    velr_stream** out_stream,
    char** out_err);

velr_code velr_exec_start_with_control(
    velr_db* db, const char* cypher, const velr_query_options* opts,
    const velr_query_control* control, velr_stream** out_stream, char** out_err);

velr_code velr_run(
    velr_db* db,
    const char* cypher,
    char** out_err);

velr_code velr_run_with_options(
    velr_db* db,
    const char* cypher,
    const velr_query_options* opts,
    char** out_err);

velr_code velr_run_with_control(
    velr_db* db, const char* cypher, const velr_query_options* opts,
    const velr_query_control* control, char** out_err);

velr_code velr_exec_one(
    velr_db* db,
    const char* cypher,
    velr_table** out_table,
    char** out_err);

velr_code velr_exec_one_with_options(
    velr_db* db,
    const char* cypher,
    const velr_query_options* opts,
    velr_table** out_table,
    char** out_err);

velr_code velr_exec_one_with_control(
    velr_db* db, const char* cypher, const velr_query_options* opts,
    const velr_query_control* control, velr_table** out_table, char** out_err);

velr_code velr_stream_next_table(
    velr_stream* stream,
    velr_table** out_table,
    int* out_has,
    char** out_err);

void velr_exec_close(velr_stream* stream);

/* --------------------------------------------------------------------- */
/* EXPLAIN / EXPLAIN ANALYZE                                             */
/* --------------------------------------------------------------------- */
velr_code velr_explain(
    velr_db* db,
    const char* cypher,
    velr_explain_trace** out_explain,
    char** out_err);

velr_code velr_explain_analyze(
    velr_db* db,
    const char* cypher,
    velr_explain_trace** out_explain,
    char** out_err);

velr_code velr_tx_explain(
    velr_tx* tx,
    const char* cypher,
    velr_explain_trace** out_explain,
    char** out_err);

velr_code velr_tx_explain_analyze(
    velr_tx* tx,
    const char* cypher,
    velr_explain_trace** out_explain,
    char** out_err);

void   velr_explain_trace_close(velr_explain_trace* xp);

size_t velr_explain_trace_plan_count(velr_explain_trace* xp);

velr_code velr_explain_trace_plan_meta(
    velr_explain_trace* xp,
    size_t plan_idx,
    velr_explain_plan_meta* out_meta);

size_t velr_explain_trace_step_count(
    velr_explain_trace* xp,
    size_t plan_idx);

velr_code velr_explain_trace_step_meta(
    velr_explain_trace* xp,
    size_t plan_idx,
    size_t step_idx,
    velr_explain_step_meta* out_meta);

size_t velr_explain_trace_statement_count(
    velr_explain_trace* xp,
    size_t plan_idx,
    size_t step_idx);

velr_code velr_explain_trace_statement_meta(
    velr_explain_trace* xp,
    size_t plan_idx,
    size_t step_idx,
    size_t stmt_idx,
    velr_explain_stmt_meta* out_meta);

size_t velr_explain_trace_sqlite_plan_count(
    velr_explain_trace* xp,
    size_t plan_idx,
    size_t step_idx,
    size_t stmt_idx);

velr_code velr_explain_trace_sqlite_plan_detail(
    velr_explain_trace* xp,
    size_t plan_idx,
    size_t step_idx,
    size_t stmt_idx,
    size_t detail_idx,
    velr_strview* out_detail);

velr_code velr_explain_trace_compact_len(
    velr_explain_trace* xp,
    size_t* out_len,
    char** out_err);

velr_code velr_explain_trace_compact_write(
    velr_explain_trace* xp,
    uint8_t* dst_ptr,
    size_t dst_len,
    size_t* out_written,
    char** out_err);

velr_code velr_explain_trace_compact_malloc(
    velr_explain_trace* xp,
    uint8_t** out_ptr,
    size_t* out_len,
    char** out_err);

/* --------------------------------------------------------------------- */
/* Table / Rows                                                          */
/* --------------------------------------------------------------------- */
size_t velr_table_column_count(velr_table* table);

velr_code velr_table_column_name(
    velr_table* table,
    size_t idx,
    const uint8_t** out_ptr,
    size_t* out_len);

velr_code velr_table_rows_open(
    velr_table* table,
    velr_rows** out_rows,
    char** out_err);

velr_code velr_table_json_rows_malloc(
    velr_table* table,
    int parse_json,
    uint8_t** out_ptr,
    size_t* out_len,
    char** out_err);

velr_code velr_table_json_rows_with_options_malloc(
    velr_table* table,
    int parse_json,
    const size_t* json_text_columns,
    size_t json_text_column_count,
    uint8_t** out_ptr,
    size_t* out_len,
    char** out_err);

velr_code velr_table_python_rows_malloc(
    velr_table* table,
    int parse_json,
    const size_t* json_text_columns,
    size_t json_text_column_count,
    uint8_t** out_ptr,
    size_t* out_len,
    char** out_err);

velr_code velr_table_python_rows_with_paths_malloc(
    velr_table* table,
    const size_t* json_text_columns,
    size_t json_text_column_count,
    uint8_t** out_ptr,
    size_t* out_len,
    char** out_err);

void velr_table_close(velr_table* table);

int velr_rows_next(
    velr_rows* rows,
    velr_cell* buf,
    size_t buf_len,
    size_t* out_written,
    char** out_err);

int velr_rows_next_batch(
    velr_rows* rows,
    velr_cell* buf,
    size_t buf_len,
    size_t max_rows,
    size_t* out_rows,
    size_t* out_cells,
    char** out_err);

void velr_rows_close(velr_rows* rows);

/* --------------------------------------------------------------------- */
/* Transactions                                                          */
/* --------------------------------------------------------------------- */
velr_code velr_tx_begin(velr_db* db, velr_tx** out_tx, char** out_err);
velr_code velr_tx_commit(velr_tx* tx, char** out_err);
velr_code velr_tx_rollback(velr_tx* tx, char** out_err);
void      velr_tx_close(velr_tx* tx);

/* --------------------------------------------------------------------- */
/* Exec inside tx                                                        */
/* --------------------------------------------------------------------- */
velr_code velr_tx_exec_start(
    velr_tx* tx,
    const char* cypher,
    velr_stream_tx** out_stream,
    char** out_err);

velr_code velr_tx_exec_start_with_options(
    velr_tx* tx,
    const char* cypher,
    const velr_query_options* opts,
    velr_stream_tx** out_stream,
    char** out_err);

velr_code velr_tx_exec_start_with_control(
    velr_tx* tx, const char* cypher, const velr_query_options* opts,
    const velr_query_control* control, velr_stream_tx** out_stream, char** out_err);

velr_code velr_tx_run(
    velr_tx* tx,
    const char* cypher,
    char** out_err);

velr_code velr_tx_run_with_options(
    velr_tx* tx,
    const char* cypher,
    const velr_query_options* opts,
    char** out_err);

velr_code velr_tx_run_with_control(
    velr_tx* tx, const char* cypher, const velr_query_options* opts,
    const velr_query_control* control, char** out_err);

velr_code velr_stream_tx_next_table(
    velr_stream_tx* stream,
    velr_table** out_table,
    int* out_has,
    char** out_err);

void velr_exec_tx_close(velr_stream_tx* stream);

/* --------------------------------------------------------------------- */
/* Savepoints                                                            */
/* --------------------------------------------------------------------- */
velr_code velr_tx_savepoint(velr_tx* tx, velr_sp** out_sp, char** out_err);
velr_code velr_sp_release(velr_sp* sp, char** out_err);
velr_code velr_sp_rollback(velr_sp* sp, char** out_err);
void      velr_sp_close(velr_sp* sp);

velr_code velr_tx_savepoint_named(
    velr_tx* tx,
    const char* name_utf8,
    velr_sp** out_sp,
    char** out_err);

velr_code velr_tx_rollback_to(
    velr_tx* tx,
    const char* name_utf8,
    char** out_err);

velr_code velr_bind_cells(
    velr_db* db,
    const char* logical_utf8,
    const velr_strview* colnames,
    size_t col_count,
    const velr_cell* cells,
    size_t row_count,
    char** out_err);

velr_code velr_tx_bind_cells(
    velr_tx* tx,
    const char* logical_utf8,
    const velr_strview* colnames,
    size_t col_count,
    const velr_cell* cells,
    size_t row_count,
    char** out_err);

/* --------------------------------------------------------------------- */
/* Arrow IPC / Arrow C Data                                              */
/* Keep this section only if your loaded library was built with          */
/* the Rust `arrow-ipc` feature / VELR_HAS_ARROW_IPC.                    */
/* --------------------------------------------------------------------- */
struct ArrowSchema;
struct ArrowArray;

velr_code velr_bind_arrow(
    velr_db* db,
    const char* logical_utf8,
    const struct ArrowSchema* const* schemas,
    const struct ArrowArray* const* arrays,
    const velr_strview* colnames,
    size_t col_count,
    char** out_err);

velr_code velr_tx_bind_arrow(
    velr_tx* tx,
    const char* logical_utf8,
    const struct ArrowSchema* const* schemas,
    const struct ArrowArray* const* arrays,
    const velr_strview* colnames,
    size_t col_count,
    char** out_err);

velr_code velr_bind_arrow_ipc(
    velr_db* db,
    const char* logical_utf8,
    const uint8_t* ipc_ptr,
    size_t ipc_len,
    char** out_err);

velr_code velr_tx_bind_arrow_ipc(
    velr_tx* tx,
    const char* logical_utf8,
    const uint8_t* ipc_ptr,
    size_t ipc_len,
    char** out_err);

velr_code velr_table_ipc_file_len(
    velr_table* table,
    size_t* out_len,
    char** out_err);

velr_code velr_table_ipc_file_write(
    velr_table* table,
    uint8_t* dst_ptr,
    size_t dst_len,
    size_t* out_written,
    char** out_err);

velr_code velr_table_ipc_file_malloc(
    velr_table* table,
    uint8_t** out_ptr,
    size_t* out_len,
    char** out_err);

typedef struct velr_arrow_chunks {
  const struct ArrowSchema* const* schemas;
  const struct ArrowArray*  const* arrays;
  size_t                    chunk_count;
} velr_arrow_chunks;

velr_code velr_bind_arrow_chunks(
    velr_db* db,
    const char* logical_utf8,
    const struct velr_arrow_chunks* cols,
    const velr_strview* colnames,
    size_t col_count,
    char** out_err);

velr_code velr_tx_bind_arrow_chunks(
    velr_tx* tx,
    const char* logical_utf8,
    const struct velr_arrow_chunks* cols,
    const velr_strview* colnames,
    size_t col_count,
    char** out_err);
""")


# ---------------- Platform helpers ----------------

def _platform_names() -> list[str]:
    if sys.platform.startswith("darwin"):
        return ["libvelrc.dylib", "velrc.dylib"]
    if sys.platform.startswith("win"):
        return ["velrc.dll"]
    # linux & everything else assumed ELF
    return ["libvelrc.so", "velrc.so"]

def _iter_candidate_paths() -> list[Path]:
    """
    Strict: only search the vendored location inside the wheel/editable install:
        <package>/velrpy/_vendor/<platform-lib-name>
    No environment overrides (e.g., VELR_LIB), no PATH/LD/DYLD, no cwd fallbacks.
    """
    here = Path(__file__).resolve()
    vendor_dir = here.parent / "_vendor"
    names = _platform_names()

    out: list[Path] = []
    for n in names:
        p = (vendor_dir / n)
        if p.exists():
            out.append(p.resolve())
    return out

@contextmanager
def _windows_dll_dir(path: Path):
    if not sys.platform.startswith("win"):
        yield
        return
    add_dir = getattr(os, "add_dll_directory", None)
    if add_dir is None:
        yield
        return
    handle = None
    try:
        handle = add_dir(str(path))
        yield
    finally:
        if handle is not None:
            with suppress(Exception):
                handle.close()


def _try_dlopen(candidates: list[Path]) -> object:
    last_err: Exception | None = None

    # Only try the vendored copies we found.
    for p in candidates:
        try:
            if sys.platform.startswith("win"):
                with _windows_dll_dir(p.parent):
                    return ffi.dlopen(str(p))
            return ffi.dlopen(str(p))
        except OSError as e:
            last_err = e

    # Nothing worked — hard fail with a precise message.
    vendor_msg = str((Path(__file__).resolve().parent / "_vendor").resolve())
    cand_lines = ["  - " + str(p) for p in candidates]
    if not cand_lines:
        cand_lines = ["  - <no files present>"]

    msg_lines = [
        "velr: failed to locate/load the native library from the vendored wheel location.",
        f"Platform: {sys.platform}",
        "Checked:",
        *cand_lines,
        "",
        "Expected the library to be bundled under:",
        f"  {vendor_msg}",
        "",
        "Fixes:",
        "  * Reinstall velr from a wheel (python -m pip install --force-reinstall velr).",
        "  * Ensure _vendor/ contains the platform library.",
        "  * Make sure Python arch matches the wheel (e.g. arm64 vs x86_64).",
    ]
    if last_err:
        msg_lines += ["", "Last dlopen error: " + str(last_err)]
    raise OSError("\n".join(msg_lines))

def _dlopen():
    candidates = _iter_candidate_paths()
    return _try_dlopen(candidates)

# The loaded C-ABI library handle
lib = _dlopen()
