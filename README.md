# Solr Index Validator

Detect field-level data loss and value corruption by comparing two Solr indexes (or a pre-operation snapshot against a live index) document-by-document.

## Requirements

- Python 3.8+
- `requests>=2.31`

```bash
pip install -r requirements.txt
```

Works against **standalone Solr (cores)** and **SolrCloud (collections)**. The tool auto-detects the mode.

## Version compatibility

Compatible with **Solr 8.x and 9.x**. All HTTP APIs used (`cursorMark`, Schema API, `/admin/info/system`) have been stable since Solr 8.0. Both the legacy trie-based field types (`tdate`, `tfloat`, `tdouble`) common in 8.x schemas and the points-based types (`pdate`, `pfloat`, `pdouble`) used as defaults in 9.x are recognized for type-aware float and date comparison.

The `selftest` command creates temporary indexes using the `_default` configset, which requires Solr 7.3 or later. The `snapshot` and `compare` commands have no such constraint and work on any Solr 8.x or 9.x instance.

Solr versions prior to 8.0 are not tested or supported.

## Quickstart

### Option A: Snapshot then compare (recommended when you suspect an operation corrupts data)

```bash
# 1. Capture the index before the suspect operation
python -m solr_validator --solr http://localhost:8983/solr snapshot \
  --index orders \
  --out ./orders_before.jsonl

# 2. Run the suspect operation on `orders`

# 3. Compare the snapshot against the now-modified live index
python -m solr_validator --solr http://localhost:8983/solr compare \
  file:./orders_before.jsonl \
  index:orders \
  --report ./corruption-report.json
```

### Option B: Compare two live indexes directly

```bash
python -m solr_validator --solr http://localhost:8983/solr compare \
  index:orders_v1 \
  index:orders_v2 \
  --report ./report.json
```

### Verify the validator itself

```bash
python -m solr_validator --solr http://localhost:8983/solr selftest
```

This creates two temporary indexes (`validator_a`, `validator_b`), injects known corruptions into `validator_b`, runs a compare, and asserts the report matches exactly. A `PASS` means the validator is working correctly against your Solr instance.

## Commands

### `snapshot`

Stream a sorted copy of an index to a local JSONL file.

```
python -m solr_validator [global-flags] snapshot --index NAME --out FILE
```

| Flag | Default | Description |
|------|---------|-------------|
| `--index` | required | Core or collection name |
| `--out` | required | Output JSONL path |

The snapshot file starts with a metadata header line and then one JSON object per document, sorted by `id` ascending. This file can be used as a source in `compare`.

### `compare`

Diff two sources field-by-field and write a JSON report.

```
python -m solr_validator [global-flags] compare SOURCE TARGET [flags]
```

Each source is either `index:<name>` or `file:<path>` (a snapshot file).

| Flag | Default | Description |
|------|---------|-------------|
| `--report` | `validation-report.json` | Output JSON report path |
| `--float-rtol` | `1e-9` | Relative tolerance for float/double comparisons |
| `--float-atol` | `0.0` | Absolute tolerance for float/double comparisons |
| `--checkpoint` | `.validator-state.json` | Checkpoint file path |
| `--checkpoint-interval` | `10000` | Write checkpoint every N events |
| `--no-checkpoint` | off | Disable checkpointing |
| `--resume` | off | Resume from last checkpoint |

**Resume caveat**: `--resume` restores the progress counter (docs already processed) but not prior findings. The output report will only contain findings from the resumed run onwards. For a complete picture across runs you must merge the reports manually.

### `selftest`

Run the end-to-end verification harness (requires a reachable Solr instance):

```
python -m solr_validator [global-flags] selftest
```

### Global flags

| Flag | Default | Description |
|------|---------|-------------|
| `--solr` | `http://localhost:8983/solr` | Solr base URL |
| `--timeout` | `60` | HTTP request timeout in seconds |
| `--batch-size` | `1000` | cursorMark page size |
| `--exclude-field FIELD` | — | Exclude a field from comparison (repeatable) |

The following fields are always excluded: `_version_`, `_root_`, `_nest_path_`, `_nest_parent_`, `_text_`, `_route_`, `score`. `_version_` is guaranteed to differ between any two indexes and would produce only noise.

## Report format

```json
{
  "schema_diff": {
    "only_in_source": ["field_a"],
    "only_in_target": [],
    "type_mismatches": []
  },
  "summary": {
    "total_compared": 1000000,
    "docs_with_field_diffs": 12,
    "missing_in_target": 3,
    "missing_in_source": 1
  },
  "field_diffs": [
    {
      "id": "doc-42",
      "diffs": [
        {"field": "price", "kind": "value_diff", "source": 19.99, "target": 0.0},
        {"field": "tags",  "kind": "field_missing_in_target", "source": ["a", "b"]}
      ]
    }
  ],
  "missing_in_target": ["doc-99", "doc-100"],
  "missing_in_source": ["doc-77"]
}
```

Diff kinds:
- `value_diff` — field present on both sides but values differ
- `field_missing_in_target` — field present in source, absent in target
- `field_missing_in_source` — field present in target, absent in source

## Comparison semantics

| Field type | Comparison method |
|---|---|
| `pfloat`, `tfloat`, `float`, `pdouble`, `tdouble`, `double` | `math.isclose(rel_tol=float_rtol, abs_tol=float_atol)` |
| `pdate`, `date`, `daterange`, `tdate` | Parsed as `datetime` objects; timezone-aware |
| Multi-valued (list) | Multiset comparison — order-insensitive, count-sensitive |
| Everything else | Direct `==` |

Field types are resolved from the schema: declared fields first, then dynamic field patterns (e.g. `*_i`, `*_ss`, `*_dt`). For `file:` sources with no schema, all fields fall back to direct equality.

## Performance notes

- Documents are streamed via cursorMark pagination — memory usage is bounded to a small number of batches regardless of index size.
- A background thread prefetches batches from Solr into a bounded queue so the comparator is never blocked on I/O.
- HTTP connections are pooled via `requests.Session` — a single TCP connection is reused for all requests to the same Solr instance.
- At 50M documents, a compare run takes several hours. Use `--checkpoint` (enabled by default) to survive restarts.

## Limitations (v1)

- **Nested / child documents** (block join) are not supported. Only flat documents.
- **No TLS or authentication**. HTTP only.
- **No parallel sharded comparison**. A single process compares both sides sequentially.
- **`/export` handler not used**. The validator uses cursorMark, which works without docValues on every field. The `/export` handler is faster but requires `docValues=true` on all returned fields.
- **ID comparison is lexicographic**. Solr sorts `id asc` as strings. Numeric-looking IDs (`1`, `2`, `10`) will sort as `"1"`, `"10"`, `"2"`. Ensure both indexes were populated with the same id format.
