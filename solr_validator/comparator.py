import math
import collections
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set


# Solr field type names (lowercased) that need special comparison handling
_DATE_TYPES: Set[str] = {"pdate", "date", "daterange", "tdate"}
_FLOAT_TYPES: Set[str] = {
    "pfloat", "tfloat", "float",
    "pdouble", "tdouble", "double",
}

DEFAULT_EXCLUDE_FIELDS: Set[str] = {
    "_version_", "_root_", "_nest_path_", "_nest_parent_",
    "_text_", "_route_", "score",
}

TypeResolver = Callable[[str], Optional[str]]


def build_type_resolver(
    declared_fields: List[Dict],
    dynamic_patterns: Optional[List[Dict]] = None,
) -> TypeResolver:
    """Return a callable that maps a field name to its Solr type string.

    Checks declared fields first, then dynamic field patterns. Returns None if
    the field is unknown (falls back to exact equality comparison).
    """
    declared_map: Dict[str, Optional[str]] = {
        f["name"]: f.get("type") for f in declared_fields
    }
    patterns: List[Dict] = dynamic_patterns or []

    def resolve(field_name: str) -> Optional[str]:
        if field_name in declared_map:
            return declared_map[field_name]
        for p in patterns:
            pat = p["name"]
            if pat.startswith("*") and field_name.endswith(pat[1:]):
                return p.get("type")
            if pat.endswith("*") and field_name.startswith(pat[:-1]):
                return p.get("type")
        return None

    return resolve


def null_type_resolver(field_name: str) -> Optional[str]:  # noqa: ARG001
    """Resolver that returns None for every field (file-vs-file with no schema)."""
    return None


def _is_date_type(type_name: Optional[str]) -> bool:
    return (type_name or "").lower() in _DATE_TYPES


def _is_float_type(type_name: Optional[str]) -> bool:
    return (type_name or "").lower() in _FLOAT_TYPES


def _normalize_date(val: str) -> datetime:
    """Parse a Solr ISO-8601 date string to an aware datetime. Works on Python 3.8+."""
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    return datetime.fromisoformat(val)


def _canonical_scalar(val: Any, type_name: Optional[str]) -> Any:
    """Return a canonical, sortable form of a scalar value for multiset comparison."""
    if _is_date_type(type_name) and isinstance(val, str):
        try:
            return _normalize_date(val)
        except (ValueError, AttributeError):
            return val
    return val


def _values_equal(
    a: Any,
    b: Any,
    type_name: Optional[str],
    float_rtol: float,
    float_atol: float,
) -> bool:
    a_list = isinstance(a, list)
    b_list = isinstance(b, list)

    if a_list != b_list:
        return False

    if a_list:
        if len(a) != len(b):
            return False
        if _is_float_type(type_name):
            # Sort and compare element-by-element with tolerance
            try:
                sa = sorted(float(x) for x in a)
                sb = sorted(float(x) for x in b)
                return all(
                    math.isclose(x, y, rel_tol=float_rtol, abs_tol=float_atol)
                    for x, y in zip(sa, sb)
                )
            except (TypeError, ValueError):
                pass
        if _is_date_type(type_name):
            try:
                ca = sorted(_normalize_date(str(v)) for v in a)
                cb = sorted(_normalize_date(str(v)) for v in b)
                return ca == cb
            except (ValueError, AttributeError):
                pass
        # Generic multiset comparison: order-insensitive
        return collections.Counter(str(v) for v in a) == collections.Counter(str(v) for v in b)

    # Scalar comparison
    if _is_float_type(type_name) and isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=float_rtol, abs_tol=float_atol)

    if _is_date_type(type_name) and isinstance(a, str) and isinstance(b, str):
        try:
            return _normalize_date(a) == _normalize_date(b)
        except (ValueError, AttributeError):
            pass

    return a == b


def diff_doc(
    source_doc: Dict,
    target_doc: Dict,
    resolve_type: TypeResolver,
    exclude_fields: Set[str],
    float_rtol: float = 1e-9,
    float_atol: float = 0.0,
) -> List[Dict]:
    """Return a list of field-level diff records between source_doc and target_doc."""
    all_fields = (
        (set(source_doc.keys()) | set(target_doc.keys()))
        - exclude_fields
        - {"id"}
    )
    diffs = []
    for field in sorted(all_fields):
        type_name = resolve_type(field)
        in_src = field in source_doc
        in_tgt = field in target_doc

        if in_src and not in_tgt:
            diffs.append({
                "field": field,
                "kind": "field_missing_in_target",
                "source": source_doc[field],
            })
        elif in_tgt and not in_src:
            diffs.append({
                "field": field,
                "kind": "field_missing_in_source",
                "target": target_doc[field],
            })
        else:
            if not _values_equal(
                source_doc[field], target_doc[field],
                type_name, float_rtol, float_atol
            ):
                diffs.append({
                    "field": field,
                    "kind": "value_diff",
                    "source": source_doc[field],
                    "target": target_doc[field],
                })
    return diffs
