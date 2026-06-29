from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .control_file import compare_control_files, summarize_control_file
from .database_summary import summarize_database_dir
from .discovery import discover_data_files
from .evidence import (
    capture_data_file_evidence,
    parse_page_selection,
    verify_evidence_manifest,
)
from .extract import extract_csv_with_calibrated_metadata
from .metadata import CalibratedMetadata
from .page import ObservedPageHeader, format_hex_dump
from .page_catalog import catalog_data_file_pages
from .preflight import evaluate_database_summary_preflight
from .resolver import OfflineResolveError, resolve_offline_table_metadata
from .row import iter_observed_rows, scan_observed_row_chain
from .storage import DataFile
from .sysdict import (
    find_syscolumn_candidates,
    find_sysindex_candidates,
    find_sysobject_candidates,
    find_sysobject_index_child_candidates,
)


def _cmd_file_info(args: argparse.Namespace) -> int:
    data_file = DataFile(Path(args.file), page_size=args.page_size)
    stat = data_file.path.stat()
    print(f"file={data_file.path}")
    print(f"bytes={stat.st_size}")
    print(f"page_size={data_file.page_size}")
    print(f"pages={stat.st_size // data_file.page_size}")
    return 0


def _cmd_dump_page(args: argparse.Namespace) -> int:
    data_file = DataFile(Path(args.file), page_size=args.page_size)
    page = data_file.read_page(args.page_no)
    limit = args.bytes if args.bytes is not None else len(page)
    print(format_hex_dump(page[:limit], base_offset=args.page_no * data_file.page_size))
    return 0


def _cmd_inspect_page(args: argparse.Namespace) -> int:
    data_file = DataFile(Path(args.file), page_size=args.page_size)
    page = data_file.read_page(args.page_no)
    header = ObservedPageHeader.from_page(page)
    for key, value in header.as_dict().items():
        print(f"{key}={value}")
    if args.rows:
        print()
        print("observed_rows_from_header_count:")
        rows = iter_observed_rows(page, row_count=header.observed_row_count)
        for index, row in enumerate(rows, start=1):
            print(
                f"{index}: offset={row.page_offset} "
                f"length={row.length} deleted={row.is_deleted}"
            )
        print()
        print("observed_physical_row_chain:")
        physical_rows = scan_observed_row_chain(page)
        for index, row in enumerate(physical_rows, start=1):
            print(
                f"{index}: offset={row.page_offset} "
                f"length={row.length} deleted={row.is_deleted}"
            )
    if args.dump:
        print()
        print(format_hex_dump(page[: args.dump], base_offset=args.page_no * data_file.page_size))
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    data_file = DataFile(Path(args.file), page_size=args.page_size)
    marker = args.marker.encode(args.encoding)
    matches = list(data_file.find(marker))
    for match in matches:
        print(
            f"offset={match.offset} page={match.page_no} "
            f"page_offset={match.page_offset}"
        )
    return 0 if matches else 1


