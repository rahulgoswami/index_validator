import json
import sys
from typing import Dict, List, Optional, Set


class ReportWriter:
    def __init__(self) -> None:
        self.field_diffs: List[Dict] = []
        self.missing_in_target: List[str] = []
        self.missing_in_source: List[str] = []
        self.total_compared: int = 0
        self.schema_diff: Optional[Dict] = None
        self.changed_fields: Set[str] = set()

    def add_field_diff(self, doc_id: str, diffs: List[Dict]) -> None:
        self.field_diffs.append({"id": doc_id, "diffs": diffs})
        for d in diffs:
            self.changed_fields.add(d["field"])

    def add_missing_in_target(self, doc_id: str) -> None:
        self.missing_in_target.append(doc_id)

    def add_missing_in_source(self, doc_id: str) -> None:
        self.missing_in_source.append(doc_id)

    def save(self, path: str) -> None:
        report = {
            "schema_diff": self.schema_diff,
            "summary": {
                "total_compared": self.total_compared,
                "docs_with_field_diffs": len(self.field_diffs),
                "missing_in_target": len(self.missing_in_target),
                "missing_in_source": len(self.missing_in_source),
                "changed_fields": sorted(self.changed_fields),
            },
            "field_diffs": self.field_diffs,
            "missing_in_target": self.missing_in_target,
            "missing_in_source": self.missing_in_source,
        }
        with open(path, "w") as fh:
            json.dump(report, fh, indent=2, default=str)

    def print_summary(
        self,
        source_label: str = "source",
        target_label: str = "target",
        elapsed_seconds: Optional[float] = None,
    ) -> None:
        total_issues = (
            len(self.field_diffs)
            + len(self.missing_in_target)
            + len(self.missing_in_source)
        )
        print(f"\n{'='*50}")
        print("Validation Summary")
        print(f"{'='*50}")
        print(f"  Documents compared:         {self.total_compared:>10,}")
        print(f"  Documents with field diffs: {len(self.field_diffs):>10,}")
        print(f"  Missing in {target_label[:15]:<15} {len(self.missing_in_target):>10,}")
        print(f"  Missing in {source_label[:15]:<15} {len(self.missing_in_source):>10,}")
        if elapsed_seconds is not None:
            h, rem = divmod(int(elapsed_seconds), 3600)
            m, s = divmod(rem, 60)
            elapsed_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
            print(f"  Time elapsed:               {elapsed_str:>10}")
        print(f"{'='*50}")
        if total_issues == 0:
            print("  RESULT: CLEAN — no discrepancies found")
        else:
            print(f"  RESULT: {total_issues:,} discrepancy/ies found")

        if self.schema_diff:
            only_in_src = self.schema_diff.get("only_in_source", [])
            only_in_tgt = self.schema_diff.get("only_in_target", [])
            type_mismatches = self.schema_diff.get("type_mismatches", [])
            if only_in_src or only_in_tgt or type_mismatches:
                print("\n  Schema drift detected:")
                for f in only_in_src:
                    print(f"    field '{f}' exists in source only")
                for f in only_in_tgt:
                    print(f"    field '{f}' exists in target only")
                for m in type_mismatches:
                    print(
                        f"    field '{m['field']}': "
                        f"source type={m['source_type']!r}, "
                        f"target type={m['target_type']!r}"
                    )

        if self.changed_fields:
            print(f"\n  Fields with diffs ({len(self.changed_fields)}):")
            for f in sorted(self.changed_fields):
                print(f"    {f}")

        if self.field_diffs:
            preview = self.field_diffs[:5]
            print(f"\n  First {len(preview)} doc(s) with field diffs:")
            for item in preview:
                print(f"    doc {item['id']}:")
                for d in item["diffs"][:3]:
                    print(f"      {d['field']}: {d['kind']}")
                if len(item["diffs"]) > 3:
                    print(f"      ... and {len(item['diffs']) - 3} more field(s)")
        print()
