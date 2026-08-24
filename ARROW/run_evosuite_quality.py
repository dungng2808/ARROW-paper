#!/usr/bin/env python3
"""Đo toàn bộ cột Table III cho các EvoSuite test VALID đã sinh sẵn.

Ví dụ:
  python3 run_evosuite_quality.py --download-tools --setup-only
  python3 run_evosuite_quality.py --workers 3
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from src.evosuite_quality import (
    default_quality_tools,
    ensure_quality_tools,
    quality_key,
    run_quality_sample,
    summarize_table_iii,
)
from src.evosuite_runner import default_tools, ensure_tools, load_jsonl, sha256_file


ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dùng JaCoCo/PIT đo IC, LC, BC, MC, MS cho EvoSuite VALID và xuất đúng một dòng Table III."
    )
    parser.add_argument(
        "--evosuite-run",
        type=Path,
        default=ROOT / "runs" / "evosuite" / "evosuite-rq3-seed42",
        help="Thư mục chứa evosuite_records.jsonl của lần chạy EvoSuite.",
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "pipeline.yaml")
    parser.add_argument("--workers", type=int, default=3, help="Số repository đo song song.")
    parser.add_argument("--pit-threads", type=int, default=1, help="Số thread PIT bên trong mỗi worker.")
    parser.add_argument("--memory-mb", type=int, default=2048)
    parser.add_argument("--coverage-timeout", type=int, default=600)
    parser.add_argument("--mutation-timeout", type=int, default=1800)
    parser.add_argument("--limit", type=int, default=0, help="0 = toàn bộ 30 test VALID; dùng 1 để smoke-test.")
    parser.add_argument("--tools-dir", type=Path, default=ROOT / "tools" / "quality")
    parser.add_argument("--evosuite-tools-dir", type=Path, default=ROOT / "tools" / "evosuite")
    parser.add_argument("--download-tools", action="store_true")
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--rerun-status",
        action="append",
        default=[],
        help="Chạy lại COMPLETE/PARTIAL/FAILED; có thể lặp hoặc ngăn cách bằng dấu phẩy.",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument(
        "--remove-repo-cache",
        action="store_true",
        help="Xóa cache repo sau mỗi sample; mặc định giữ cache để resume nhanh.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in ("workers", "pit_threads", "memory_mb", "coverage_timeout", "mutation_timeout"):
        if getattr(args, name) < 1:
            raise ValueError(f"--{name.replace('_', '-')} phải >= 1")
    if args.limit < 0:
        raise ValueError("--limit phải >= 0")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.unlink(missing_ok=True)
        return
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    validate_args(args)
    if yaml is None:
        raise RuntimeError("Thiếu PyYAML. Chạy: python3 -m pip install -r requirements.txt")
    quality_tools = default_quality_tools(args.tools_dir.resolve())
    quality_provenance = ensure_quality_tools(quality_tools, args.download_tools)
    evosuite_tools = default_tools(args.evosuite_tools_dir.resolve())
    evosuite_provenance = ensure_tools(evosuite_tools, False)
    if args.setup_only:
        print(json.dumps({"quality_tools": quality_provenance, "evosuite_tools": evosuite_provenance}, indent=2))
        return 0

    run_dir = args.evosuite_run.resolve()
    source_path = run_dir / "evosuite_records.jsonl"
    source_records = load_jsonl(source_path)
    if not source_records:
        raise FileNotFoundError(f"Không có EvoSuite records: {source_path}")
    valid_sources = [row for row in source_records if row.get("valid") is True]
    valid_sources.sort(key=lambda row: (int(row.get("manifest_rank") or 0), int(row.get("seed") or 0)))
    if not valid_sources:
        raise ValueError("Không có record VALID để đo Table III")
    selected = valid_sources[: args.limit] if args.limit else valid_sources
    with args.config.resolve().open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    dataset_dir = ROOT.parent / "classes2test" / "dataset"
    output_jsonl = run_dir / "table_iii_records.jsonl"
    records = [] if args.no_resume else load_jsonl(output_jsonl)
    rerun_statuses = {
        status.strip().upper()
        for item in args.rerun_status
        for status in item.split(",")
        if status.strip()
    }
    rerun_keys = {
        quality_key(row)
        for row in records
        if str(row.get("quality_status") or "").upper() in rerun_statuses
    }
    completed_keys = {quality_key(row) for row in records} - rerun_keys
    pending = [row for row in selected if quality_key(row) not in completed_keys]
    provenance = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_records": str(source_path),
        "source_records_sha256": sha256_file(source_path),
        "quality_runner_sha256": sha256_file(Path(__file__).resolve()),
        "quality_library_sha256": sha256_file(ROOT / "src" / "evosuite_quality.py"),
        "config": str(args.config.resolve()),
        "config_sha256": sha256_file(args.config.resolve()),
        "valid_source_records": len(valid_sources),
        "selected_this_invocation": len(selected),
        "workers": min(args.workers, max(1, len(pending))),
        "pit_threads_per_worker": args.pit_threads,
        "memory_mb_per_worker": args.memory_mb,
        "coverage_timeout_seconds": args.coverage_timeout,
        "mutation_timeout_seconds": args.mutation_timeout,
        "quality_tools": quality_provenance,
        "evosuite_tools": evosuite_provenance,
        "method": (
            "Only source records with valid=true are measured. Saved EvoSuite Java sources are copied and "
            "recompiled after a runner-only instrumentation adaptation (shared classloader/pre-attached agents); "
            "generated statements and assertions are unchanged. JaCoCo re-executes that test and PIT mutates "
            "only the focal class. No EvoSuite generation is repeated."
        ),
    }
    (run_dir / "table_iii_provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Table III: source={len(source_records)}, VALID={len(valid_sources)}, pending={len(pending)}, "
        f"workers={provenance['workers']}"
    )
    write_lock = threading.Lock()
    project_locks = {str(row.get("project_id") or ""): threading.Lock() for row in selected}

    def persist() -> None:
        ordered = sorted(records, key=lambda row: (int(row.get("manifest_rank") or 0), int(row.get("seed") or 0)))
        write_jsonl(output_jsonl, ordered)
        write_csv(run_dir / "table_iii_records.csv", ordered)
        summary = summarize_table_iii(source_records, ordered)
        (run_dir / "table_iii_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_csv(run_dir / "table_iii.csv", [summary])

    def worker(source: dict[str, Any]) -> dict[str, Any]:
        label = f"{source.get('manifest_rank')}:{source.get('project_id')}"
        print(f"[START] {label}", flush=True)
        with project_locks[str(source.get("project_id") or "")]:
            result = run_quality_sample(
                root=ROOT,
                dataset_dir=dataset_dir,
                source_record=source,
                config=config,
                evosuite_tools=evosuite_tools,
                quality_tools=quality_tools,
                coverage_timeout=args.coverage_timeout,
                mutation_timeout=args.mutation_timeout,
                memory_mb=args.memory_mb,
                pit_threads=args.pit_threads,
                keep_workspace=args.keep_workspace,
                keep_repo_cache=not args.remove_repo_cache,
            )
        print(f"[DONE] {label} -> {result['quality_status']}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=min(args.workers, max(1, len(pending)))) as executor:
        futures = [executor.submit(worker, source) for source in pending]
        for future in as_completed(futures):
            result = future.result()
            with write_lock:
                key = quality_key(result)
                records = [row for row in records if quality_key(row) != key]
                records.append(result)
                persist()
    persist()
    summary = summarize_table_iii(source_records, records)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Dòng điền Table III: {run_dir / 'table_iii.csv'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Đã dừng; các sample hoàn tất đã được lưu để resume.", file=sys.stderr)
        raise SystemExit(130)