def _cmd_capture_evidence(args: argparse.Namespace) -> int:
    evidence = capture_data_file_evidence(
        path=Path(args.file),
        page_size=args.page_size,
        pages=parse_page_selection(args.pages or ""),
        markers=tuple(args.marker or ()),
        marker_encoding=args.encoding,
        marker_context=args.context,
        label=args.label,
        copy_state=args.copy_state,
        notes=tuple(args.note or ()),
    )
    payload = json.dumps(evidence, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def _cmd_verify_evidence(args: argparse.Namespace) -> int:
    result = verify_evidence_manifest(Path(args.manifest))
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def _cmd_catalog_pages(args: argparse.Namespace) -> int:
    catalog = catalog_data_file_pages(
        path=Path(args.file),
        page_size=args.page_size,
        start_page=args.start_page,
        max_pages=args.max_pages,
        sample_limit=args.sample_limit,
    )
    payload = json.dumps(catalog, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def _cmd_summarize_database(args: argparse.Namespace) -> int:
    summary = summarize_database_dir(
        database_dir=Path(args.database_dir),
        page_size=args.page_size,
        catalog_pages=args.catalog_pages,
        sample_limit=args.sample_limit,
    )
    payload = json.dumps(summary, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def _cmd_summarize_control_file(args: argparse.Namespace) -> int:
    summary = summarize_control_file(
        Path(args.file),
        sample_limit=args.sample_limit,
    )
    payload = json.dumps(summary, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def _cmd_preflight_database(args: argparse.Namespace) -> int:
    summary = summarize_database_dir(
        database_dir=Path(args.database_dir),
        page_size=args.page_size,
        catalog_pages=args.catalog_pages,
        sample_limit=args.sample_limit,
    )
    result = {
        "summary": summary,
        "preflight": evaluate_database_summary_preflight(summary),
    }
    payload = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0 if result["preflight"]["ok"] else 1


def _cmd_compare_control_files(args: argparse.Namespace) -> int:
    comparison = compare_control_files(
        Path(args.before),
        Path(args.after),
        context_bytes=args.context_bytes,
        sample_limit=args.sample_limit,
    )
    payload = json.dumps(comparison, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


def _cmd_extract_csv(args: argparse.Namespace) -> int:
    if args.metadata_json is None and args.database_dir is None:
        print(
            "dmdul: extract-csv requires --metadata-json or --database-dir",
            file=sys.stderr,
        )
        return 2
    if args.metadata_json is not None:
        metadata = CalibratedMetadata.from_json_file(Path(args.metadata_json))
    else:
        if not args.skip_preflight:
            summary = summarize_database_dir(
                database_dir=Path(args.database_dir),
                page_size=args.page_size,
                catalog_pages=args.preflight_catalog_pages,
                sample_limit=args.preflight_sample_limit,
            )
            preflight = evaluate_database_summary_preflight(summary)
            if not preflight["ok"]:
                print(
                    "dmdul: extract-csv preflight failed; "
                    "run preflight-database for the full report",
                    file=sys.stderr,
                )
                for item in preflight["fatal_codes"]:
                    print(
                        f"fatal_preflight={item['code']} count={item['count']}",
                        file=sys.stderr,
                    )
                return 1
        resolved = resolve_offline_table_metadata(
            database_dir=Path(args.database_dir),
            table_name=args.table,
            page_size=args.page_size,
            owner=args.owner,
            scan_pages=args.scan_pages,
        )
        metadata = resolved.metadata
    report = extract_csv_with_calibrated_metadata(
        metadata=metadata,
        table_name=args.table,
        output=Path(args.output),
    )
    print(f"table={report.table}")
    print(f"output={report.output}")
    print(f"rows_written={report.rows_written}")
    print(f"rows_skipped_deleted={report.rows_skipped_deleted}")
    print(f"rows_skipped_decode_error={report.rows_skipped_decode_error}")
    for error in report.decode_errors:
        print(f"decode_error={error}", file=sys.stderr)
    print(f"mode={report.mode}")
    return 0


def _cmd_resolve_table(args: argparse.Namespace) -> int:
    resolved = resolve_offline_table_metadata(
        database_dir=Path(args.database_dir),
        table_name=args.table,
        page_size=args.page_size,
        owner=args.owner,
        scan_pages=args.scan_pages,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "system_file": str(resolved.system_file),
                    "table": resolved.table.qualified_name,
                    "table_object_id": resolved.table_object_id,
                    "storage_index_id": resolved.index_child.index_id,
                    "storage": {
                        "group_id": resolved.table.storage.group_id,
                        "file_no": resolved.table.storage.file_no,
                        "root_page": resolved.table.storage.root_page,
                        "scan_pages": resolved.table.storage.scan_pages,
                    },
                    "columns": [
                        {
                            "name": column.name,
                            "type_name": column.type_name,
                            "length": column.length,
                        }
                        for column in resolved.table.columns
                    ],
                },
                indent=2,
            )
        )
        return 0
    print(f"system_file={resolved.system_file}")
    print(f"table={resolved.table.qualified_name}")
    print(f"table_object_id={resolved.table_object_id}")
    print(f"storage_index_id={resolved.index_child.index_id}")
    print(
        "storage="
        f"group:{resolved.table.storage.group_id},"
        f"file:{resolved.table.storage.file_no},"
        f"root_page:{resolved.table.storage.root_page},"
        f"scan_pages:{resolved.table.storage.scan_pages}"
    )
    print(
        "columns="
        + ",".join(
            f"{column.name}:{column.type_name}"
            for column in resolved.table.columns
        )
    )
    return 0


def _cmd_discover_files(args: argparse.Namespace) -> int:
    files = discover_data_files(Path(args.database_dir), page_size=args.page_size)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "path": str(item.path),
                        "bytes": item.bytes,
                        "page_size": item.page_size,
                        "pages": item.pages,
                        "group_raw": item.group_raw,
                        "group_id": item.group_id,
                        "file_no_hint": item.file_no_hint,
                        "page_no": item.page_no,
                        "page_kind_raw": item.page_kind_raw,
                        "system_candidate": item.is_system_candidate,
                    }
                    for item in files
                ],
                indent=2,
            )
        )
        return 0
    for item in files:
        marker = " system_candidate" if item.is_system_candidate else ""
        print(
            f"group={item.group_id} file_hint={item.file_no_hint} "
            f"group_raw=0x{item.group_raw:x} pages={item.pages} "
            f"kind=0x{item.page_kind_raw:x} path={item.path}{marker}"
        )
    return 0


