import argparse
import datetime
import json
import sys
import time
from typing import Dict, Iterator, List, Optional, Set

from .comparator import DEFAULT_EXCLUDE_FIELDS, TypeResolver, build_type_resolver, diff_doc, null_type_resolver
from .report import ReportWriter
from .solr_client import SolrClient
from .source import parse_source_spec
from .state import ValidatorState, load_state, save_state


def _compute_schema_diff(src_fields: List[Dict], tgt_fields: List[Dict]) -> Dict:
    src_map = {f["name"]: f.get("type") for f in src_fields}
    tgt_map = {f["name"]: f.get("type") for f in tgt_fields}
    return {
        "only_in_source": sorted(set(src_map) - set(tgt_map)),
        "only_in_target": sorted(set(tgt_map) - set(src_map)),
        "type_mismatches": [
            {"field": f, "source_type": src_map[f], "target_type": tgt_map[f]}
            for f in sorted(set(src_map) & set(tgt_map))
            if src_map[f] != tgt_map[f]
        ],
    }


def _merge_compare(
    src_iter: Iterator[Dict],
    tgt_iter: Iterator[Dict],
    resolve_type: TypeResolver,
    exclude_fields: Set[str],
    report: ReportWriter,
    state: ValidatorState,
    state_path: Optional[str],
    checkpoint_interval: int,
    float_rtol: float,
    float_atol: float,
    src_total: Optional[int],
) -> None:
    a = next(src_iter, None)
    b = next(tgt_iter, None)
    events = 0

    while a is not None or b is not None:
        if a is not None and b is not None and a["id"] == b["id"]:
            diffs = diff_doc(a, b, resolve_type, exclude_fields, float_rtol, float_atol)
            if diffs:
                report.add_field_diff(a["id"], diffs)
            report.total_compared += 1
            state.docs_compared += 1
            state.last_processed_id = a["id"]
            a = next(src_iter, None)
            b = next(tgt_iter, None)
        elif b is None or (a is not None and a["id"] < b["id"]):
            report.add_missing_in_target(a["id"])
            state.missing_in_target += 1
            state.last_processed_id = a["id"]
            a = next(src_iter, None)
        else:
            report.add_missing_in_source(b["id"])
            state.missing_in_source += 1
            state.last_processed_id = b["id"]
            b = next(tgt_iter, None)

        events += 1
        if events % 5000 == 0:
            n_diffs = len(report.field_diffs)
            n_missing = len(report.missing_in_target) + len(report.missing_in_source)
            total_str = f"{src_total:,}" if src_total else "?"
            pct = f" ({100 * events / src_total:.1f}%)" if src_total else ""
            print(
                f"\r  {events:>12,} / {total_str}{pct}  "
                f"field_diffs: {n_diffs:,}  missing: {n_missing:,}   ",
                end="",
                flush=True,
            )

        if state_path and events > 0 and events % checkpoint_interval == 0:
            save_state(state_path, state)

    if state_path:
        save_state(state_path, state)
    if events >= 5000:
        print()  # newline after the \r progress line


