from __future__ import annotations

import argparse
import json
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
    output_dir.mkdir(parents=True, exist_ok=True)
    progress = None if args.json else _dump_data_progress_printer()
    if progress is not None:
        print(
            f"[dump-data] start tables_total={len(tables)} parallel={max(1, workers)} output_dir={output_dir}",
            file=sys.stderr,
            flush=True,
        )
    reports = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                extract_csv_with_calibrated_metadata,
                metadata=metadata,
                table_name=table.qualified_name,
                output=output_dir / f"{_safe_output_name(table.qualified_name)}.dul",
                page_plan_fallback_level="error" if args.strict_page_plan else "warning",
                delimiter=delimiter,
                include_sql_header=True,
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
                        "output": str(output_dir / f"{_safe_output_name(table.qualified_name)}.dul"),
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
        "parallel": max(1, workers),
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



def _normalize_identifier(value: str) -> str:
    return value.strip().strip('"').casefold()



def _safe_output_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-", "."} else "_" for char in value)


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
    for diagnostic in report.diagnostics:
        print(
            f"diagnostic={diagnostic['code']} level={diagnostic['level']}",
            file=sys.stderr,
        )
    for error in report.decode_errors:
        print(f"decode_error={error}", file=sys.stderr)
    print(f"mode={report.mode}")
    if args.strict_page_plan and not report.ok:
        return 1
    return 0


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
    dump_data.add_argument("--user")
    dump_data.add_argument("--parallel", type=int)
    dump_data.add_argument("--delimiter", choices=["|", "~"])
    dump_data.add_argument("--report-output")
    dump_data.add_argument("--strict-page-plan", action="store_true")
    dump_data.add_argument("--json", action="store_true")
    dump_data.set_defaults(func=_cmd_dump_data)

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
        "--strict-page-plan",
        action="store_true",
        help="fail if a segment manifest cannot provide a page-reference traversal plan",
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
