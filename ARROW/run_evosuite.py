#!/usr/bin/env python3
"""Chạy EvoSuite baseline trên đúng repository/sample trong manifest ARROW.

Thiết lập công cụ:
  python3 run_evosuite.py --download-tools --setup-only

Chạy thử một sample:
  python3 run_evosuite.py --limit 1 --workers 1 --search-budget 60

Chạy đủ manifest (MacBook 48 GB nên bắt đầu với 2 workers):
  python3 run_evosuite.py --workers 2 --search-budget 120 --seeds 42
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
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

from src.evosuite_runner import (
    default_tools,
    ensure_tools,
    experiment_key,
    load_jsonl,
    read_manifest,
    run_sample,
    sha256_file,
    summarize,
)


ROOT = Path(__file__).resolve().parent


def parse_seeds(value: str) -> list[int]:
    try:
        seeds = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--seeds phải là số nguyên, ví dụ 42 hoặc 42,43,44") from exc
    if not seeds or len(seeds) != len(set(seeds)):
        raise argparse.ArgumentTypeError("--seeds phải chứa ít nhất một seed và không được lặp")
    return seeds


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chạy EvoSuite công bằng trên manifest ARROW và xuất JSONL/CSV.")
    parser.add_argument("--manifest", type=Path, default=ROOT / "shards" / "repo_shard_05_manifest.csv")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "pipeline.yaml")
    parser.add_argument("--run-id", default="", help="Mặc định evosuite-<UTC>.")
    parser.add_argument("--output-dir", type=Path, help="Mặc định runs/evosuite/<run-id>.")
    parser.add_argument("--start-rank", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="0 = toàn bộ manifest.")
    parser.add_argument("--workers", type=int, default=2, help="Số repository chạy song song; Mac 48 GB nên bắt đầu bằng 2.")
    parser.add_argument("--seeds", type=parse_seeds, default=[42], help="Một hoặc nhiều seed, ví dụ 42 hoặc 42,43,44.")
    parser.add_argument("--search-budget", type=int, default=120, help="Ngân sách tìm kiếm EvoSuite cho mỗi class/seed (giây).")
    parser.add_argument("--generation-timeout", type=int, default=0, help="0 = search-budget + 180 giây.")
    parser.add_argument("--build-timeout", type=int, default=900)
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--memory-mb", type=int, default=2048, help="Heap tối đa cho mỗi EvoSuite worker/JUnit process.")
    parser.add_argument("--criterion", default="BRANCH", help="Tiêu chí search của EvoSuite; mặc định BRANCH.")
    parser.add_argument("--java-home", default="", help="Ghi đè auto-detect JDK cho mọi sample (thường không nên dùng).")
    parser.add_argument("--tools-dir", type=Path, default=ROOT / "tools" / "evosuite")
    parser.add_argument("--download-tools", action="store_true", help="Tải EvoSuite 1.2.0 runtime và JUnit nếu còn thiếu.")
    parser.add_argument("--setup-only", action="store_true", help="Chỉ tải/kiểm tra tool rồi dừng.")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ khóa/kiểm tra manifest; không clone, build hay chạy EvoSuite.")
    parser.add_argument("--no-resume", action="store_true", help="Không bỏ qua record đã có trong JSONL cùng run-id.")
    parser.add_argument(
        "--rerun-status",
        action="append",
        default=[],
        help="Chạy lại record có status này và thay record cũ; có thể lặp lại, ví dụ TOOL_ERROR và CLASSPATH_FAILED.",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--keep-repo-cache", action="store_true", help="Nên bật khi chạy thử/rerun để tránh clone lại.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in ("workers", "search_budget", "build_timeout", "test_timeout", "memory_mb"):
        if getattr(args, name) < 1:
            raise ValueError(f"--{name.replace('_', '-')} phải >= 1")
    if args.generation_timeout < 0:
        raise ValueError("--generation-timeout phải >= 0")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    validate_args(args)
    if yaml is None:
        raise RuntimeError("Thiếu PyYAML. Chạy: python3 -m pip install -r requirements.txt")
    tools = default_tools(args.tools_dir.resolve())
    if args.setup_only:
        tool_provenance = ensure_tools(tools, args.download_tools)
        print(json.dumps(tool_provenance, ensure_ascii=False, indent=2))
        return 0

    if args.dry_run:
        tool_provenance = {
            name: {"path": str(path.resolve()), "present": path.is_file()}
            for name, path in zip(("evosuite", "runtime", "junit", "hamcrest"), tools.paths())
        }
    else:
        tool_provenance = ensure_tools(tools, args.download_tools)

    manifest = args.manifest.resolve()
    config_path = args.config.resolve()
    manifest_rows = read_manifest(manifest, args.start_rank, args.limit)
    if not manifest_rows:
        raise ValueError("Không có sample nào được chọn từ manifest")
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    run_id = args.run_id or f"evosuite-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = (args.output_dir or ROOT / "runs" / "evosuite" / run_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    locked_manifest = output_dir / "manifest_locked.csv"
    shutil.copy2(manifest, locked_manifest)
    provenance = {
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest),
        "manifest_sha256": sha256_file(manifest),
        "config": str(config_path),
        "config_sha256": sha256_file(config_path),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "runner_library_sha256": sha256_file(ROOT / "src" / "evosuite_runner.py"),
        "selected_samples": len(manifest_rows),
        "unique_repositories": len({row["project_id"] for row in manifest_rows}),
        "seeds": args.seeds,
        "search_budget_seconds": args.search_budget,
        "criterion": args.criterion,
        "workers": min(args.workers, len(manifest_rows)),
        "memory_mb_per_process": args.memory_mb,
        "generation_timeout_seconds": args.generation_timeout or args.search_budget + 180,
        "build_timeout_seconds": args.build_timeout,
        "test_timeout_seconds": args.test_timeout,
        "tools": tool_provenance,
        "valid_definition": "baseline module passes AND generated EvoSuite sources compile AND generated JUnit tests pass",
        "coverage_warning": "EvoSuite statistics coverage is search coverage, not JaCoCo/PIT.",
    }
    (output_dir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"EvoSuite run_id={run_id}; repositories={len(manifest_rows)}; seeds={args.seeds}; workers={provenance['workers']}")
    print(f"Manifest lock: {locked_manifest}")
    if args.dry_run:
        for index, row in enumerate(manifest_rows, 1):
            print(f"[{index}/{len(manifest_rows)}] {row['project_id']}/{row['sample_file']}")
        print(f"Dry-run hoàn tất; chưa clone/build/chạy EvoSuite. Provenance: {output_dir / 'provenance.json'}")
        return 0

    jsonl_path = output_dir / "evosuite_records.jsonl"
    if args.no_resume:
        jsonl_path.unlink(missing_ok=True)
    prior = [] if args.no_resume else load_jsonl(jsonl_path)
    rerun_statuses = {status.strip().upper() for item in args.rerun_status for status in item.split(",") if status.strip()}
    rerun_keys = {
        experiment_key(row)
        for row in prior
        if str(row.get("status", "")).upper() in rerun_statuses
    }
    if rerun_keys:
        print(
            f"[rerun] sẽ chạy lại {len(rerun_keys)} record có status={sorted(rerun_statuses)}; "
            "record cũ chỉ được thay sau khi record mới hoàn tất"
        )
    completed_keys = {experiment_key(row) for row in prior} - rerun_keys
    pending: list[dict[str, str]] = []
    for row in manifest_rows:
        expected = {(row["project_id"], row["sample_file"], seed) for seed in args.seeds}
        if expected.issubset(completed_keys):
            print(f"[resume] bỏ qua {row['project_id']}/{row['sample_file']}")
        else:
            pending.append(row)

    write_lock = threading.Lock()
    records = list(prior)
    dataset_dir = ROOT.parent / "classes2test" / "dataset"
    generation_timeout = args.generation_timeout or args.search_budget + 180

    def worker(row: dict[str, str]) -> list[dict[str, Any]]:
        missing_seeds = [seed for seed in args.seeds if (row["project_id"], row["sample_file"], seed) not in completed_keys]
        print(f"[START] {row['project_id']}/{row['sample_file']} seeds={missing_seeds}", flush=True)
        try:
            result = run_sample(
                root=ROOT,
                dataset_dir=dataset_dir,
                manifest_row=row,
                config=config,
                run_id=run_id,
                seeds=missing_seeds,
                tools=tools,
                output_dir=output_dir,
                search_budget=args.search_budget,
                generation_timeout=generation_timeout,
                build_timeout=args.build_timeout,
                test_timeout=args.test_timeout,
                memory_mb=args.memory_mb,
                criterion=args.criterion,
                manual_java_home=args.java_home,
                keep_workspace=args.keep_workspace,
                keep_repo_cache=args.keep_repo_cache,
            )
        except Exception as exc:
            # A per-repository infrastructure/cleanup failure must still
            # produce auditable records and must never discard other workers'
            # completed results.
            result = [
                {
                    "run_id": run_id,
                    "manifest_rank": row.get("rank", ""),
                    "project_id": row.get("project_id", ""),
                    "sample_file": row.get("sample_file", ""),
                    "seed": seed,
                    "status": "TOOL_ERROR",
                    "failure_stage": "worker",
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                    "baseline_eligible": False,
                    "compilation": False,
                    "execution_success": False,
                    "test_passed": False,
                    "valid": False,
                }
                for seed in missing_seeds
            ]
        print(f"[DONE] {row['project_id']}/{row['sample_file']} -> {', '.join(str(item['status']) for item in result)}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=min(args.workers, max(1, len(pending)))) as executor:
        futures = {executor.submit(worker, row): row for row in pending}
        for future in as_completed(futures):
            batch = future.result()
            with write_lock:
                batch_keys = {experiment_key(record) for record in batch}
                records = [record for record in records if experiment_key(record) not in batch_keys]
                records.extend(batch)
                current = sorted(records, key=lambda item: (int(item.get("manifest_rank") or 0), int(item.get("seed") or 0)))
                write_jsonl(jsonl_path, current)
                write_csv(output_dir / "evosuite_records.csv", current)
                (output_dir / "summary.json").write_text(json.dumps(summarize(current), ensure_ascii=False, indent=2), encoding="utf-8")

    ordered = sorted(records, key=lambda item: (int(item.get("manifest_rank") or 0), int(item.get("seed") or 0)))
    summary = summarize(ordered)
    write_jsonl(jsonl_path, ordered)
    write_csv(output_dir / "evosuite_records.csv", ordered)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Kết quả: {output_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Đã dừng theo yêu cầu; các record hoàn tất vẫn được giữ để --resume.", file=sys.stderr)
        raise SystemExit(130)
