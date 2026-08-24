#!/usr/bin/env python3
"""Per-sample EvoSuite -> JaCoCo/PIT -> tsDetect pipeline with cleanup.

Each worker clones one repository, completes every requested metric for that
sample, persists compact records, and removes the repository cache.  The same
locked 200-row manifest can be split evenly across five machines with
``--shard-count 5 --shard-index 0..4``.
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

from src.evosuite_full import (
    default_smell_java_home,
    ensure_detector,
    not_applicable_quality,
    not_applicable_smell,
    partition_manifest_rows,
    run_full_sample,
    summarize_full_run,
    table_iii_row,
    validate_manifest_coverage,
)
from src.evosuite_quality import default_quality_tools, ensure_quality_tools, quality_key
from src.evosuite_runner import (
    default_tools,
    ensure_tools,
    experiment_key,
    load_jsonl,
    read_manifest,
    sha256_file,
)
from src.repo_manager import safe_remove_tree


ROOT = Path(__file__).resolve().parent


def parse_seeds(value: str) -> list[int]:
    try:
        seeds = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--seeds phải là số nguyên, ví dụ 42 hoặc 42,43") from exc
    if not seeds or len(seeds) != len(set(seeds)):
        raise argparse.ArgumentTypeError("--seeds phải có ít nhất một seed và không được lặp")
    return seeds


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chạy EvoSuite, JaCoCo, PIT và tsDetect liên tiếp cho từng sample rồi xóa repo cache."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "shards" / "clean-samples-seed42" / "final" / "final_manifest_200.csv",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "shards" / "clean-samples-seed42" / "dataset",
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "pipeline.yaml")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--compact-dir",
        type=Path,
        help="Xuất compact records/provenance để chuyển qua Git hoặc sang máy merge.",
    )
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="0 = toàn bộ shard; dùng 1 cho smoke test.")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seeds", type=parse_seeds, default=[42])
    parser.add_argument("--search-budget", type=int, default=120)
    parser.add_argument("--generation-timeout", type=int, default=0)
    parser.add_argument("--build-timeout", type=int, default=900)
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--coverage-timeout", type=int, default=600)
    parser.add_argument("--mutation-timeout", type=int, default=1800)
    parser.add_argument("--smell-timeout", type=int, default=900)
    parser.add_argument("--memory-mb", type=int, default=2048)
    parser.add_argument("--pit-threads", type=int, default=1)
    parser.add_argument("--criterion", default="BRANCH")
    parser.add_argument("--java-home", default="", help="Override JDK cho build/EvoSuite; thường để trống.")
    parser.add_argument("--smell-java-home", default="", help="JDK chạy tsDetect; mặc định JDK 11 local.")
    parser.add_argument("--evosuite-tools-dir", type=Path, default=ROOT / "tools" / "evosuite")
    parser.add_argument("--quality-tools-dir", type=Path, default=ROOT / "tools" / "quality")
    parser.add_argument(
        "--detector-jar",
        type=Path,
        default=ROOT / "tools" / "tsdetect" / "TestSmellDetector.jar",
    )
    parser.add_argument("--download-tools", action="store_true")
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--rerun-status",
        action="append",
        default=[],
        help="Rerun full_status; lặp lại hoặc ngăn cách dấu phẩy, ví dụ PARTIAL,GENERATION_INVALID.",
    )
    parser.add_argument("--keep-workspace", action="store_true", help="Chỉ dùng debug; repo cache vẫn bị xóa.")
    parser.add_argument(
        "--merge-run-dir",
        type=Path,
        action="append",
        default=[],
        help="Merge compact output từ các máy; truyền đúng một lần cho mỗi shard.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in (
        "workers",
        "search_budget",
        "build_timeout",
        "test_timeout",
        "coverage_timeout",
        "mutation_timeout",
        "smell_timeout",
        "memory_mb",
        "pit_threads",
        "shard_count",
    ):
        if getattr(args, name) < 1:
            raise ValueError(f"--{name.replace('_', '-')} phải >= 1")
    if args.generation_timeout < 0 or args.limit < 0:
        raise ValueError("--generation-timeout và --limit phải >= 0")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise ValueError("--shard-index phải nằm trong [0, shard-count)")
    if args.merge_run_dir and (args.setup_only or args.dry_run or args.no_resume or args.limit):
        raise ValueError("Merge không dùng cùng --setup-only/--dry-run/--no-resume/--limit")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    temporary.replace(path)


def _atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        if fields:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                        if isinstance(value, (dict, list))
                        else value
                        for key, value in row.items()
                    }
                )
    temporary.replace(path)


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (int(row.get("manifest_rank") or 0), int(row.get("seed") or 0)))


def _replace_batch(
    existing: list[dict[str, Any]], replacements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    keys = {experiment_key(row) for row in replacements}
    return [row for row in existing if experiment_key(row) not in keys] + replacements


def persist_outputs(
    output_dir: Path,
    generations: list[dict[str, Any]],
    qualities: list[dict[str, Any]],
    smells: list[dict[str, Any]],
    full: list[dict[str, Any]],
    *,
    expected_records: int,
) -> dict[str, Any]:
    generations = _sort_rows(generations)
    qualities = _sort_rows(qualities)
    smells = _sort_rows(smells)
    full = _sort_rows(full)
    _atomic_jsonl(output_dir / "evosuite_records.jsonl", generations)
    _atomic_csv(output_dir / "evosuite_records.csv", generations)
    _atomic_jsonl(output_dir / "quality_records.jsonl", qualities)
    _atomic_csv(output_dir / "quality_records.csv", qualities)
    _atomic_jsonl(output_dir / "smell_records.jsonl", smells)
    _atomic_csv(output_dir / "smell_records.csv", smells)
    _atomic_jsonl(output_dir / "evosuite_full_quality_records.jsonl", full)
    _atomic_csv(output_dir / "evosuite_full_quality_records.csv", full)
    summary = summarize_full_run(
        generations, qualities, smells, full, expected_records=expected_records
    )
    _atomic_json(output_dir / "evosuite_full_quality_summary.json", summary)
    _atomic_csv(output_dir / "evosuite_full_quality_summary.csv", [summary])
    _atomic_json(output_dir / "completeness_report.json", summary)
    _atomic_csv(output_dir / "table_iii_evosuite.csv", [table_iii_row(summary)])
    return summary


def _load_component(run_dir: Path, name: str) -> list[dict[str, Any]]:
    path = run_dir / name
    rows = load_jsonl(path)
    if not rows:
        raise FileNotFoundError(f"Thiếu hoặc rỗng: {path}")
    return rows


COMPACT_FILES = (
    "provenance.json",
    "evosuite_records.jsonl",
    "quality_records.jsonl",
    "smell_records.jsonl",
    "evosuite_full_quality_records.jsonl",
    "evosuite_full_quality_summary.json",
    "table_iii_evosuite.csv",
    "completeness_report.json",
)


def export_compact(run_dir: Path, compact_dir: Path) -> None:
    destination = compact_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for name in COMPACT_FILES:
        source = run_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"Không thể export compact vì thiếu {source}")
        target = destination / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(source, temporary)
        temporary.replace(target)


def merge_runs(args: argparse.Namespace, manifest_rows: list[dict[str, str]], manifest_hash: str) -> int:
    if len(args.merge_run_dir) != args.shard_count:
        raise ValueError(f"Cần đúng {args.shard_count} --merge-run-dir; nhận {len(args.merge_run_dir)}")
    provenances: list[dict[str, Any]] = []
    by_index: dict[int, Path] = {}
    component_names = {
        "generations": "evosuite_records.jsonl",
        "qualities": "quality_records.jsonl",
        "smells": "smell_records.jsonl",
        "full": "evosuite_full_quality_records.jsonl",
    }
    merged: dict[str, list[dict[str, Any]]] = {name: [] for name in component_names}
    seeds: list[int] | None = None
    for raw_dir in args.merge_run_dir:
        run_dir = raw_dir.resolve()
        provenance_path = run_dir / "provenance.json"
        if not provenance_path.is_file():
            raise FileNotFoundError(f"Thiếu provenance: {provenance_path}")
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("manifest_sha256") != manifest_hash:
            raise ValueError(f"Manifest SHA mismatch tại {run_dir}")
        if int(provenance.get("shard_count", -1)) != args.shard_count:
            raise ValueError(f"shard_count mismatch tại {run_dir}")
        index = int(provenance.get("shard_index", -1))
        if index in by_index or index < 0 or index >= args.shard_count:
            raise ValueError(f"shard_index thiếu/trùng/không hợp lệ tại {run_dir}: {index}")
        current_seeds = [int(seed) for seed in provenance.get("seeds", [])]
        if seeds is None:
            seeds = current_seeds
        elif seeds != current_seeds:
            raise ValueError(f"Seed mismatch tại {run_dir}")
        expected_rows = partition_manifest_rows(manifest_rows, args.shard_count, index)
        actual_keys = {
            (str(row.get("project_id")), str(row.get("sample_file")), int(row.get("seed", 0)))
            for row in _load_component(run_dir, component_names["generations"])
        }
        expected_keys = {
            (row["project_id"], row["sample_file"], seed) for row in expected_rows for seed in current_seeds
        }
        if actual_keys != expected_keys:
            missing = len(expected_keys - actual_keys)
            extra = len(actual_keys - expected_keys)
            raise ValueError(f"Shard {index} chưa hoàn chỉnh hoặc sai phân vùng: missing={missing}, extra={extra}")
        by_index[index] = run_dir
        provenances.append(provenance)
        for name, filename in component_names.items():
            merged[name].extend(_load_component(run_dir, filename))
    if set(by_index) != set(range(args.shard_count)):
        raise ValueError(f"Thiếu shard: {sorted(set(range(args.shard_count)) - set(by_index))}")
    assert seeds is not None
    expected_records = len(manifest_rows) * len(seeds)
    for name, rows in merged.items():
        keys = [experiment_key(row) for row in rows]
        if len(keys) != expected_records or len(set(keys)) != expected_records:
            raise ValueError(f"Merge {name} thiếu/trùng key: rows={len(keys)}, unique={len(set(keys))}")
    run_id = args.run_id or "evosuite-clean-200-merged"
    output_dir = (args.output_dir or ROOT / "runs" / "evosuite" / run_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = persist_outputs(output_dir, expected_records=expected_records, **merged)
    _atomic_json(
        output_dir / "provenance.json",
        {
            "run_id": run_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "merge",
            "manifest": str(args.manifest.resolve()),
            "manifest_sha256": manifest_hash,
            "manifest_samples": len(manifest_rows),
            "shard_count": args.shard_count,
            "seeds": seeds,
            "source_run_dirs": [str(by_index[index]) for index in range(args.shard_count)],
            "source_provenance": provenances,
        },
    )
    if args.compact_dir:
        export_compact(output_dir, args.compact_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Merged output: {output_dir}")
    return 0 if summary.get("full_quality_ready") else 2


def _synthetic_failure(
    run_id: str,
    row: dict[str, str],
    seed: int,
    exc: Exception,
    *,
    repo_cache_removed: bool,
    cleanup_warning: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    generation = {
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
        "repo_cache_removed": repo_cache_removed,
    }
    if cleanup_warning:
        generation["cleanup_warning"] = cleanup_warning
    quality = not_applicable_quality(generation)
    smell = not_applicable_smell(generation)
    full = dict(generation)
    full.update({key: value for key, value in quality.items() if key not in generation})
    full.update({key: value for key, value in smell.items() if key not in generation})
    full["full_status"] = "INFRA_ERROR"
    full["full_metrics_complete"] = False
    return generation, quality, smell, full


def main() -> int:
    args = parse_args()
    validate_args(args)
    if yaml is None:
        raise RuntimeError("Thiếu PyYAML. Chạy: python3 -m pip install -r requirements.txt")
    manifest = args.manifest.resolve()
    all_manifest_rows = read_manifest(manifest)
    manifest_hash = sha256_file(manifest)
    if args.merge_run_dir:
        return merge_runs(args, all_manifest_rows, manifest_hash)

    evosuite_tools = default_tools(args.evosuite_tools_dir.resolve())
    quality_tools = default_quality_tools(args.quality_tools_dir.resolve())
    evosuite_provenance = ensure_tools(evosuite_tools, args.download_tools)
    quality_provenance = ensure_quality_tools(quality_tools, args.download_tools)
    smell_provenance = ensure_detector(args.detector_jar, args.download_tools)
    smell_java_home = args.smell_java_home or default_smell_java_home(ROOT)
    if args.setup_only:
        print(
            json.dumps(
                {
                    "evosuite": evosuite_provenance,
                    "quality": quality_provenance,
                    "tsdetect": smell_provenance,
                    "smell_java_home": smell_java_home,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    shard_rows = partition_manifest_rows(all_manifest_rows, args.shard_count, args.shard_index)
    validate_manifest_coverage(all_manifest_rows, shard_rows, args.shard_count, args.shard_index)
    selected_rows = shard_rows[: args.limit] if args.limit else shard_rows
    if not selected_rows:
        raise ValueError("Shard/limit không chọn được sample nào")
    for row in selected_rows:
        sample_path = args.dataset.resolve() / row["project_id"] / row["sample_file"]
        if not sample_path.is_file():
            raise FileNotFoundError(f"Dataset snapshot thiếu sample: {sample_path}")

    run_id = args.run_id or f"evosuite-full-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = (args.output_dir or ROOT / "runs" / "evosuite" / run_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    generation_timeout = args.generation_timeout or args.search_budget + 180
    expected_records = len(selected_rows) * len(args.seeds)
    provenance = {
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "integrated-per-sample",
        "manifest": str(manifest),
        "manifest_sha256": manifest_hash,
        "manifest_samples": len(all_manifest_rows),
        "dataset": str(args.dataset.resolve()),
        "dataset_snapshot": True,
        "shard_count": args.shard_count,
        "shard_index": args.shard_index,
        "shard_samples": len(shard_rows),
        "selected_this_run": len(selected_rows),
        "seeds": args.seeds,
        "expected_records": expected_records,
        "workers": min(args.workers, len(selected_rows)),
        "search_budget_seconds": args.search_budget,
        "generation_timeout_seconds": generation_timeout,
        "build_timeout_seconds": args.build_timeout,
        "test_timeout_seconds": args.test_timeout,
        "coverage_timeout_seconds": args.coverage_timeout,
        "mutation_timeout_seconds": args.mutation_timeout,
        "smell_timeout_seconds": args.smell_timeout,
        "memory_mb_per_worker": args.memory_mb,
        "pit_threads_per_worker": args.pit_threads,
        "criterion": args.criterion,
        "evosuite_tools": evosuite_provenance,
        "quality_tools": quality_provenance,
        "tsdetect": smell_provenance,
        "smell_java_home": smell_java_home,
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "library_sha256": sha256_file(ROOT / "src" / "evosuite_full.py"),
        "repo_lifecycle": "clone once per sample; generate; JaCoCo; PIT; tsDetect; delete cache",
    }
    _atomic_json(output_dir / "provenance.json", provenance)
    if args.dry_run:
        print(json.dumps(provenance, ensure_ascii=False, indent=2))
        for row in selected_rows:
            print(f"{row['rank']}: {row['project_id']}/{row['sample_file']}")
        return 0

    with args.config.resolve().open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    generation_path = output_dir / "evosuite_records.jsonl"
    quality_path = output_dir / "quality_records.jsonl"
    smell_path = output_dir / "smell_records.jsonl"
    full_path = output_dir / "evosuite_full_quality_records.jsonl"
    if args.no_resume:
        generations: list[dict[str, Any]] = []
        qualities: list[dict[str, Any]] = []
        smells: list[dict[str, Any]] = []
        full: list[dict[str, Any]] = []
    else:
        generations = load_jsonl(generation_path)
        qualities = load_jsonl(quality_path)
        smells = load_jsonl(smell_path)
        full = load_jsonl(full_path)
    rerun_statuses = {
        status.strip().upper()
        for item in args.rerun_status
        for status in item.split(",")
        if status.strip()
    }
    component_keys = [
        {experiment_key(row) for row in generations},
        {quality_key(row) for row in qualities},
        {experiment_key(row) for row in smells},
        {experiment_key(row) for row in full},
    ]
    completed_keys = set.intersection(*component_keys) if all(component_keys) else set()
    rerun_keys = {
        experiment_key(row)
        for row in full
        if str(row.get("full_status") or "").upper() in rerun_statuses
    }
    completed_keys -= rerun_keys
    pending: list[tuple[dict[str, str], list[int]]] = []
    for row in selected_rows:
        missing = [
            seed
            for seed in args.seeds
            if (row["project_id"], row["sample_file"], seed) not in completed_keys
        ]
        if missing:
            pending.append((row, missing))
        else:
            print(f"[resume] {row['project_id']}/{row['sample_file']}")
    print(
        f"run={run_id}; shard={args.shard_index}/{args.shard_count}; "
        f"samples={len(selected_rows)}; pending={len(pending)}; workers={min(args.workers, max(1, len(pending)))}"
    )
    write_lock = threading.Lock()

    def worker(item: tuple[dict[str, str], list[int]]):
        row, seeds = item
        label = f"{row['rank']}:{row['project_id']}"
        print(f"[START] {label} seeds={seeds}", flush=True)
        try:
            result = run_full_sample(
                root=ROOT,
                dataset_dir=args.dataset.resolve(),
                manifest_row=row,
                config=config,
                run_id=run_id,
                seeds=seeds,
                evosuite_tools=evosuite_tools,
                quality_tools=quality_tools,
                detector_jar=args.detector_jar.resolve(),
                smell_java_home=smell_java_home,
                output_dir=output_dir,
                search_budget=args.search_budget,
                generation_timeout=generation_timeout,
                build_timeout=args.build_timeout,
                test_timeout=args.test_timeout,
                coverage_timeout=args.coverage_timeout,
                mutation_timeout=args.mutation_timeout,
                smell_timeout=args.smell_timeout,
                memory_mb=args.memory_mb,
                pit_threads=args.pit_threads,
                criterion=args.criterion,
                manual_java_home=args.java_home,
                keep_workspace=args.keep_workspace,
            )
        except Exception as exc:
            cache_root = ROOT / str(config.get("repo", {}).get("repos_dir", "repos"))
            cached_repo = cache_root / row["project_id"]
            cleanup_warning = ""
            if cached_repo.exists():
                try:
                    safe_remove_tree(cached_repo, cache_root)
                except Exception as cleanup_exc:
                    cleanup_warning = f"repo cleanup: {type(cleanup_exc).__name__}: {cleanup_exc}"
            batches = [
                _synthetic_failure(
                    run_id,
                    row,
                    seed,
                    exc,
                    repo_cache_removed=not cached_repo.exists(),
                    cleanup_warning=cleanup_warning,
                )
                for seed in seeds
            ]
            result = tuple([[batch[index] for batch in batches] for index in range(4)])
        print(
            f"[DONE] {label} -> {', '.join(str(record.get('full_status')) for record in result[3])}",
            flush=True,
        )
        return result

    with ThreadPoolExecutor(max_workers=min(args.workers, max(1, len(pending)))) as executor:
        futures = [executor.submit(worker, item) for item in pending]
        for future in as_completed(futures):
            generation_batch, quality_batch, smell_batch, full_batch = future.result()
            with write_lock:
                generations = _replace_batch(generations, generation_batch)
                qualities = _replace_batch(qualities, quality_batch)
                smells = _replace_batch(smells, smell_batch)
                full = _replace_batch(full, full_batch)
                persist_outputs(
                    output_dir,
                    generations,
                    qualities,
                    smells,
                    full,
                    expected_records=expected_records,
                )
    summary = persist_outputs(
        output_dir, generations, qualities, smells, full, expected_records=expected_records
    )
    if args.compact_dir:
        export_compact(output_dir, args.compact_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Output: {output_dir}")
    return 0 if summary.get("full_quality_ready") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Đã dừng; record hoàn tất đã được lưu atomic để resume.", file=sys.stderr)
        raise SystemExit(130)
