from __future__ import annotations

import argparse
import csv
import json
import mmap
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .block import (
    analyze_data_file_block,
    dump_unknown_data_file_structures,
    load_column_meta_from_jsonl,
    parse_column_specs,
)
from .bootstrap import build_bootstrap_dicts
from .control_file import compare_control_files, summarize_control_file
from .control_map import write_control_ctl
from .dul_config import (
    FileListEntry,
    build_filelist_from_control_file,
    build_filelist_from_database_dir,
    build_filelist_from_dirs,
    load_runtime_config,
    read_filelist,
    validate_filelist,
    write_filelist,
    write_init_dul,
)
from .database_summary import summarize_database_dir
from .decode import DecodeError, decode_observed_row_values
from .discovery import discover_data_files
from .evidence import (
    capture_data_file_evidence,
    parse_page_selection,
    verify_evidence_manifest,
)
from .extract import extract_csv_with_calibrated_metadata, extract_split_parts_with_calibrated_metadata
from .metadata import CalibratedMetadata, ColumnMeta, DataFileMeta, StorageRoot, TableMeta
from .page import ObservedPageHeader, format_hex_dump
from .page_catalog import catalog_data_file_pages
from .preflight import evaluate_database_summary_preflight
from .resolver import OfflineResolveError, resolve_offline_table_metadata
from .row_archive import import_data_to_sql
from .row import iter_observed_rows, iter_observed_rows_by_slots, scan_observed_row_chain
from .storage import DataFile
from .sysdict import (
    dump_sysobject_rows,
    find_syscolumn_candidates,
    find_sysindex_candidates,
    find_sysobject_candidates,
    find_sysobject_index_child_candidates,
)




def _read_init_dul(path: Path) -> dict[str, str]:
    config: dict[str, str] = {}
    if not path.exists():
        return config
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("--"):
            key = key[2:]
        normalized_key = key.lower().replace("-", "_")
        normalized_value = value.strip().strip('"').strip("'")
        config[normalized_key] = normalized_value
    return config