def cmd_snapshot(args: argparse.Namespace) -> int:
    client = SolrClient(args.solr, timeout=args.timeout)
    exclude_fields = DEFAULT_EXCLUDE_FIELDS | set(args.exclude_field or [])

    print(f"Fetching schema for index '{args.index}'...")
    schema_fields = client.get_schema_fields(args.index)
    declared_names = [f["name"] for f in schema_fields if f["name"] not in exclude_fields]

    print("Counting documents...")
    doc_count = client.get_doc_count(args.index)
    mode = client.detect_mode()
    print(f"  {doc_count:,} documents  (mode: {mode})")

    # Explicit declared fields ensure docValues-only fields are returned;
    # * catches dynamic field instances and other stored fields.
    fl = declared_names + ["*"]

    meta = {
        "__meta__": {
            "index": args.index,
            "mode": mode,
            "doc_count": doc_count,
            "taken_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "solr_url": args.solr,
        }
    }

    print(f"Writing snapshot to {args.out}...")
    written = 0
    with open(args.out, "w") as fh:
        fh.write(json.dumps(meta) + "\n")
        for doc in client.stream_docs(args.index, fl, args.batch_size):
            filtered = {k: v for k, v in doc.items() if k not in exclude_fields}
            fh.write(json.dumps(filtered, default=str) + "\n")
            written += 1
            if written % 10000 == 0:
                print(
                    f"\r  Written {written:>12,} / {doc_count:,}   ",
                    end="",
                    flush=True,
                )

    print(f"\r  Done. Wrote {written:,} docs to {args.out}         ")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    client = SolrClient(args.solr, timeout=args.timeout)
    exclude_fields = DEFAULT_EXCLUDE_FIELDS | set(args.exclude_field or [])

    src_spec: str = args.source
    tgt_spec: str = args.target

    src_schema_fields: List[Dict] = []
    tgt_schema_fields: List[Dict] = []
    src_dynamic_fields: List[Dict] = []
    tgt_dynamic_fields: List[Dict] = []
    if src_spec.startswith("index:"):
        print(f"Fetching schema for {src_spec}...")
        src_schema_fields = client.get_schema_fields(src_spec[6:])
        src_dynamic_fields = client.get_dynamic_fields(src_spec[6:])
    if tgt_spec.startswith("index:"):
        print(f"Fetching schema for {tgt_spec}...")
        tgt_schema_fields = client.get_schema_fields(tgt_spec[6:])
        tgt_dynamic_fields = client.get_dynamic_fields(tgt_spec[6:])

    # Build TypeResolver: source resolver wins on type conflicts
    # For file sources, fall back to null_type_resolver (no schema available)
    if src_schema_fields or src_dynamic_fields:
        src_resolver = build_type_resolver(src_schema_fields, src_dynamic_fields)
    else:
        src_resolver = null_type_resolver
    if tgt_schema_fields or tgt_dynamic_fields:
        tgt_resolver = build_type_resolver(tgt_schema_fields, tgt_dynamic_fields)
    else:
        tgt_resolver = null_type_resolver

    def resolve_type(field_name: str) -> Optional[str]:
        return src_resolver(field_name) or tgt_resolver(field_name)

    src_field_types = {f["name"]: f.get("type") for f in src_schema_fields}
    tgt_field_types = {f["name"]: f.get("type") for f in tgt_schema_fields}

    all_schema_names = sorted(
        (set(src_field_types) | set(tgt_field_types)) - exclude_fields
    )
    fl = all_schema_names + ["*"]

    report = ReportWriter()
    if src_schema_fields and tgt_schema_fields:
        report.schema_diff = _compute_schema_diff(src_schema_fields, tgt_schema_fields)
        sd = report.schema_diff
        if sd["only_in_source"] or sd["only_in_target"] or sd["type_mismatches"]:
            print("WARNING: schema drift detected (details in report)")

    resume_from_id: Optional[str] = None
    state = ValidatorState()
    if args.resume:
        state = load_state(args.checkpoint)
        resume_from_id = state.last_processed_id
        if resume_from_id:
            print(
                f"Resuming from ID: {resume_from_id!r} "
                f"({state.docs_compared:,} docs already processed)"
            )
            print(
                "  WARNING: --resume restores progress counts but NOT prior findings. "
                "The output report will only contain findings from this run onwards."
            )
            report.total_compared = state.docs_compared

    src = parse_source_spec(src_spec, client, fl, args.batch_size, resume_from_id)
    tgt = parse_source_spec(tgt_spec, client, fl, args.batch_size, resume_from_id)

    src_count = src.get_doc_count()
    tgt_count = tgt.get_doc_count()
    if src_count is not None:
        print(f"Source ({src.label()}): {src_count:,} docs")
    if tgt_count is not None:
        print(f"Target ({tgt.label()}): {tgt_count:,} docs")
    if src_count and tgt_count and src_count != tgt_count:
        print(f"  WARNING: doc counts differ by {abs(src_count - tgt_count):,}")

    state_path = None if getattr(args, "no_checkpoint", False) else args.checkpoint

    print("Comparing...")
    _start = time.monotonic()
    _merge_compare(
        src.iter_docs(),
        tgt.iter_docs(),
        resolve_type,
        exclude_fields,
        report,
        state,
        state_path,
        args.checkpoint_interval,
        args.float_rtol,
        args.float_atol,
        src_count,
    )

    report.save(args.report)
    report.print_summary(src.label(), tgt.label(), elapsed_seconds=time.monotonic() - _start)
    print(f"  Report saved to: {args.report}")

    clean = (
        not report.field_diffs
        and not report.missing_in_target
        and not report.missing_in_source
    )
    return 0 if clean else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m solr_validator",
        description="Validate Solr index integrity by comparing documents field-by-field.",
    )
    parser.add_argument(
        "--solr",
        default="http://localhost:8983/solr",
        help="Solr base URL (default: http://localhost:8983/solr)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP request timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        dest="batch_size",
        help="cursorMark page size (default: 1000)",
    )
    parser.add_argument(
        "--exclude-field",
        action="append",
        metavar="FIELD",
        dest="exclude_field",
        help="Extra field to exclude from comparison (repeatable)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── snapshot ──────────────────────────────────────────────────────────
    sp = sub.add_parser(
        "snapshot",
        help="Stream an index to a JSONL snapshot file (run before your suspect operation)",
    )
    sp.add_argument("--index", required=True, help="Index name (core or collection)")
    sp.add_argument("--out", required=True, help="Output JSONL file")

    # ── compare ───────────────────────────────────────────────────────────
    cp = sub.add_parser(
        "compare",
        help="Compare two sources; each source is 'index:<name>' or 'file:<path>'",
    )
    cp.add_argument("source", help="Source — 'index:<name>' or 'file:<path>'")
    cp.add_argument("target", help="Target — 'index:<name>' or 'file:<path>'")
    cp.add_argument(
        "--report",
        default="validation-report.json",
        help="Output JSON report path (default: validation-report.json)",
    )
    cp.add_argument(
        "--float-rtol",
        type=float,
        default=1e-9,
        dest="float_rtol",
        help="Relative tolerance for float comparison (default: 1e-9)",
    )
    cp.add_argument(
        "--float-atol",
        type=float,
        default=0.0,
        dest="float_atol",
        help="Absolute tolerance for float comparison (default: 0.0)",
    )
    cp.add_argument(
        "--checkpoint",
        default=".validator-state.json",
        help="Checkpoint state file (default: .validator-state.json)",
    )
    cp.add_argument(
        "--checkpoint-interval",
        type=int,
        default=10000,
        dest="checkpoint_interval",
        help="Write checkpoint every N events (default: 10000)",
    )
    cp.add_argument(
        "--no-checkpoint",
        action="store_true",
        dest="no_checkpoint",
        help="Disable checkpointing entirely",
    )
    cp.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint (reads --checkpoint file)",
    )

    # ── selftest ──────────────────────────────────────────────────────────
    sub.add_parser(
        "selftest",
        help="Run the end-to-end verification harness against a local Solr instance",
    )

    args = parser.parse_args()

    if args.command == "snapshot":
        sys.exit(cmd_snapshot(args))
    elif args.command == "compare":
        sys.exit(cmd_compare(args))
    elif args.command == "selftest":
        from .selftest import run_selftest
        sys.exit(run_selftest(args.solr, args.timeout))
