"""End-to-end verification harness.

Creates two test indexes (validator_a, validator_b), seeds known corruptions
into validator_b, runs compare, and asserts the report matches exactly.
"""

import json
import os
import tempfile
import traceback
from argparse import Namespace
from typing import Dict, List

from .cli import cmd_compare
from .solr_client import SolrClient


def _generate_corpus(n: int) -> List[Dict]:
    docs = []
    for i in range(n):
        # Default tags vary by index
        tags = ["tag_a", "tag_b", "tag_c"] if i % 3 == 0 else ["tag_x", "tag_y"]
        docs.append({
            "id": f"doc-{i}",
            "title_s": f"Title {i}",
            "count_i": i,
            "price_d": float(i) * 1.5,
            "tags_ss": tags,
            "created_dt": f"2023-01-{(i % 28) + 1:02d}T10:30:00Z",
        })
    # Pin specific values used by the test assertions
    docs[20]["tags_ss"] = ["tag_a", "tag_b", "tag_c"]
    docs[21]["tags_ss"] = ["tag_a", "tag_b", "tag_c"]
    docs[30]["price_d"] = 100.0
    return docs


def _inject_corruptions(client: SolrClient, index_name: str, corpus: List[Dict]) -> None:
    # 1. Remove title_s from doc-5 — field_missing_in_target
    doc5 = {k: v for k, v in corpus[5].items() if k != "title_s"}
    client.index_docs(index_name, [doc5], commit=False)

    # 2. Change count_i of doc-10 — value_diff
    doc10 = {**corpus[10], "count_i": 99999}
    client.index_docs(index_name, [doc10], commit=False)

    # 3. Remove one element from tags_ss of doc-20 — value_diff (different multiset)
    doc20 = {**corpus[20], "tags_ss": ["tag_a", "tag_b"]}
    client.index_docs(index_name, [doc20], commit=False)

    # 4. Reorder tags_ss of doc-21 — must NOT produce a diff
    doc21 = {**corpus[21], "tags_ss": ["tag_c", "tag_b", "tag_a"]}
    client.index_docs(index_name, [doc21], commit=False)

    # 5. Perturb price_d of doc-30 by 1e-12 — must NOT produce a diff (within rtol=1e-9)
    doc30 = {**corpus[30], "price_d": corpus[30]["price_d"] + 1e-12}
    client.index_docs(index_name, [doc30], commit=False)

    # 6. Add a new doc only in target — missing_in_source
    client.index_docs(
        index_name,
        [{"id": "doc-9999", "title_s": "Extra doc", "count_i": 9999}],
        commit=False,
    )

    # 7. Delete doc-50 from target — missing_in_target; commit all above at once
    client.delete_doc(index_name, corpus[50]["id"], commit=True)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _verify_report(report_path: str) -> None:
    with open(report_path) as fh:
        report = json.load(fh)

    summary = report["summary"]
    field_diff_ids = {item["id"] for item in report["field_diffs"]}
    field_diff_map = {item["id"]: item["diffs"] for item in report["field_diffs"]}

    # doc counts
    _assert(
        summary["docs_with_field_diffs"] == 3,
        f"Expected 3 docs with field diffs, got {summary['docs_with_field_diffs']}",
    )
    _assert(
        summary["missing_in_target"] == 1,
        f"Expected 1 missing_in_target, got {summary['missing_in_target']}",
    )
    _assert(
        summary["missing_in_source"] == 1,
        f"Expected 1 missing_in_source, got {summary['missing_in_source']}",
    )

    # exact field-diff document set
    _assert(
        field_diff_ids == {"doc-5", "doc-10", "doc-20"},
        f"Unexpected field_diff IDs: {field_diff_ids}",
    )

    # doc-21 reorder must NOT appear (order-insensitive multiset)
    _assert("doc-21" not in field_diff_ids, "doc-21 should not diff (reorder is order-insensitive)")

    # doc-30 float perturbation must NOT appear (within tolerance)
    _assert("doc-30" not in field_diff_ids, "doc-30 should not diff (within float tolerance)")

    # doc-5: title_s missing in target
    d5_diffs = {d["field"]: d["kind"] for d in field_diff_map["doc-5"]}
    _assert(
        d5_diffs.get("title_s") == "field_missing_in_target",
        f"doc-5 expected title_s:field_missing_in_target, got {d5_diffs}",
    )

    # doc-10: count_i value_diff
    d10_diffs = {d["field"]: d["kind"] for d in field_diff_map["doc-10"]}
    _assert(
        d10_diffs.get("count_i") == "value_diff",
        f"doc-10 expected count_i:value_diff, got {d10_diffs}",
    )

    # doc-20: tags_ss value_diff
    d20_diffs = {d["field"]: d["kind"] for d in field_diff_map["doc-20"]}
    _assert(
        d20_diffs.get("tags_ss") == "value_diff",
        f"doc-20 expected tags_ss:value_diff, got {d20_diffs}",
    )

    # missing_in_target: only doc-50
    _assert(
        set(report["missing_in_target"]) == {"doc-50"},
        f"Expected missing_in_target=[doc-50], got {report['missing_in_target']}",
    )

    # missing_in_source: only doc-9999
    _assert(
        set(report["missing_in_source"]) == {"doc-9999"},
        f"Expected missing_in_source=[doc-9999], got {report['missing_in_source']}",
    )