def _cmd_find_sysobject(args: argparse.Namespace) -> int:
    candidates = find_sysobject_candidates(
        Path(args.system_file),
        args.object_name,
        page_size=args.page_size,
    )
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": item.name,
                        "offset": item.offset,
                        "page_no": item.page_no,
                        "page_offset": item.page_offset,
                        "score": item.score,
                        "object_ids": list(item.object_ids),
                        "likely_object_ids": list(item.likely_object_ids),
                        "preferred_object_ids": list(item.preferred_object_ids),
                        "has_schobj": item.has_schobj,
                        "has_utab": item.has_utab,
                    }
                    for item in candidates
                ],
                indent=2,
            )
        )
        return 0
    for item in candidates[: args.limit]:
        ids = ",".join(str(value) for value in item.object_ids) or "-"
        likely_ids = ",".join(str(value) for value in item.likely_object_ids) or "-"
        preferred_ids = ",".join(str(value) for value in item.preferred_object_ids) or "-"
        print(
            f"score={item.score} offset={item.offset} "
            f"page={item.page_no} page_offset={item.page_offset} "
            f"schobj={item.has_schobj} utab={item.has_utab} "
            f"preferred_object_ids={preferred_ids} "
            f"likely_object_ids={likely_ids} object_ids={ids}"
        )
    return 0 if candidates else 1


def _cmd_find_syscolumns(args: argparse.Namespace) -> int:
    candidates = find_syscolumn_candidates(
        Path(args.system_file),
        args.object_id,
        page_size=args.page_size,
    )
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "object_id": item.object_id,
                        "offset": item.offset,
                        "page_no": item.page_no,
                        "page_offset": item.page_offset,
                        "score": item.score,
                        "column_id": item.column_id,
                        "length": item.length,
                        "name": item.name,
                        "type_name": item.type_name,
                        "name_offset": item.name_offset,
                        "type_offset": item.type_offset,
                    }
                    for item in candidates
                ],
                indent=2,
            )
        )
        return 0
    for item in candidates[: args.limit]:
        column_id = "-" if item.column_id is None else str(item.column_id)
        length = "-" if item.length is None else str(item.length)
        print(
            f"score={item.score} object_id={item.object_id} "
            f"offset={item.offset} page={item.page_no} "
            f"page_offset={item.page_offset} column_id={column_id} "
            f"length={length} name={item.name} type={item.type_name}"
        )
    return 0 if candidates else 1