def _bool_from_init(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_init_config(args: argparse.Namespace, argv: list[str]) -> dict[str, str]:
    candidates: list[Path] = []
    explicit_init = getattr(args, "init_file", None)
    if explicit_init:
        candidates.append(Path(explicit_init))
    database_dir = getattr(args, "database_dir", None)
    if database_dir:
        candidates.append(Path(database_dir) / "init.dul")
    candidates.append(Path("init.dul"))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        config = _read_init_dul(candidate)
        if config:
            return config
    return {}


def _apply_init_config(args: argparse.Namespace, argv: list[str]) -> None:
    config = _load_init_config(args, argv)
    if not config:
        return
    explicit = set(argv)
    if "--page-size" not in explicit and "page_size" in config:
        args.page_size = int(config["page_size"])
    if args.command in {"bootstrap", "bootstrap-dicts"}:
        if getattr(args, "database_dir", None) is None and "database_dir" in config:
            args.database_dir = config["database_dir"]
        if getattr(args, "database_dir", None) is None and "dirlist" in config:
            first_dir = next((item.strip() for item in config["dirlist"].split(",") if item.strip()), None)
            if first_dir:
                args.database_dir = first_dir
        if getattr(args, "output_dir", None) is None and "dict_dir" in config:
            args.output_dir = config["dict_dir"]
        if getattr(args, "output_dir", None) is None and "output_dir" in config:
            args.output_dir = config["output_dir"]
        if not getattr(args, "download_dictionaries", False):
            value = config.get("download_dictionaries") or config.get("bootstrap")
            if value is not None:
                args.download_dictionaries = _bool_from_init(value)
        if getattr(args, "owner", None) is None and "owner" in config:
            args.owner = config["owner"]
        if "scan_pages" in config and "--scan-pages" not in explicit:
            args.scan_pages = int(config["scan_pages"])
        if "catalog_pages" in config and "--catalog-pages" not in explicit:
            args.catalog_pages = int(config["catalog_pages"])
        if "sample_limit" in config and "--sample-limit" not in explicit:
            args.sample_limit = int(config["sample_limit"])
        if not getattr(args, "table", None) and "tables" in config:
            args.table = [item.strip() for item in config["tables"].split(",") if item.strip()]
    if args.command in {"dump-data"}:
        if getattr(args, "dict_dir", None) is None and "dict_dir" in config:
            args.dict_dir = config["dict_dir"]
        if getattr(args, "output_dir", None) is None and "output_dir" in config:
            args.output_dir = config["output_dir"]
        if getattr(args, "parallel", None) is None and "parallel" in config:
            args.parallel = int(config["parallel"])
        if getattr(args, "delimiter", None) is None and "data_delimiter" in config:
            args.delimiter = config["data_delimiter"]


def _cmd_prepare(args: argparse.Namespace) -> int:
    init_path = Path(args.init_output)
    filelist_path = Path(args.filelist_output)
    dirlist = tuple(Path(item.strip()) for item in (args.dirlist or "").split(",") if item.strip())
    if args.control_file:
        control_file = Path(args.control_file)
        if not dirlist:
            if args.database_dir:
                dirlist = (Path(args.database_dir),)
            else:
                dirlist = (control_file.parent,)
        entries = build_filelist_from_control_file(
            control_file=control_file,
            search_dirs=dirlist,
            page_size=args.page_size,
            sample_limit=args.sample_limit,
        )
    elif args.database_dir:
        entries = build_filelist_from_database_dir(
            database_dir=Path(args.database_dir),
            page_size=args.page_size,
            sample_limit=args.sample_limit,
        )
        if not dirlist:
            dirlist = (Path(args.database_dir),)
    else:
        entries = build_filelist_from_dirs(
            dirlist=dirlist or (Path("."),),
            page_size=args.page_size,
        )
    write_filelist(filelist_path, entries)
    write_init_dul(
        init_path,
        {
            "filelist": str(filelist_path),
            "dirlist": ",".join(str(item) for item in dirlist),
            "output_dir": args.output_dir,
            "dict_dir": args.dict_dir or args.output_dir,
            "parallel": str(args.parallel),
            "page_size": str(args.page_size),
            "data_delimiter": args.delimiter,
        },
    )
    diagnostics = validate_filelist(entries)
    manifest = {
        "mode": "dm-dul-prepare",
        "init_file": str(init_path),
        "filelist": str(filelist_path),
        "files_total": len(entries),
        "diagnostics": diagnostics,
    }
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(f"init={init_path}")
        print(f"filelist={filelist_path}")
        print(f"files_total={len(entries)}")
    return 0 if not any(item.get("level") == "error" for item in diagnostics) else 1


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


def _cmd_analyze_block(args: argparse.Namespace) -> int:
    columns = _load_analysis_columns(args)
    analysis = analyze_data_file_block(
        path=Path(args.file),
        page_no=args.page_no,
        page_size=args.page_size,
        object_id=args.object_id,
        columns=columns,
        row_start_offset=args.row_start_offset,
        max_rows=args.max_rows,
        candidate_scan_bytes=args.candidate_scan_bytes,
    )
    print(json.dumps(analysis, indent=2))
    return 0


def _cmd_dump_unknown_structures(args: argparse.Namespace) -> int:
    pages = parse_page_selection(args.pages)
    payload = dump_unknown_data_file_structures(
        path=Path(args.file),
        pages=pages,
        page_size=args.page_size,
        row_start_offset=args.row_start_offset,
        max_rows=args.max_rows,
        tail_scan_bytes=args.tail_scan_bytes,
        chunk_sizes=tuple(args.chunk_size),
    )
    output = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
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


def _cmd_write_control_ctl(args: argparse.Namespace) -> int:
    if args.control_file:
        dirlist = tuple(Path(item.strip()) for item in (args.dirlist or "").split(",") if item.strip())
        if not dirlist:
            if args.database_dir:
                dirlist = (Path(args.database_dir),)
            else:
                dirlist = (Path(args.control_file).parent,)
        entries = build_filelist_from_control_file(
            control_file=Path(args.control_file),
            search_dirs=dirlist,
            page_size=args.page_size,
            sample_limit=args.sample_limit,
        )
        write_filelist(Path(args.output), entries)
        manifest = _filelist_manifest_from_entries(
            mode="dm-control-filelist",
            output=Path(args.output),
            entries=entries,
            diagnostics=validate_filelist(entries),
        )
    else:
        if args.database_dir is None:
            print(
                "dmdul: write-control-ctl requires database_dir or --control-file",
                file=sys.stderr,
            )
            return 2
        manifest = write_control_ctl(
            database_dir=Path(args.database_dir),
            output=Path(args.output),
            page_size=args.page_size,
            sample_limit=args.sample_limit,
        )
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(f"output={manifest['output']}")
        print(f"rows={manifest['rows_total']}")
    return 0 if manifest["rows_total"] else 1


def _filelist_manifest_from_entries(
    *,
    mode: str,
    output: Path,
    entries: tuple[FileListEntry, ...],
    diagnostics: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "mode": mode,
        "output": str(output),
        "rows_total": len(entries),
        "rows": [
            {
                "tablespace_id": entry.group_id,
                "file_id": entry.file_id,
                "path": str(entry.path),
            }
            for entry in entries
        ],
        "diagnostics": diagnostics,
    }


def _cmd_bootstrap_dicts(args: argparse.Namespace) -> int:
    if args.command == "bootstrap":
        args.download_dictionaries = True
    if args.database_dir is None or args.output_dir is None:
        print(
            "dmdul: bootstrap requires database_dir and --output-dir, either on the command line or in init.dul",
            file=sys.stderr,
        )
        return 2
    progress = None if args.json else _bootstrap_progress_printer()
    if progress is not None:
        progress(
            "start: "
            f"database_dir={args.database_dir} "
            f"output_dir={args.output_dir} "
            f"page_size={args.page_size} "
            f"download_dictionaries={args.experimental_heuristic_dicts or args.download_dictionaries}"
        )
    manifest = build_bootstrap_dicts(
        database_dir=Path(args.database_dir),
        output_dir=Path(args.output_dir),
        page_size=args.page_size,
        catalog_pages=args.catalog_pages,
        sample_limit=args.sample_limit,
        tables=tuple(args.table or ()),
        owner=args.owner,
        scan_pages=args.scan_pages,
        experimental_heuristic_dicts=args.experimental_heuristic_dicts or args.download_dictionaries,
        source_dict_dir=None if args.source_dict_dir is None else Path(args.source_dict_dir),
        progress=progress,
    )
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(f"manifest={manifest['manifest_path']}")
        for name, path in manifest["dict_files"].items():
            print(f"{name}={path} rows={manifest['rows'][name]}")
    return 0



def _bootstrap_progress_printer():
    def progress(message: str) -> None:
        print(f"[bootstrap] {message}", file=sys.stderr, flush=True)

    return progress


def _dump_data_progress_printer():
    def progress(event: dict[str, object]) -> None:
        kind = str(event.get("event", ""))
        table = str(event.get("table", "-"))
        if kind == "storage_scan_file_start":
            print(
                "[dump-data] "
                f"scan-storage table={table} "
                f"file_no={event.get('file_no')} "
                f"pages={event.get('pages_total')} "
                f"path={event.get('path')}",
                file=sys.stderr,
                flush=True,
            )
        elif kind == "storage_scan_file_done":
            print(
                "[dump-data] "
                f"scan-storage-done table={table} "
                f"file_no={event.get('file_no')} "
                f"header_hits={event.get('header_hits')} "
                f"pages_planned={event.get('pages_planned')} "
                f"pages_planned_total={event.get('pages_planned_total')}",
                file=sys.stderr,
                flush=True,
            )
        elif kind == "storage_scan_progress":
            pages_scanned = int(event.get("pages_scanned") or 0)
            pages_total = int(event.get("pages_total") or 0)
            percent = (pages_scanned * 100.0 / pages_total) if pages_total else 0.0
            print(
                "[dump-data] "
                f"scan-storage-progress table={table} "
                f"file_no={event.get('file_no')} "
                f"pages={pages_scanned}/{pages_total} "
                f"percent={percent:.1f} "
                f"header_hits={event.get('header_hits')} "
                f"pages_planned={event.get('pages_planned')}",
                file=sys.stderr,
                flush=True,
            )
        elif kind == "plan":
            print(
                "[dump-data] "
                f"plan table={table} pages_total={event.get('pages_total')} "
                f"output={event.get('output')}",
                file=sys.stderr,
                flush=True,
            )
        elif kind == "block":
            print(
                "[dump-data] "
                f"block table={table} "
                f"pages={event.get('pages_done')}/{event.get('pages_total')} "
                f"file_no={event.get('file_no')} "
                f"page_no={event.get('page_no')} "
                f"rows={event.get('rows_written')} "
                f"output={event.get('output')}",
                file=sys.stderr,
                flush=True,
            )
        elif kind == "complete":
            print(
                "[dump-data] "
                f"complete table={table} ok={str(event.get('ok')).lower()} "
                f"pages={event.get('pages_done')} "
                f"rows={event.get('rows_written')} "
                f"rows_skipped_deleted={event.get('rows_skipped_deleted')} "
                f"rows_skipped_decode_error={event.get('rows_skipped_decode_error')} "
                f"output={event.get('output')}",
                file=sys.stderr,
                flush=True,
            )

    return progress


def _cmd_dump_data(args: argparse.Namespace) -> int:
    if args.dict_dir is None or args.output_dir is None:
        runtime = load_runtime_config(Path(args.init_file) if args.init_file else None)
        dict_dir = Path(args.dict_dir) if args.dict_dir else runtime.dict_dir
        output_dir = Path(args.output_dir) if args.output_dir else runtime.output_dir
        delimiter = args.delimiter or runtime.data_delimiter
        workers = args.parallel or runtime.parallel
    else:
        dict_dir = Path(args.dict_dir)
        output_dir = Path(args.output_dir)
        delimiter = args.delimiter or "|"
        workers = args.parallel or 1
    metadata = CalibratedMetadata.from_dict_dir(dict_dir)
    requested = {_normalize_identifier(value) for value in (args.table or ())}
    requested_names = {value.split(".", 1)[-1] for value in requested}
    requested_user = _normalize_identifier(args.user) if args.user else None
    tables = [
        table
        for table in metadata.tables
        if (
            (not requested and requested_user is None)
            or _normalize_identifier(table.name) in requested
            or _normalize_identifier(table.name) in requested_names
            or _normalize_identifier(table.qualified_name) in requested
            or (requested_user is not None and _normalize_identifier(table.owner) == requested_user)
        )
    ]
    partition_names = _partition_names(args.partition)
    if partition_names and len(tables) != 1:
        print(
            "dmdul: --partition requires exactly one matched table",
            file=sys.stderr,
        )
        return 2
    recovery_storage_ids: tuple[int, ...] = ()
    if args.orphan_scan_storage_id is not None:
        recovery_storage_ids = (args.orphan_scan_storage_id,)
    if args.truncate and not recovery_storage_ids and len(tables) == 1:
        recovery_storage_ids = _truncate_recovery_storage_ids_from_tab_dict(
            dict_dir=dict_dir,
            table=tables[0],
        )
        if not recovery_storage_ids:
            print(
                "dmdul: --truncate could not determine storage_index_id/storage_index_ids from tab.dict; use --orphan-scan-storage-id explicitly",
                file=sys.stderr,
            )
            return 2
    if recovery_storage_ids or args.truncate:
        if len(tables) != 1:
            print(
                "dmdul: truncate/orphan recovery requires exactly one matched table",
                file=sys.stderr,
            )
            return 2
        if partition_names:
            print(
                "dmdul: --orphan-scan-storage-id cannot be combined with --partition",
                file=sys.stderr,
            )
            return 2
        if max(1, args.partition_parallel) > 1:
            print(
                "dmdul: --orphan-scan-storage-id cannot be combined with --partition-parallel",
                file=sys.stderr,
            )
            return 2
    output_dir.mkdir(parents=True, exist_ok=True)
    progress = None if args.json else _dump_data_progress_printer()
    if progress is not None:
        print(
            f"[dump-data] start tables_total={len(tables)} parallel={max(1, workers)} partition_parallel={max(1, args.partition_parallel)} output_dir={output_dir}",
            file=sys.stderr,
            flush=True,
        )
    extract_func = (
        extract_split_parts_with_calibrated_metadata
        if max(1, args.partition_parallel) > 1
        else extract_csv_with_calibrated_metadata
    )
    reports = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                extract_func,
                metadata=metadata,
                table_name=table.qualified_name,
                output=output_dir / f"{_safe_output_name(table.qualified_name)}.{_output_extension(args.output_format)}",
                page_plan_fallback_level="error" if args.strict_page_plan else "warning",
                delimiter=delimiter,
                lob_mode=args.lob_mode,
                lob_hash=args.lob_hash,
                output_format=args.output_format,
                partition_names=partition_names,
                empty_page_plan_level="error",
                **(
                    {"part_workers": max(1, args.partition_parallel)}
                    if max(1, args.partition_parallel) > 1
                    else {
                        "include_sql_header": True,
                        "orphan_scan_storage_ids": recovery_storage_ids,
                    }
                ),
                progress=progress,
            ): table
            for table in tables
        }
        for future in as_completed(futures):
            table = futures[future]
            try:
                reports.append(future.result().as_dict())
            except Exception as exc:  # pragma: no cover
                reports.append(
                    {
                        "table": table.qualified_name,
                        "ok": False,
                        "output": str(output_dir / f"{_safe_output_name(table.qualified_name)}.{_output_extension(args.output_format)}"),
                        "rows_written": 0,
                        "diagnostics": [{"level": "error", "code": "dump-data-table-failed", "message": str(exc)}],
                    }
                )
    reports.sort(key=lambda item: str(item["table"]))
    manifest = {
        "mode": "dm-dump-data",
        "dict_dir": str(dict_dir),
        "output_dir": str(output_dir),
        "delimiter": delimiter,
        "output_format": args.output_format,
        "parallel": max(1, workers),
        "partition_parallel": max(1, args.partition_parallel),
        "tables_total": len(tables),
        "tables_ok": sum(1 for item in reports if item.get("ok")),
        "tables_failed": sum(1 for item in reports if not item.get("ok")),
        "reports": reports,
    }
    if args.report_output:
        Path(args.report_output).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        _print_dump_data_summary(manifest)
    return 0 if manifest["tables_failed"] == 0 else 1