def run_selftest(solr_url: str, timeout: int = 60) -> int:
    print(f"\n{'='*55}")
    print("Solr Validator — Self-Test Harness")
    print(f"  Solr URL: {solr_url}")
    print(f"{'='*55}\n")

    client = SolrClient(solr_url, timeout=timeout)

    # Step 1: detect mode
    try:
        mode = client.detect_mode()
    except Exception as exc:
        print(f"FAIL: cannot reach Solr at {solr_url!r}: {exc}")
        return 1
    print(f"[1/5] Solr mode detected: {mode}")

    # Step 2: delete & recreate test indexes
    print("[2/5] Recreating test indexes (validator_a, validator_b)...")
    for name in ("validator_a", "validator_b"):
        try:
            client.delete_index(name)
        except Exception:
            pass  # may not exist yet
        try:
            client.create_index(name)
        except Exception as exc:
            print(f"FAIL: could not create index '{name}': {exc}")
            return 1
    print("      Done.")

    # Step 3: index corpus into both
    corpus = _generate_corpus(1000)
    print("[3/5] Indexing 1,000-doc corpus into both indexes...")
    try:
        # Index in chunks to avoid overly large payloads
        chunk = 200
        for start in range(0, len(corpus), chunk):
            batch = corpus[start: start + chunk]
            client.index_docs("validator_a", batch, commit=False)
            client.index_docs("validator_b", batch, commit=False)
        # Final commit
        client.index_docs("validator_a", [], commit=True)
        client.index_docs("validator_b", [], commit=True)
    except Exception as exc:
        print(f"FAIL: indexing error: {exc}")
        return 1
    print("      Done.")

    # Step 4: inject corruptions into validator_b
    print("[4/5] Injecting known corruptions into validator_b...")
    try:
        _inject_corruptions(client, "validator_b", corpus)
    except Exception as exc:
        print(f"FAIL: corruption injection error: {exc}")
        traceback.print_exc()
        return 1
    print("      Corruptions:")
    print("        doc-5   : title_s removed (→ field_missing_in_target)")
    print("        doc-10  : count_i changed (→ value_diff)")
    print("        doc-20  : tags_ss element removed (→ value_diff)")
    print("        doc-21  : tags_ss reordered (→ NO diff expected)")
    print("        doc-30  : price_d + 1e-12 (→ NO diff expected, within tolerance)")
    print("        doc-50  : deleted (→ missing_in_target)")
    print("        doc-9999: added only in target (→ missing_in_source)")

    # Step 5: run compare and verify
    print("[5/5] Running compare...")
    report_fd, report_path = tempfile.mkstemp(suffix=".json", prefix="selftest_report_")
    os.close(report_fd)
    try:
        args = Namespace(
            solr=solr_url,
            timeout=timeout,
            batch_size=100,
            source="index:validator_a",
            target="index:validator_b",
            report=report_path,
            float_rtol=1e-9,
            float_atol=0.0,
            checkpoint=".selftest-state.json",
            checkpoint_interval=10000,
            no_checkpoint=True,
            resume=False,
            exclude_field=None,
        )
        cmd_compare(args)
    except Exception as exc:
        print(f"FAIL: compare raised an exception: {exc}")
        traceback.print_exc()
        return 1

    # Verify report
    try:
        _verify_report(report_path)
    except AssertionError as exc:
        print(f"\nFAIL: assertion failed: {exc}")
        print(f"  Report is at: {report_path}")
        return 1
    except Exception as exc:
        print(f"\nFAIL: unexpected error during verification: {exc}")
        traceback.print_exc()
        return 1
    finally:
        try:
            os.unlink(report_path)
        except OSError:
            pass

    print(f"\n{'='*55}")
    print("  PASS — all assertions satisfied")
    print(f"{'='*55}\n")
    return 0
