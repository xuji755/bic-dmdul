from __future__ import annotations

from typing import Any


DEFAULT_FATAL_CODES = frozenset(
    {
        "control-file-not-found",
        "control-file-dbf-hint-missing",
        "duplicate-group-file-hint",
        "trailing-bytes",
        "empty-data-file",
        "short-dbf-file",
        "unparsed-dbf-file",
        "catalog-page-number-mismatch",
        "catalog-reference-out-of-range",
    }
)


def evaluate_database_summary_preflight(
    summary: dict[str, Any],
    *,
    fatal_codes: frozenset[str] = DEFAULT_FATAL_CODES,
) -> dict[str, Any]:
    """Evaluate a database summary before offline extraction.

    This gate is intentionally conservative. It consumes stable diagnostic
    codes from summarize-database output and reports whether extraction should
    proceed with the current parser maturity.
    """

    counts_by_code = summary.get("diagnostics", {}).get("counts_by_code", {})
    if not isinstance(counts_by_code, dict):
        counts_by_code = {}
    fatal: list[dict[str, Any]] = []
    nonfatal: list[dict[str, Any]] = []
    for code, count in sorted(counts_by_code.items()):
        item = {
            "code": str(code),
            "count": int(count),
        }
        if str(code) in fatal_codes:
            fatal.append(item)
        else:
            nonfatal.append(item)
    return {
        "ok": not fatal,
        "fatal_codes": fatal,
        "nonfatal_codes": nonfatal,
        "warnings": list(summary.get("warnings", [])),
    }