def _cmd_extract_dicts(args: argparse.Namespace) -> int:
    metadata = CalibratedMetadata.from_dict_dir(Path(args.dict_dir))
    requested = {_normalize_identifier(value) for value in (args.table or ())}
    requested_names = {value.split(".", 1)[-1] for value in requested}
    tables = [
        table
        for table in metadata.tables
        if not requested
        or _normalize_identifier(table.name) in requested
        or _normalize_identifier(table.name) in requested_names
        or _normalize_identifier(table.qualified_name) in requested
    ]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    max_workers = max(1, args.workers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                extract_csv_with_calibrated_metadata,
                metadata=metadata,
                table_name=table.qualified_name,
                output=output_dir / f"{_safe_output_name(table.qualified_name)}.csv",
                page_plan_fallback_level="error" if args.strict_page_plan else "warning",
                lob_mode=args.lob_mode,
                lob_hash=args.lob_hash,
            ): table
            for table in tables
        }
        for future in as_completed(futures):
            table = futures[future]
            try:
                report = future.result()
            except Exception as exc:  # pragma: no cover - defensive report path
                reports.append(
                    {
                        "table": table.qualified_name,
                        "ok": False,
                        "output": str(output_dir / f"{_safe_output_name(table.qualified_name)}.csv"),
                        "rows_written": 0,
                        "diagnostics": [
                            {
                                "level": "error",
                                "code": "extract-dicts-table-failed",
                                "message": str(exc),
                            }
                        ],
                        "mode": "extract-dicts-error",
                    }
                )
                continue
            reports.append(report.as_dict())
    reports.sort(key=lambda item: str(item["table"]))
    manifest = {
        "mode": "dm-extract-dicts",
        "dict_dir": str(args.dict_dir),
        "output_dir": str(output_dir),
        "workers": max_workers,
        "tables_total": len(tables),
        "tables_ok": sum(1 for item in reports if item.get("ok")),
        "tables_failed": sum(1 for item in reports if not item.get("ok")),
        "reports": reports,
    }
    if args.report_output:
        Path(args.report_output).write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(f"tables_total={manifest['tables_total']}")
        print(f"tables_ok={manifest['tables_ok']}")
        print(f"tables_failed={manifest['tables_failed']}")
        _print_table_failure_details(reports)
    return 0 if manifest["tables_failed"] == 0 else 1


def _print_dump_data_summary(manifest: dict[str, object]) -> None:
    reports = manifest.get("reports", ()) or ()
    print("dump_data_summary")
    print(f"  tables_total={manifest['tables_total']}")
    print(f"  tables_ok={manifest['tables_ok']}")
    print(f"  tables_failed={manifest['tables_failed']}")
    for report in reports:
        if not isinstance(report, dict):
            continue
        status = "OK" if report.get("ok") else "FAILED"
        print(f"table={report.get('table')} status={status}")
        print(f"  output={report.get('output')}")
        print(f"  rows_written={report.get('rows_written', 0)}")
        print(f"  rows_skipped_deleted={report.get('rows_skipped_deleted', 0)}")
        print(f"  rows_skipped_decode_error={report.get('rows_skipped_decode_error', 0)}")
        print(f"  pages_scanned={len(report.get('scanned_page_refs', ()) or ())}")
        diagnostics = report.get("diagnostics", ()) or ()
        error_count = sum(
            1
            for item in diagnostics
            if isinstance(item, dict) and item.get("level") == "error"
        )
        warning_count = sum(
            1
            for item in diagnostics
            if isinstance(item, dict) and item.get("level") == "warning"
        )
        print(f"  diagnostics_errors={error_count}")
        print(f"  diagnostics_warnings={warning_count}")
        if not report.get("ok"):
            _print_table_failure_details(report)


def _print_table_failure_details(report: dict[str, object]) -> None:
    for diagnostic in report.get("diagnostics", ()) or ():
        if not isinstance(diagnostic, dict):
            continue
        code = diagnostic.get("code", "")
        level = diagnostic.get("level", "")
        message = diagnostic.get("message", "")
        print(
            f"  diagnostic={code} level={level} message={message}",
            file=sys.stderr,
        )
    for error in report.get("decode_errors", ()) or ():
        print(f"  decode_error={error}", file=sys.stderr)