def _cmd_find_sysindex(args: argparse.Namespace) -> int:
    candidates = find_sysindex_candidates(
        Path(args.system_file),
        args.index_id,
        page_size=args.page_size,
    )
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "index_id": item.index_id,
                        "offset": item.offset,
                        "page_no": item.page_no,
                        "page_offset": item.page_offset,
                        "score": item.score,
                        "is_unique": item.is_unique,
                        "group_id": item.group_id,
                        "root_file": item.root_file,
                        "root_page": item.root_page,
                        "type_name": item.type_name,
                        "flag": item.flag,
                    }
                    for item in candidates
                ],
                indent=2,
            )
        )
        return 0
    for item in candidates[: args.limit]:
        print(
            f"score={item.score} index_id={item.index_id} "
            f"offset={item.offset} page={item.page_no} "
            f"page_offset={item.page_offset} unique={item.is_unique or '-'} "
            f"group={item.group_id} root_file={item.root_file} "
            f"root_page={item.root_page} type={item.type_name or '-'} "
            f"flag={item.flag}"
        )
    return 0 if candidates else 1


def _cmd_find_sysobject_indexes(args: argparse.Namespace) -> int:
    candidates = find_sysobject_index_child_candidates(
        Path(args.system_file),
        args.parent_object_id,
        page_size=args.page_size,
    )
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "parent_object_id": item.parent_object_id,
                        "index_id": item.index_id,
                        "name": item.name,
                        "offset": item.offset,
                        "page_no": item.page_no,
                        "page_offset": item.page_offset,
                        "score": item.score,
                        "type_name": item.type_name,
                        "name_offset": item.name_offset,
                        "index_id_offset": item.index_id_offset,
                    }
                    for item in candidates
                ],
                indent=2,
            )
        )
        return 0
    for item in candidates[: args.limit]:
        index_id_offset = (
            "-" if item.index_id_offset is None else str(item.index_id_offset)
        )
        print(
            f"score={item.score} parent_object_id={item.parent_object_id} "
            f"index_id={item.index_id} name={item.name} "
            f"offset={item.offset} page={item.page_no} "
            f"page_offset={item.page_offset} type={item.type_name or '-'} "
            f"name_offset={item.name_offset} index_id_offset={index_id_offset}"
        )
    return 0 if candidates else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dmdul")
    parser.add_argument(
        "--page-size",
        type=int,
        default=8192,
        help="DM data file page size in bytes, default: 8192",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    file_info = subparsers.add_parser("file-info", help="show basic file facts")
    file_info.add_argument("file")
    file_info.set_defaults(func=_cmd_file_info)

    dump_page = subparsers.add_parser("dump-page", help="hex dump one page")
    dump_page.add_argument("file")
    dump_page.add_argument("page_no", type=int)
    dump_page.add_argument("--bytes", type=int)
    dump_page.set_defaults(func=_cmd_dump_page)

    inspect_page = subparsers.add_parser(
        "inspect-page",
        help="print observed DM page header fields",
    )
    inspect_page.add_argument("file")
    inspect_page.add_argument("page_no", type=int)
    inspect_page.add_argument(
        "--dump",
        type=int,
        default=0,
        help="also hex dump this many bytes from the page",
    )
    inspect_page.add_argument(
        "--rows",
        action="store_true",
        help="also print observed row offsets and status",
    )
    inspect_page.set_defaults(func=_cmd_inspect_page)

    find = subparsers.add_parser("find", help="find an ASCII/encoded marker")
    find.add_argument("file")
    find.add_argument("marker")
    find.add_argument("--encoding", default="utf-8")
    find.set_defaults(func=_cmd_find)

    capture_evidence = subparsers.add_parser(
        "capture-evidence",
        help="capture raw page and marker evidence from a DM data file",
    )
    capture_evidence.add_argument("file")
    capture_evidence.add_argument(
        "--pages",
        default="",
        help="comma-separated page numbers and inclusive ranges, e.g. 0,1,16,96-98",
    )
    capture_evidence.add_argument(
        "--marker",
        action="append",
        help="marker string to locate; may be supplied multiple times",
    )
    capture_evidence.add_argument("--encoding", default="utf-8")
    capture_evidence.add_argument(
        "--label",
        help="human-readable evidence label, e.g. dmdul_fix_types_clean",
    )
    capture_evidence.add_argument(
        "--copy-state",
        choices=[
            "clean-shutdown",
            "storage-snapshot",
            "live-copy",
            "crash-state",
            "open-transaction",
            "unknown",
        ],
        default="unknown",
        help="how the source file set was copied",
    )
    capture_evidence.add_argument(
        "--note",
        action="append",
        help="free-form evidence note; may be supplied multiple times",
    )
    capture_evidence.add_argument(
        "--context",
        type=int,
        default=64,
        help="bytes of context to capture on each side of marker matches",
    )
    capture_evidence.add_argument("--output")
    capture_evidence.set_defaults(func=_cmd_capture_evidence)

    verify_evidence = subparsers.add_parser(
        "verify-evidence",
        help="verify an evidence manifest and referenced local files",
    )
    verify_evidence.add_argument("manifest")
    verify_evidence.set_defaults(func=_cmd_verify_evidence)

    catalog_pages = subparsers.add_parser(
        "catalog-pages",
        help="scan a DM data file and summarize observed page headers",
    )
    catalog_pages.add_argument("file")
    catalog_pages.add_argument("--start-page", type=int, default=0)
    catalog_pages.add_argument("--max-pages", type=int)
    catalog_pages.add_argument("--sample-limit", type=int, default=32)
    catalog_pages.add_argument("--output")
    catalog_pages.set_defaults(func=_cmd_catalog_pages)

    summarize_database = subparsers.add_parser(
        "summarize-database",
        help="summarize control files, DBF files, groups, and sampled page kinds",
    )
    summarize_database.add_argument("database_dir")
    summarize_database.add_argument(
        "--catalog-pages",
        type=int,
        default=64,
        help="pages to scan from each discovered file for page-kind samples",
    )
    summarize_database.add_argument("--sample-limit", type=int, default=8)
    summarize_database.add_argument("--output")
    summarize_database.set_defaults(func=_cmd_summarize_database)

    summarize_control = subparsers.add_parser(
        "summarize-control-file",
        help="summarize one dm.ctl/control file for byte-level structure research",
    )
    summarize_control.add_argument("file")
    summarize_control.add_argument("--sample-limit", type=int, default=32)
    summarize_control.add_argument("--output")
    summarize_control.set_defaults(func=_cmd_summarize_control_file)

    preflight_database = subparsers.add_parser(
        "preflight-database",
        help="run conservative preflight checks before offline extraction",
    )
    preflight_database.add_argument("database_dir")
    preflight_database.add_argument(
        "--catalog-pages",
        type=int,
        default=64,
        help="pages to scan from each discovered file for page-kind samples",
    )
    preflight_database.add_argument("--sample-limit", type=int, default=8)
    preflight_database.add_argument("--output")
    preflight_database.set_defaults(func=_cmd_preflight_database)

    compare_control_files_parser = subparsers.add_parser(
        "compare-control-files",
        help="compare two dm.ctl snapshots for byte-level structure research",
    )
    compare_control_files_parser.add_argument("before")
    compare_control_files_parser.add_argument("after")
    compare_control_files_parser.add_argument("--context-bytes", type=int, default=16)
    compare_control_files_parser.add_argument("--sample-limit", type=int, default=64)
    compare_control_files_parser.add_argument("--output")
    compare_control_files_parser.set_defaults(func=_cmd_compare_control_files)

    extract_csv = subparsers.add_parser(
        "extract-csv",
        help="extract a table to CSV",
    )
    extract_csv.add_argument("--metadata-json")
    extract_csv.add_argument("--database-dir")
    extract_csv.add_argument("--owner")
    extract_csv.add_argument(
        "--skip-preflight",
        action="store_true",
        help="skip conservative database-dir preflight checks for research use",
    )
    extract_csv.add_argument(
        "--preflight-catalog-pages",
        type=int,
        default=64,
        help="pages to sample per file during extract-csv preflight",
    )
    extract_csv.add_argument(
        "--preflight-sample-limit",
        type=int,
        default=8,
        help="sample limit during extract-csv preflight",
    )
    extract_csv.add_argument(
        "--scan-pages",
        type=int,
        default=64,
        help="temporary fallback page scan count from the storage root, default: 64",
    )
    extract_csv.add_argument("--table", required=True)
    extract_csv.add_argument("--output", required=True)
    extract_csv.set_defaults(func=_cmd_extract_csv)

    resolve_table = subparsers.add_parser(
        "resolve-table",
        help="resolve offline table metadata from a database directory",
    )
    resolve_table.add_argument("database_dir")
    resolve_table.add_argument("--table", required=True)
    resolve_table.add_argument("--owner")
    resolve_table.add_argument("--scan-pages", type=int, default=64)
    resolve_table.add_argument("--json", action="store_true")
    resolve_table.set_defaults(func=_cmd_resolve_table)

    discover_files = subparsers.add_parser(
        "discover-files",
        help="scan a database directory for DM data files",
    )
    discover_files.add_argument("database_dir")
    discover_files.add_argument("--json", action="store_true")
    discover_files.set_defaults(func=_cmd_discover_files)

    find_sysobject = subparsers.add_parser(
        "find-sysobject",
        help="heuristically find a SYSOBJECTS record in SYSTEM.DBF",
    )
    find_sysobject.add_argument("system_file")
    find_sysobject.add_argument("object_name")
    find_sysobject.add_argument("--json", action="store_true")
    find_sysobject.add_argument("--limit", type=int, default=10)
    find_sysobject.set_defaults(func=_cmd_find_sysobject)

    find_syscolumns = subparsers.add_parser(
        "find-syscolumns",
        help="heuristically find SYSCOLUMNS records in SYSTEM.DBF",
    )
    find_syscolumns.add_argument("system_file")
    find_syscolumns.add_argument("object_id", type=int)
    find_syscolumns.add_argument("--json", action="store_true")
    find_syscolumns.add_argument("--limit", type=int, default=50)
    find_syscolumns.set_defaults(func=_cmd_find_syscolumns)

    find_sysindex = subparsers.add_parser(
        "find-sysindex",
        help="heuristically find a SYSINDEXES record in SYSTEM.DBF",
    )
    find_sysindex.add_argument("system_file")
    find_sysindex.add_argument("index_id", type=int)
    find_sysindex.add_argument("--json", action="store_true")
    find_sysindex.add_argument("--limit", type=int, default=10)
    find_sysindex.set_defaults(func=_cmd_find_sysindex)

    find_sysobject_indexes = subparsers.add_parser(
        "find-sysobject-indexes",
        help="heuristically find SYSOBJECTS child INDEX objects for a table object id",
    )
    find_sysobject_indexes.add_argument("system_file")
    find_sysobject_indexes.add_argument("parent_object_id", type=int)
    find_sysobject_indexes.add_argument("--json", action="store_true")
    find_sysobject_indexes.add_argument("--limit", type=int, default=10)
    find_sysobject_indexes.set_defaults(func=_cmd_find_sysobject_indexes)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OfflineResolveError as exc:
        print(f"dmdul: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"dmdul: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