def _truncate_recovery_storage_ids_from_tab_dict(
    *,
    dict_dir: Path,
    table: object,
) -> tuple[int, ...]:
    tab_path = dict_dir / "tab.dict"
    if not tab_path.exists():
        return ()
    table_owner = _normalize_identifier(getattr(table, "owner", ""))
    table_name = _normalize_identifier(getattr(table, "name", ""))
    table_qualified = _normalize_identifier(getattr(table, "qualified_name", ""))
    with tab_path.open("r", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            owner = _normalize_identifier(row.get("owner") or "")
            name = _normalize_identifier(row.get("name") or "")
            qualified = _normalize_identifier(row.get("qualified_name") or "")
            if not (
                (owner == table_owner and name == table_name)
                or qualified == table_qualified
            ):
                continue
            values = _storage_id_values_from_tab_row(row)
            if not values:
                return ()
            return values
    return ()


def _storage_id_values_from_tab_row(row: dict[str, str]) -> tuple[int, ...]:
    raw_values = []
    storage_index_id = (row.get("storage_index_id") or "").strip()
    if storage_index_id:
        raw_values.append(storage_index_id)
    storage_index_ids = (row.get("storage_index_ids") or "").strip()
    if storage_index_ids:
        raw_values.extend(item.strip() for item in storage_index_ids.split(";") if item.strip())
    result: list[int] = []
    seen: set[int] = set()
    for value in raw_values:
        try:
            storage_id = int(value)
        except ValueError as exc:
            table_name = row.get("qualified_name") or row.get("name") or "<unknown>"
            raise ValueError(f"invalid storage id for {table_name}: {value}") from exc
        if storage_id in seen:
            continue
        seen.add(storage_id)
        result.append(storage_id)
    return tuple(result)


def _cmd_scan_orphan_storages(args: argparse.Namespace) -> int:
    dict_dir = Path(args.dict_dir)
    tablespaces = _tablespace_filters(args.tablespace)
    if tablespaces and not _file_dict_has_tablespace_mapping(dict_dir / "file.dict"):
        print(
            "dmdul: --tablespace requires tablespace_name/tablespace/group_name in file.dict; "
            "use --group-id or refresh dictionaries with tablespace metadata",
            file=sys.stderr,
        )
        return 2
    known_storage_ids = _known_storage_ids_from_tab_dict(dict_dir / "tab.dict")
    candidates = _scan_orphan_storage_candidates(
        dict_dir=dict_dir,
        known_storage_ids=known_storage_ids,
        group_id=args.group_id,
        tablespaces=tablespaces,
        min_pages=max(1, args.min_pages),
        sample_rows=max(0, args.sample_rows),
    )
    payload = {
        "mode": "dm-scan-orphan-storages",
        "dict_dir": str(dict_dir),
        "group_id": args.group_id,
        "tablespaces": list(tablespaces),
        "known_storage_ids": len(known_storage_ids),
        "candidates": candidates,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"known_storage_ids={payload['known_storage_ids']}")
        for item in candidates:
            print(
                f"storage_id={item['storage_id']} group_id={item['group_id']} "
                f"file_no={item['file_no']} pages={item['pages']} "
                f"first_pages={','.join(str(p) for p in item['first_pages'])}"
            )
            for sample in item.get("row_samples", ()):
                print(
                    f"  sample page={sample['page_no']} offset={sample['offset']} "
                    f"len={sample['len']} ascii={sample['ascii_hint']}"
                )
    return 0


def _cmd_recover_orphan_table(args: argparse.Namespace) -> int:
    dict_dir = Path(args.dict_dir)
    output_dir = Path(args.output_dir)
    columns = _load_analysis_columns(args)
    tablespaces = _tablespace_filters(args.tablespace)
    if tablespaces and not _file_dict_has_tablespace_mapping(dict_dir / "file.dict"):
        print(
            "dmdul: --tablespace requires tablespace_name/tablespace/group_name in file.dict; "
            "use --group-id or refresh dictionaries with tablespace metadata",
            file=sys.stderr,
        )
        return 2
    known_storage_ids = _known_storage_ids_from_tab_dict(dict_dir / "tab.dict")
    candidates = _scan_orphan_storage_candidates(
        dict_dir=dict_dir,
        known_storage_ids=known_storage_ids,
        group_id=args.group_id,
        tablespaces=tablespaces,
        min_pages=max(1, args.min_pages),
        sample_rows=0,
    )
    if args.storage_id is not None:
        candidates = [
            item for item in candidates if int(item["storage_id"]) == args.storage_id
        ]
    if not candidates:
        print("dmdul: no orphan storage candidates matched recovery criteria", file=sys.stderr)
        return 1

    if columns:
        scored = [
            _score_orphan_candidate_columns(
                dict_dir=dict_dir,
                candidate=item,
                columns=columns,
                sample_rows=max(1, args.sample_rows),
            )
            for item in candidates
        ]
        scored.sort(
            key=lambda item: (
                -int(item["decode_ok"]),
                int(item["decode_errors"]),
                -int(item["pages"]),
                int(item["storage_id"]),
            )
        )
        best = scored[0]
        if int(best["decode_ok"]) == 0:
            payload = {
                "mode": "dm-recover-orphan-table",
                "status": "no-decodable-candidate",
                "candidates": scored[: args.max_candidates],
            }
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print("dmdul: no candidate rows decoded with the supplied columns", file=sys.stderr)
            return 1
        table_name = args.table_name or f"tab_{best['storage_id']}"
        metadata = _metadata_for_orphan_candidate(
            dict_dir=dict_dir,
            candidate=best,
            table_name=table_name,
            columns=columns,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".row" if args.output_format == "row" else ".dul"
        output = output_dir / f"{table_name}{suffix}"
        report = extract_csv_with_calibrated_metadata(
            metadata=metadata,
            table_name=table_name,
            output=output,
            include_sql_header=args.output_format == "dul",
            delimiter=args.delimiter,
            output_format=args.output_format,
            orphan_scan_storage_id=int(best["storage_id"]),
            lob_mode=args.lob_mode,
            lob_dir=output_dir / "lob",
            lob_hash=args.lob_hash,
            initial_diagnostics=(
                {
                    "level": "info",
                    "code": "orphan-storage-column-fit",
                    "message": "orphan storage was selected by decoding row samples with supplied columns",
                    "decode_ok": int(best["decode_ok"]),
                    "decode_errors": int(best["decode_errors"]),
                    "sample_rows": int(best["sample_rows"]),
                },
            ),
        )
        payload = {
            "mode": "dm-recover-orphan-table",
            "status": "exported",
            "selected_candidate": best,
            "output": str(output),
            "report": report.as_dict(),
        }
        if args.report_output:
            Path(args.report_output).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"selected_storage_id={best['storage_id']}")
            print(f"table={table_name}")
            print(f"output={output}")
            print(f"rows_written={report.rows_written}")
            print(f"rows_skipped_decode_error={report.rows_skipped_decode_error}")
        return 0 if report.ok else 1

    selected = candidates[0]
    schema = _infer_raw_orphan_schema(
        dict_dir=dict_dir,
        candidate=selected,
        sample_rows=max(1, args.sample_rows),
        table_name=args.table_name or f"tab_{selected['storage_id']}",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    schema_path = output_dir / f"{schema['table_name']}.schema.sql"
    schema_path.write_text(str(schema["create_table_sql"]) + "\n", encoding="utf-8")
    raw_output = output_dir / f"{schema['table_name']}.raw.dul"
    raw_rows = _write_raw_orphan_rows(
        dict_dir=dict_dir,
        candidate=selected,
        table_name=str(schema["table_name"]),
        output=raw_output,
    )
    payload = {
        "mode": "dm-recover-orphan-table",
        "status": "raw-exported",
        "selected_candidate": selected,
        "schema_output": str(schema_path),
        "raw_output": str(raw_output),
        "raw_rows_written": raw_rows,
        "schema": schema,
    }
    if args.report_output:
        Path(args.report_output).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"selected_storage_id={selected['storage_id']}")
        print(f"schema_output={schema_path}")
        print(f"raw_output={raw_output}")
        print(f"raw_rows_written={raw_rows}")
        print(schema["create_table_sql"])
    return 0


def _known_storage_ids_from_tab_dict(path: Path) -> set[int]:
    known: set[int] = set()
    if not path.exists():
        return known
    with path.open("r", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            known.update(_storage_id_values_from_tab_row(row))
    return known


def _file_dict_has_tablespace_mapping(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        return any(field in (reader.fieldnames or ()) for field in ("tablespace_name", "tablespace", "group_name"))


def _scan_orphan_storage_candidates(
    *,
    dict_dir: Path,
    known_storage_ids: set[int],
    group_id: int | None,
    tablespaces: tuple[str, ...],
    min_pages: int,
    sample_rows: int,
) -> list[dict[str, object]]:
    by_storage: dict[tuple[int, int, int], dict[str, object]] = {}
    file_dict = dict_dir / "file.dict"
    if not file_dict.exists():
        return []
    with file_dict.open("r", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            row_group_id = int(row.get("group_id") or 0)
            if group_id is not None and row_group_id != group_id:
                continue
            if tablespaces and _normalize_tablespace_name(_file_row_tablespace_name(row)) not in tablespaces:
                continue
            file_no = int(row.get("file_no") or 0)
            path = Path(row["path"])
            page_size = int(row.get("page_size") or 8192)
            if not path.exists():
                continue
            pages_total = path.stat().st_size // page_size
            with path.open("rb") as data_file:
                with mmap.mmap(data_file.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    for page_no in range(pages_total):
                        page_start = page_no * page_size
                        if int.from_bytes(mm[page_start + 0x14 : page_start + 0x18], "little") != 0x14:
                            continue
                        header_file_no = int.from_bytes(mm[page_start : page_start + 4], "little") >> 16
                        header_page_no = int.from_bytes(mm[page_start + 4 : page_start + 8], "little")
                        if header_file_no != file_no or header_page_no != page_no:
                            continue
                        storage_id = int.from_bytes(mm[page_start + 0x3A : page_start + 0x3E], "little")
                        if storage_id == 0 or storage_id in known_storage_ids:
                            continue
                        key = (row_group_id, file_no, storage_id)
                        item = by_storage.setdefault(
                            key,
                            {
                                "storage_id": storage_id,
                                "group_id": row_group_id,
                                "file_no": file_no,
                                "path": str(path),
                                "pages": 0,
                                "first_pages": [],
                                "row_samples": [],
                            },
                        )
                        item["pages"] = int(item["pages"]) + 1
                        first_pages = item["first_pages"]
                        assert isinstance(first_pages, list)
                        if len(first_pages) < 8:
                            first_pages.append(page_no)
                        row_samples = item["row_samples"]
                        assert isinstance(row_samples, list)
                        if len(row_samples) < sample_rows:
                            page = mm[page_start : page_start + page_size]
                            rows = iter_observed_rows_by_slots(page) or [
                                row for row in scan_observed_row_chain(page) if not row.is_deleted
                            ]
                            for observed_row in rows:
                                raw = observed_row.data[:96]
                                row_samples.append(
                                    {
                                        "page_no": page_no,
                                        "offset": observed_row.page_offset,
                                        "deleted": observed_row.is_deleted,
                                        "len": observed_row.length,
                                        "raw_hex": raw.hex(),
                                        "ascii_hint": _ascii_hint(raw),
                                    }
                                )
                                if len(row_samples) >= sample_rows:
                                    break
    candidates = [
        item
        for item in by_storage.values()
        if int(item["pages"]) >= min_pages
    ]
    candidates.sort(key=lambda item: (-int(item["pages"]), int(item["storage_id"])))
    return candidates


def _score_orphan_candidate_columns(
    *,
    dict_dir: Path,
    candidate: dict[str, object],
    columns: tuple[ColumnMeta, ...],
    sample_rows: int,
) -> dict[str, object]:
    rows = _sample_rows_for_storage_id(
        dict_dir=dict_dir,
        group_id=int(candidate["group_id"]),
        file_no=int(candidate["file_no"]),
        storage_id=int(candidate["storage_id"]),
        sample_rows=sample_rows,
    )
    decode_ok = 0
    decode_errors = 0
    first_errors: list[str] = []
    for row in rows:
        if row.is_deleted:
            continue
        try:
            decode_observed_row_values(row, columns)
            decode_ok += 1
        except DecodeError as exc:
            decode_errors += 1
            if len(first_errors) < 5:
                first_errors.append(str(exc))
        except ValueError as exc:
            decode_errors += 1
            if len(first_errors) < 5:
                first_errors.append(str(exc))
    result = dict(candidate)
    result.update(
        {
            "sample_rows": len(rows),
            "decode_ok": decode_ok,
            "decode_errors": decode_errors,
            "first_decode_errors": first_errors,
        }
    )
    return result


def _metadata_for_orphan_candidate(
    *,
    dict_dir: Path,
    candidate: dict[str, object],
    table_name: str,
    columns: tuple[ColumnMeta, ...],
) -> CalibratedMetadata:
    group_id = int(candidate["group_id"])
    file_no = int(candidate["file_no"])
    data_files = tuple(_data_file_meta_for_group(dict_dir=dict_dir, group_id=group_id))
    table = TableMeta(
        owner="",
        name=table_name,
        columns=columns,
        storage=StorageRoot(
            group_id=group_id,
            file_no=file_no,
            root_page=0,
            scan_pages=1,
            storage_id=int(candidate["storage_id"]),
        ),
    )
    return CalibratedMetadata(data_files=data_files, tables=(table,))


def _infer_raw_orphan_schema(
    *,
    dict_dir: Path,
    candidate: dict[str, object],
    sample_rows: int,
    table_name: str,
) -> dict[str, object]:
    rows = _sample_rows_for_storage_id(
        dict_dir=dict_dir,
        group_id=int(candidate["group_id"]),
        file_no=int(candidate["file_no"]),
        storage_id=int(candidate["storage_id"]),
        sample_rows=sample_rows,
    )
    row_lengths = [row.length for row in rows if not row.is_deleted]
    max_row_len = max(row_lengths, default=1)
    inferred_columns = _infer_columns_from_observed_rows(rows)
    ddl_columns = [
        {
            "name": "raw_row",
            "type_name": "VARBINARY",
            "length": max(1, max_row_len),
            "source": "full_physical_row_bytes",
        }
    ]
    strategy = "raw-export-with-heuristic-field-report" if inferred_columns else "raw-row-single-column"
    confidence = "medium" if inferred_columns and len(rows) >= 8 else "low"
    reason = (
        "guessed recovery exports only full physical row bytes; inferred_columns "
        "are advisory and are not used to transform row data"
    )
    create_sql = _create_inferred_table_sql(table_name, ddl_columns)
    return {
        "table_name": table_name,
        "strategy": strategy,
        "confidence": confidence,
        "reason": reason,
        "sample_rows": len(rows),
        "max_row_length": max_row_len,
        "columns": ddl_columns,
        "inferred_columns": inferred_columns,
        "create_table_sql": create_sql,
    }


def _infer_columns_from_observed_rows(rows: list[object]) -> list[dict[str, object]]:
    active_rows = [row for row in rows if not row.is_deleted and len(row.data) >= 4]
    if not active_rows:
        return []
    if any(row.data[2] != 0 for row in active_rows):
        return []
    payloads = [row.data[3:] for row in active_rows]
    offset = 0
    columns: list[dict[str, object]] = []
    while offset < min(len(payload) for payload in payloads) and len(columns) < 64:
        if all(_all_zero(payload[offset:]) for payload in payloads):
            break
        inferred = _infer_field_at_offset(payloads, offset, len(columns) + 1)
        if inferred is None:
            break
        columns.append(inferred["column"])
        offset += int(inferred["consumed"])
    return columns


def _infer_field_at_offset(
    payloads: list[bytes],
    offset: int,
    ordinal: int,
) -> dict[str, object] | None:
    variable_payloads: list[bytes] = []
    variable_consumed: list[int] = []
    variable_ok = True
    for payload in payloads:
        try:
            decoded = _decode_var_prefix_for_inference(payload[offset:])
        except ValueError:
            variable_ok = False
            break
        end = offset + decoded.encoded_size + decoded.length
        if end > len(payload):
            variable_ok = False
            break
        variable_payloads.append(payload[offset + decoded.encoded_size : end])
        variable_consumed.append(decoded.encoded_size + decoded.length)
    if variable_ok and len(set(variable_consumed)) <= max(2, len(variable_consumed)):
        if all(_looks_like_number_payload(raw) for raw in variable_payloads):
            return {
                "consumed": max(variable_consumed),
                "column": {
                    "name": f"col{ordinal}",
                    "type_name": "NUMBER",
                    "source": "inferred_var_number",
                    "confidence": "medium",
                },
            }
        if all(_looks_like_text(raw) for raw in variable_payloads):
            max_len = max((len(raw) for raw in variable_payloads), default=1)
            return {
                "consumed": max(variable_consumed),
                "column": {
                    "name": f"col{ordinal}",
                    "type_name": "VARCHAR",
                    "length": max(1, max_len),
                    "source": "inferred_var_text",
                    "confidence": "medium",
                },
            }
        if all(variable_consumed):
            max_len = max((len(raw) for raw in variable_payloads), default=1)
            return {
                "consumed": max(variable_consumed),
                "column": {
                    "name": f"col{ordinal}",
                    "type_name": "VARBINARY",
                    "length": max(1, max_len),
                    "source": "inferred_var_binary",
                    "confidence": "low",
                },
            }
    if _fixed_slice_available(payloads, offset, 8) and all(
        _looks_like_timestamp(payload[offset : offset + 8]) for payload in payloads
    ):
        return {
            "consumed": 8,
            "column": {
                "name": f"col{ordinal}",
                "type_name": "TIMESTAMP",
                "source": "inferred_fixed_timestamp",
                "confidence": "medium",
            },
        }
    if _fixed_slice_available(payloads, offset, 3) and all(
        _looks_like_date(payload[offset : offset + 3]) for payload in payloads
    ):
        return {
            "consumed": 3,
            "column": {
                "name": f"col{ordinal}",
                "type_name": "DATE",
                "source": "inferred_fixed_date",
                "confidence": "medium",
            },
        }
    if _fixed_slice_available(payloads, offset, 4):
        values = [
            int.from_bytes(payload[offset : offset + 4], "little", signed=True)
            for payload in payloads
        ]
        if any(value != 0 for value in values) and all(abs(value) < 2_000_000_000 for value in values):
            return {
                "consumed": 4,
                "column": {
                    "name": f"col{ordinal}",
                    "type_name": "INT",
                    "length": 4,
                    "source": "inferred_fixed_int",
                    "confidence": "medium",
                    "sample_min": min(values),
                    "sample_max": max(values),
                },
            }
    for length in (2, 1):
        if _fixed_slice_available(payloads, offset, length):
            chunks = [payload[offset : offset + length] for payload in payloads]
            if all(_looks_like_fixed_char(raw) for raw in chunks):
                return {
                    "consumed": length,
                    "column": {
                        "name": f"col{ordinal}",
                        "type_name": "CHAR",
                        "length": length,
                        "source": "inferred_fixed_char",
                        "confidence": "low",
                    },
                }
    return None


def _decode_var_prefix_for_inference(data: bytes):
    from .row import decode_observed_var_length

    return decode_observed_var_length(data)


def _create_inferred_table_sql(table_name: str, columns: list[dict[str, object]]) -> str:
    lines = []
    for column in columns:
        type_name = str(column["type_name"])
        length = column.get("length")
        type_sql = f"{type_name}({length})" if length else type_name
        lines.append(f"  {column['name']} {type_sql}")
    return f"CREATE TABLE {table_name} (\n" + ",\n".join(lines) + "\n);"


def _write_raw_orphan_rows(
    *,
    dict_dir: Path,
    candidate: dict[str, object],
    table_name: str,
    output: Path,
) -> int:
    group_id = int(candidate["group_id"])
    file_no = int(candidate["file_no"])
    storage_id = int(candidate["storage_id"])
    output.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, delimiter="|", lineterminator="\n")
        file.write(
            f"CREATE TABLE {table_name} (\n"
            "  raw_row VARBINARY\n"
            ");\n"
            "-- DATA\n"
        )
        writer.writerow(["raw_row"])
        for data_file in _data_file_meta_for_group(dict_dir=dict_dir, group_id=group_id):
            if data_file.file_no != file_no or not data_file.path.exists():
                continue
            pages_total = data_file.path.stat().st_size // data_file.page_size
            with data_file.path.open("rb") as raw_file:
                with mmap.mmap(raw_file.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    for page_no in range(pages_total):
                        page_start = page_no * data_file.page_size
                        if int.from_bytes(mm[page_start + 0x14 : page_start + 0x18], "little") != 0x14:
                            continue
                        page_storage_id = int.from_bytes(mm[page_start + 0x3A : page_start + 0x3E], "little")
                        if page_storage_id != storage_id:
                            continue
                        page = mm[page_start : page_start + data_file.page_size]
                        observed_rows = iter_observed_rows_by_slots(page) or [
                            row for row in scan_observed_row_chain(page) if not row.is_deleted
                        ]
                        for row in observed_rows:
                            if row.is_deleted:
                                continue
                            writer.writerow([row.data.hex()])
                            rows_written += 1
    return rows_written


def _fixed_slice_available(payloads: list[bytes], offset: int, length: int) -> bool:
    return all(offset + length <= len(payload) for payload in payloads)


def _all_zero(value: bytes) -> bool:
    return all(byte == 0 for byte in value)


def _looks_like_text(raw: bytes) -> bool:
    if not raw:
        return True
    printable = sum(1 for byte in raw if byte in (9, 10, 13) or 32 <= byte < 127)
    return printable == len(raw)


def _looks_like_fixed_char(raw: bytes) -> bool:
    return bool(raw) and _looks_like_text(raw) and any(byte != 0x20 for byte in raw)


def _looks_like_number_payload(raw: bytes) -> bool:
    if raw == b"\x80":
        return True
    if not raw:
        return False
    if raw[0] >= 0x80:
        return all(1 <= byte <= 100 for byte in raw[1:])
    payload = raw[1:-1] if raw.endswith(b"\x66") else raw[1:]
    return all(1 <= byte <= 101 for byte in payload)


def _looks_like_date(raw: bytes) -> bool:
    if len(raw) != 3:
        return False
    value = int.from_bytes(raw, "little")
    year = value & 0x7FFF
    month = (value >> 15) & 0x0F
    day = (value >> 19) & 0x1F
    return 1900 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31


def _looks_like_timestamp(raw: bytes) -> bool:
    if len(raw) != 8 or not _looks_like_date(raw[:3]):
        return False
    value = int.from_bytes(raw[3:8], "little")
    hour = value & 0x1F
    minute = (value >> 5) & 0x3F
    second = (value >> 11) & 0x3F
    microsecond = (value >> 17) & 0xFFFFF
    return hour <= 23 and minute <= 59 and second <= 59 and microsecond <= 999999


def _sample_rows_for_storage_id(
    *,
    dict_dir: Path,
    group_id: int,
    file_no: int,
    storage_id: int,
    sample_rows: int,
) -> list[object]:
    rows: list[object] = []
    for data_file in _data_file_meta_for_group(dict_dir=dict_dir, group_id=group_id):
        if data_file.file_no != file_no:
            continue
        path = data_file.path
        if not path.exists():
            continue
        pages_total = path.stat().st_size // data_file.page_size
        with path.open("rb") as file:
            with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for page_no in range(pages_total):
                    page_start = page_no * data_file.page_size
                    if int.from_bytes(mm[page_start + 0x14 : page_start + 0x18], "little") != 0x14:
                        continue
                    page_storage_id = int.from_bytes(mm[page_start + 0x3A : page_start + 0x3E], "little")
                    if page_storage_id != storage_id:
                        continue
                    page = mm[page_start : page_start + data_file.page_size]
                    observed_rows = iter_observed_rows_by_slots(page) or [
                        row for row in scan_observed_row_chain(page) if not row.is_deleted
                    ]
                    for row in observed_rows:
                        rows.append(row)
                        if len(rows) >= sample_rows:
                            return rows
    return rows


def _data_file_meta_for_group(
    *,
    dict_dir: Path,
    group_id: int,
) -> list[DataFileMeta]:
    result: list[DataFileMeta] = []
    file_dict = dict_dir / "file.dict"
    if not file_dict.exists():
        return result
    with file_dict.open("r", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if int(row.get("group_id") or 0) != group_id:
                continue
            result.append(
                DataFileMeta(
                    group_id=group_id,
                    file_no=int(row.get("file_no") or 0),
                    path=Path(row["path"]),
                    page_size=int(row.get("page_size") or 8192),
                )
            )
    return result


def _tablespace_filters(values: list[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        for item in value.split(","):
            normalized = _normalize_tablespace_name(item)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _file_row_tablespace_name(row: dict[str, str]) -> str:
    for key in ("tablespace_name", "tablespace", "group_name"):
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_tablespace_name(value: str) -> str:
    return value.strip().strip('"').casefold()


def _ascii_hint(raw: bytes) -> str:
    return "".join(chr(byte) if 32 <= byte < 127 else "." for byte in raw)



def _normalize_identifier(value: str) -> str:
    return value.strip().strip('"').casefold()



def _safe_output_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-", "."} else "_" for char in value)


def _output_extension(output_format: str) -> str:
    return "row" if output_format == "row" else "dul"


def _partition_names(values: list[str] | None) -> tuple[str, ...]:
    names: list[str] = []
    for value in values or ():
        for item in value.split(","):
            stripped = item.strip()
            if stripped:
                names.append(stripped)
    return tuple(names)


def _cmd_import_row(args: argparse.Namespace) -> int:
    report = import_data_to_sql(
        input_path=Path(args.input),
        output_sql=Path(args.output_sql),
        input_format=args.input_format,
        table_name=args.table,
        include_create_table=not args.no_create_table,
        delimiter=args.delimiter,
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(f"input={report.input}")
        print(f"output_sql={report.output_sql}")
        print(f"table={report.table}")
        print(f"rows={report.rows}")
    return 0


def _cmd_extract_csv(args: argparse.Namespace) -> int:
    if (
        args.metadata_json is None
        and args.segment_json is None
        and args.database_dir is None
    ):
        print(
            "dmdul: extract-csv requires --metadata-json, --segment-json, or --database-dir",
            file=sys.stderr,
        )
        return 2
    if args.metadata_json is not None:
        metadata = CalibratedMetadata.from_json_file(Path(args.metadata_json))
        page_plan_fallback_level = None
        initial_diagnostics: tuple[dict[str, object], ...] = ()
    elif args.segment_json is not None:
        segment_manifest = _read_json_file(Path(args.segment_json))
        metadata = CalibratedMetadata.from_segment_manifest(segment_manifest)
        page_plan_fallback_level = "error" if args.strict_page_plan else "warning"
        initial_diagnostics = _manifest_diagnostics(segment_manifest)
    else:
        page_plan_fallback_level = None
        initial_diagnostics = ()
        if not args.skip_preflight:
            summary = summarize_database_dir(
                database_dir=Path(args.database_dir),
                page_size=args.page_size,
                catalog_pages=args.preflight_catalog_pages,
                sample_limit=args.preflight_sample_limit,
            )
            preflight = evaluate_database_summary_preflight(summary)
            _write_preflight_output(args.preflight_output, summary, preflight)
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
        initial_diagnostics = _manifest_diagnostics(resolved.as_manifest())
    report = extract_csv_with_calibrated_metadata(
        metadata=metadata,
        table_name=args.table,
        output=Path(args.output),
        page_plan_fallback_level=page_plan_fallback_level,
        initial_diagnostics=initial_diagnostics,
        lob_mode=args.lob_mode,
        lob_dir=Path(args.lob_dir) if args.lob_dir else None,
        lob_hash=args.lob_hash,
        output_format=args.output_format,
    )
    if args.report_output:
        Path(args.report_output).write_text(
            json.dumps(report.as_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
    print(f"table={report.table}")
    print(f"output={report.output}")
    print(f"rows_written={report.rows_written}")
    print(f"rows_skipped_deleted={report.rows_skipped_deleted}")
    print(f"rows_skipped_decode_error={report.rows_skipped_decode_error}")
    print(f"ok={str(report.ok).lower()}")
    print(f"strict_ok={str(report.strict_ok).lower()}")
    for diagnostic in report.diagnostics:
        print(
            f"diagnostic={diagnostic['code']} level={diagnostic['level']}",
            file=sys.stderr,
        )
    for error in report.decode_errors:
        print(f"decode_error={error}", file=sys.stderr)
    if args.strict and not report.strict_ok:
        for diagnostic in report.strict_failures:
            print(
                f"strict_failure={diagnostic['code']} level={diagnostic['level']}",
                file=sys.stderr,
            )
    print(f"mode={report.mode}")
    if args.strict:
        return 0 if report.strict_ok else 1
    return 0 if report.ok else 1


def _read_json_file(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _load_analysis_columns(args: argparse.Namespace):
    columns = []
    if args.column:
        columns.extend(parse_column_specs(tuple(args.column)))
    if args.columns_jsonl:
        columns.extend(load_column_meta_from_jsonl(Path(args.columns_jsonl)))
    return tuple(columns)


def _manifest_diagnostics(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    diagnostics = payload.get("diagnostics", ())
    if isinstance(diagnostics, list):
        result.extend(item for item in diagnostics if isinstance(item, dict))
    segment_root = payload.get("segment_root")
    if isinstance(segment_root, dict):
        segment_diagnostics = segment_root.get("diagnostics", ())
        if isinstance(segment_diagnostics, list):
            for item in segment_diagnostics:
                if not isinstance(item, dict):
                    continue
                code = item.get("code")
                if any(existing.get("code") == code for existing in result):
                    continue
                result.append(item)
    return tuple(result)


def _write_preflight_output(
    output: str | None,
    summary: dict[str, object],
    preflight: dict[str, object],
) -> None:
    if output is None:
        return
    payload = json.dumps(
        {
            "summary": summary,
            "preflight": preflight,
        },
        indent=2,
    )
    Path(output).write_text(payload + "\n", encoding="utf-8")


def _cmd_resolve_table(args: argparse.Namespace) -> int:
    resolved = resolve_offline_table_metadata(
        database_dir=Path(args.database_dir),
        table_name=args.table,
        page_size=args.page_size,
        owner=args.owner,
        scan_pages=args.scan_pages,
    )
    manifest = resolved.as_manifest()
    if args.output:
        Path(args.output).write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(manifest, indent=2))
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
                        "page_type_raw": item.page_type_raw,
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
            f"page_type=0x{item.page_type_raw:x} kind=0x{item.page_kind_raw:x} "
            f"path={item.path}{marker}"
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


def _cmd_dump_sysobjects(args: argparse.Namespace) -> int:
    rows = dump_sysobject_rows(
        Path(args.system_file),
        page_size=args.page_size,
    )
    limited_rows = rows[: args.limit] if args.limit else rows
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": item.name,
                        "object_id": item.object_id,
                        "schema_id": item.schema_id,
                        "parent_id": item.parent_id,
                        "type_name": item.type_name,
                        "subtype_name": item.subtype_name,
                        "offset": item.offset,
                        "page_no": item.page_no,
                        "page_offset": item.page_offset,
                        "score": item.score,
                        "source": item.source,
                    }
                    for item in limited_rows
                ],
                indent=2,
            )
        )
        return 0
    for item in limited_rows:
        object_id = "-" if item.object_id is None else str(item.object_id)
        schema_id = "-" if item.schema_id is None else str(item.schema_id)
        parent_id = "-" if item.parent_id is None else str(item.parent_id)
        print(
            f"score={item.score} type={item.type_name}/{item.subtype_name} "
            f"name={item.name} object_id={object_id} schema_id={schema_id} "
            f"parent_id={parent_id} offset={item.offset} "
            f"page={item.page_no} page_offset={item.page_offset}"
        )
    return 0 if rows else 1


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
    parser.add_argument(
        "--init-file",
        help="read DUL-style default parameters from this init.dul file",
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

    analyze_block = subparsers.add_parser(
        "analyze-block",
        help="analyze one data block with page header, object id, row, and field traces",
    )
    analyze_block.add_argument("file")
    analyze_block.add_argument("page_no", type=int)
    analyze_block.add_argument("--object-id", type=int)
    analyze_block.add_argument(
        "--column",
        action="append",
        help="column spec NAME:TYPE[:LENGTH], repeatable",
    )
    analyze_block.add_argument(
        "--columns-jsonl",
        help="read column rows from col.dict CSV or legacy JSONL",
    )
    analyze_block.add_argument(
        "--row-start-offset",
        type=lambda value: int(value, 0),
        default=0x62,
    )
    analyze_block.add_argument("--max-rows", type=int, default=128)
    analyze_block.add_argument("--candidate-scan-bytes", type=int, default=512)
    analyze_block.set_defaults(func=_cmd_analyze_block)

    dump_unknown = subparsers.add_parser(
        "dump-unknown-structures",
        help="dump anonymous page regions as candidate fixed-size structures",
    )
    dump_unknown.add_argument("file")
    dump_unknown.add_argument(
        "--pages",
        required=True,
        help="comma-separated page numbers and inclusive ranges, e.g. 208,224,288",
    )
    dump_unknown.add_argument(
        "--row-start-offset",
        type=lambda value: int(value, 0),
        default=0x62,
    )
    dump_unknown.add_argument("--max-rows", type=int, default=128)
    dump_unknown.add_argument("--tail-scan-bytes", type=int, default=512)
    dump_unknown.add_argument(
        "--chunk-size",
        action="append",
        type=int,
        default=[8, 16, 24],
        help="fixed-size chunk to emit; repeatable, default: 8,16,24",
    )
    dump_unknown.add_argument("--output")
    dump_unknown.set_defaults(func=_cmd_dump_unknown_structures)

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

    write_control = subparsers.add_parser(
        "write-control-ctl",
        help="write extraction-time control.ctl as tablespace_id,file_id,path rows",
    )
    write_control.add_argument("database_dir", nargs="?")
    write_control.add_argument("--control-file")
    write_control.add_argument("--dirlist")
    write_control.add_argument("--output", required=True)
    write_control.add_argument("--sample-limit", type=int, default=1000000)
    write_control.add_argument("--json", action="store_true")
    write_control.set_defaults(func=_cmd_write_control_ctl)

    prepare = subparsers.add_parser(
        "prepare",
        help="create init.dul and filelist.dul from control files or DBF page headers",
    )
    prepare.add_argument("--database-dir")
    prepare.add_argument("--control-file")
    prepare.add_argument("--dirlist")
    prepare.add_argument("--init-output", default="init.dul")
    prepare.add_argument("--filelist-output", default="filelist.dul")
    prepare.add_argument("--output-dir", default="dulout")
    prepare.add_argument("--dict-dir")
    prepare.add_argument("--parallel", type=int, default=1)
    prepare.add_argument("--delimiter", default="|")
    prepare.add_argument("--sample-limit", type=int, default=1000000)
    prepare.add_argument("--json", action="store_true")
    prepare.set_defaults(func=_cmd_prepare)

    bootstrap_dicts = subparsers.add_parser(
        "bootstrap-dicts",
        aliases=["bootstrap"],
        help="build bootstrap dictionary artifact files from an offline database copy",
    )
    bootstrap_dicts.add_argument("database_dir", nargs="?")
    bootstrap_dicts.add_argument("--output-dir")
    bootstrap_dicts.add_argument(
        "--catalog-pages",
        type=int,
        default=0,
        help="pages to sample per file while building the bootstrap summary",
    )
    bootstrap_dicts.add_argument("--sample-limit", type=int, default=8)
    bootstrap_dicts.add_argument(
        "--table",
        action="append",
        help="target table to resolve into user.dict/tab.dict/col.dict; repeatable",
    )
    bootstrap_dicts.add_argument("--owner")
    bootstrap_dicts.add_argument(
        "--source-dict-dir",
        help="filter requested table dictionaries from an existing dict directory instead of rescanning SYSTEM.DBF",
    )
    bootstrap_dicts.add_argument("--scan-pages", type=int, default=64)
    bootstrap_dicts.add_argument(
        "-b",
        "--download-dictionaries",
        action="store_true",
        help="preprocess SYSTEM.DBF and download SYS dictionary rows into user/tab/col dict artifacts",
    )
    bootstrap_dicts.add_argument(
        "--experimental-heuristic-dicts",
        action="store_true",
        help="write target-table heuristic user/tab/col dict rows; research only",
    )
    bootstrap_dicts.add_argument("--json", action="store_true")
    bootstrap_dicts.set_defaults(func=_cmd_bootstrap_dicts)

    dump_data = subparsers.add_parser(
        "dump-data",
        help="dump one table or one user's tables using bootstrap dictionaries",
    )
    dump_data.add_argument("--dict-dir")
    dump_data.add_argument("--output-dir")
    dump_data.add_argument("--table", action="append")
    dump_data.add_argument(
        "--partition",
        action="append",
        help="leaf partition/subpartition name to export; repeatable or comma-separated",
    )
    dump_data.add_argument("--user")
    dump_data.add_argument("--parallel", type=int)
    dump_data.add_argument(
        "--partition-parallel",
        type=int,
        default=1,
        help="workers inside one table; when >1 writes split part files under a parts subdirectory",
    )
    dump_data.add_argument(
        "--orphan-scan-storage-id",
        type=int,
        help="recovery mode: scan same-group data files for BTREE data pages with this old/orphan storage id, for truncate/drop recovery only",
    )
    dump_data.add_argument(
        "--truncate",
        action="store_true",
        help="truncate recovery mode: automatically scan old/orphan storage ids from tab.dict storage_index_id/storage_index_ids",
    )
    dump_data.add_argument("--delimiter", choices=["|", "~"])
    dump_data.add_argument(
        "--output-format",
        choices=["dul", "row"],
        default="dul",
        help="output DUL text or binary row archive with embedded row bytes, default: dul",
    )
    dump_data.add_argument("--report-output")
    dump_data.add_argument("--strict-page-plan", action="store_true")
    dump_data.add_argument(
        "--lob-mode",
        choices=["inline", "external"],
        default="external",
        help="write LOB values inline or as external attachment files, default: external",
    )
    dump_data.add_argument("--lob-hash", choices=["sha256"], default="sha256")
    dump_data.add_argument("--json", action="store_true")
    dump_data.set_defaults(func=_cmd_dump_data)

    scan_orphan = subparsers.add_parser(
        "scan-orphan-storages",
        help="scan data files for BTREE storage ids not referenced by current tab.dict",
    )
    scan_orphan.add_argument("--dict-dir", required=True)
    scan_orphan.add_argument("--group-id", type=int)
    scan_orphan.add_argument(
        "--tablespace",
        action="append",
        help="tablespace name to scan; repeatable or comma-separated. Defaults to all data files when omitted",
    )
    scan_orphan.add_argument("--min-pages", type=int, default=1)
    scan_orphan.add_argument("--sample-rows", type=int, default=3)
    scan_orphan.add_argument("--json", action="store_true")
    scan_orphan.set_defaults(func=_cmd_scan_orphan_storages)

    recover_orphan = subparsers.add_parser(
        "recover-orphan-table",
        help="recover a dropped table from orphan storage pages using supplied columns or inferred raw DDL",
    )
    recover_orphan.add_argument("--dict-dir", required=True)
    recover_orphan.add_argument("--output-dir", required=True)
    recover_orphan.add_argument("--storage-id", type=int)
    recover_orphan.add_argument("--group-id", type=int)
    recover_orphan.add_argument(
        "--tablespace",
        action="append",
        help="tablespace name to scan; repeatable or comma-separated. Defaults to all data files when omitted",
    )
    recover_orphan.add_argument("--min-pages", type=int, default=1)
    recover_orphan.add_argument("--sample-rows", type=int, default=64)
    recover_orphan.add_argument("--max-candidates", type=int, default=10)
    recover_orphan.add_argument("--table-name")
    recover_orphan.add_argument(
        "--column",
        action="append",
        help="column spec NAME:TYPE[:LENGTH], repeatable; when provided, candidates are scored by row decode success and the best match is exported",
    )
    recover_orphan.add_argument(
        "--columns-jsonl",
        help="read supplied columns from col.dict CSV or legacy JSONL",
    )
    recover_orphan.add_argument("--delimiter", choices=["|", "~"], default="|")
    recover_orphan.add_argument(
        "--output-format",
        choices=["dul", "row"],
        default="dul",
        help="output DUL text or binary row archive when columns are supplied, default: dul",
    )
    recover_orphan.add_argument(
        "--lob-mode",
        choices=["inline", "external"],
        default="external",
    )
    recover_orphan.add_argument("--lob-hash", choices=["sha256"], default="sha256")
    recover_orphan.add_argument("--report-output")
    recover_orphan.add_argument("--json", action="store_true")
    recover_orphan.set_defaults(func=_cmd_recover_orphan_table)

    extract_dicts = subparsers.add_parser(
        "extract-dicts",
        help="extract tables using bootstrap CSV dictionary files",
    )
    extract_dicts.add_argument("--dict-dir", required=True)
    extract_dicts.add_argument("--output-dir", required=True)
    extract_dicts.add_argument("--table", action="append")
    extract_dicts.add_argument("--workers", type=int, default=1)
    extract_dicts.add_argument("--report-output")
    extract_dicts.add_argument("--strict-page-plan", action="store_true")
    extract_dicts.add_argument(
        "--lob-mode",
        choices=["inline", "external"],
        default="external",
        help="write LOB values inline or as external attachment files, default: external",
    )
    extract_dicts.add_argument("--lob-hash", choices=["sha256"], default="sha256")
    extract_dicts.add_argument("--json", action="store_true")
    extract_dicts.set_defaults(func=_cmd_extract_dicts)

    extract_csv = subparsers.add_parser(
        "extract-csv",
        help="extract a table to CSV",
    )
    extract_csv.add_argument("--metadata-json")
    extract_csv.add_argument(
        "--segment-json",
        help="read resolved table dictionary and segment metadata JSON",
    )
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
        "--preflight-output",
        help="write extract-csv preflight summary and decision JSON",
    )
    extract_csv.add_argument(
        "--report-output",
        help="write extract-csv report JSON after row scanning",
    )
    extract_csv.add_argument(
        "--output-format",
        choices=["dul", "row"],
        default="dul",
        help="output CSV/DUL text or binary row archive with embedded row bytes, default: dul",
    )
    extract_csv.add_argument(
        "--lob-mode",
        choices=["inline", "external"],
        default="external",
        help="write LOB values inline or as external attachment files, default: external",
    )
    extract_csv.add_argument(
        "--lob-dir",
        help="directory for extract-csv LOB attachment files, default: output path with .lob suffix",
    )
    extract_csv.add_argument("--lob-hash", choices=["sha256"], default="sha256")
    extract_csv.add_argument(
        "--strict-page-plan",
        action="store_true",
        help="fail if a segment manifest cannot provide a page-reference traversal plan",
    )
    extract_csv.add_argument(
        "--strict",
        action="store_true",
        help="fail on any decode error or known page/dictionary uncertainty in the extraction report",
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

    def add_import_data_parser(name: str, help_text: str) -> None:
        import_data = subparsers.add_parser(
            name,
            help=help_text,
        )
        import_data.add_argument("--input", required=True)
        import_data.add_argument("--output-sql", required=True)
        import_data.add_argument(
            "--input-format",
            choices=["auto", "dul", "row", "parts"],
            default="auto",
            help="input format, default: auto",
        )
        import_data.add_argument(
            "--delimiter",
            choices=[",", "|", "~"],
            help="DUL text delimiter; default detects from the data header",
        )
        import_data.add_argument(
            "--table",
            help="target table name for INSERT statements; default uses the archived table name",
        )
        import_data.add_argument(
            "--no-create-table",
            action="store_true",
            help="do not emit the archived CREATE TABLE statement",
        )
        import_data.add_argument("--json", action="store_true")
        import_data.set_defaults(func=_cmd_import_row)

    add_import_data_parser(
        "import-data",
        "generate SQL from DUL text or binary dmdul row archive",
    )
    add_import_data_parser(
        "import-row",
        "compatibility alias for import-data",
    )

    resolve_table = subparsers.add_parser(
        "resolve-table",
        help="resolve offline table metadata from a database directory",
    )
    resolve_table.add_argument("database_dir")
    resolve_table.add_argument("--table", required=True)
    resolve_table.add_argument("--owner")
    resolve_table.add_argument("--scan-pages", type=int, default=64)
    resolve_table.add_argument("--json", action="store_true")
    resolve_table.add_argument(
        "--output",
        help="write resolved dictionary and segment metadata JSON",
    )
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

    dump_sysobjects = subparsers.add_parser(
        "dump-sysobjects",
        help="heuristically dump SYSOBJECTS table and storage child rows from SYSTEM.DBF",
    )
    dump_sysobjects.add_argument("system_file")
    dump_sysobjects.add_argument("--json", action="store_true")
    dump_sysobjects.add_argument("--limit", type=int, default=100)
    dump_sysobjects.set_defaults(func=_cmd_dump_sysobjects)

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
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    _apply_init_config(args, raw_argv)
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
