#!/usr/bin/env python3
"""Chạy lại RQ1 trên đúng các sample được khóa trong một manifest CSV.

RQ1 chỉ đo sinh test ban đầu. Script này tắt Adaptive Repair, chạy ba prompt
trên từng sample trong manifest, và xuất raw/detail/summary có các chỉ số:
CSR, ESR, TPR, valid rate, coverage và mutation (valid-only + end-to-end).

Ví dụ:
  cd ARROW
  python run_RQ1.py --manifest shards/repo_shard_05_manifest.csv \
    --agent qwen-coder-2.5-7b --run-id rq1-20260821

Muốn chạy thử 2 sample đầu mà không gọi LLM:
  python run_RQ1.py --manifest shards/repo_shard_05_manifest.csv \
    --agent qwen-coder-2.5-7b --limit 2 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_PROMPTS = ("zero-shot", "few-shot", "zero-shot-project-aware")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chạy RQ1 repository-aware không repair và xuất kết quả đầy đủ.")
    parser.add_argument("--manifest", type=Path, required=True, help="CSV có cột project_id và sample_file.")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "pipeline.yaml")
    parser.add_argument("--agent", action="append", required=True, help="Tên agent/model trong pipeline.yaml; lặp lại để chạy nhiều model.")
    parser.add_argument("--run-id", default="", help="Mã run. Mặc định tự tạo theo UTC.")
    parser.add_argument("--limit", type=int, default=0, help="Chỉ chạy N sample đầu manifest; 0 = toàn bộ.")
    parser.add_argument("--workers", type=int, default=1, help="Số sample chạy song song; khuyến nghị 2-4, mặc định 1.")
    parser.add_argument("--start-rank", type=int, default=0, help="Bỏ qua các hàng manifest có rank nhỏ hơn giá trị này.")
    parser.add_argument("--java-home", default="", help="JAVA_HOME truyền cho pipeline, nếu cần.")
    parser.add_argument("--keep-workspace", action="store_true", help="Giữ workspace để debug các failure.")
    parser.add_argument("--keep-repo-cache", action="store_true", help="Giữ bản clone repository để chạy nhanh hơn/debug.")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ kiểm tra manifest và in lệnh, không gọi LLM/build.")
    parser.add_argument("--skip-metrics", action="store_true", help="Không chạy JaCoCo/PIT/tsDetect; khi đó summary coverage/mutation để trống.")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: Path, start_rank: int, limit: int) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy manifest: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"project_id", "sample_file"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"Manifest phải có cột {sorted(required)}")
    selected: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    projects: set[str] = set()
    for row in rows:
        project_id = str(row.get("project_id", "")).strip()
        sample_file = Path(str(row.get("sample_file", "")).strip()).name
        if not project_id or not sample_file:
            raise ValueError(f"Manifest có hàng thiếu project_id/sample_file: {row}")
        rank_text = str(row.get("rank", "")).strip()
        rank = int(rank_text) if rank_text.isdigit() else 0
        if start_rank and rank and rank < start_rank:
            continue
        key = (project_id, sample_file)
        if key in seen:
            raise ValueError(f"Manifest có sample lặp: {project_id}/{sample_file}")
        if project_id in projects:
            raise ValueError(
                f"Manifest không còn one-sample-per-repository: project_id {project_id} xuất hiện nhiều lần. "
                "Sửa manifest trước khi chạy RQ1."
            )
        seen.add(key)
        projects.add(project_id)
        selected.append({"project_id": project_id, "sample_file": sample_file, "rank": rank_text})
    if limit > 0:
        selected = selected[:limit]
    if not selected:
        raise ValueError("Không có sample nào được chọn từ manifest")
    return selected


def write_rq1_config(source: Path, destination: Path, run_id: str) -> None:
    """Giữ config gốc, chỉ override các trường cần thiết cho RQ1.

    YAML cho phép khóa ở cuối ghi đè khóa trước. Cách này không cần sửa
    pipeline.yaml của người dùng và bảo đảm adaptive repair bị tắt.
    """
    original = source.read_text(encoding="utf-8")
    override = (
        "\n# Override do run_RQ1.py tạo; không sửa pipeline.yaml gốc.\n"
        "run:\n"
        f"  run_id: {run_id}\n"
        "adaptive_repair:\n"
        "  enabled: false\n"
    )
    destination.write_text(original + override, encoding="utf-8")


def load_rows(runs_dir: Path, run_id: str, shard_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(runs_dir.glob("**/reports/records/experiments.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("run_id")) == run_id and str(row.get("shard_id")) == shard_id:
                    rows.append(row)
    return rows


def as_bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1", "yes"}


def pct(numerator: int, denominator: int) -> float | str:
    return round(numerator * 100 / denominator, 2) if denominator else ""


def number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize(rows: list[dict[str, Any]], expected_samples: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row.get("agent_name", "")), str(row.get("model", "")), str(row.get("generation_prompt_strategy", "")))].append(row)
    output: list[dict[str, Any]] = []
    for (agent, model, prompt), items in sorted(groups.items()):
        raw_total = len(items)
        eligible = [item for item in items if str(item.get("baseline_state", "")) == "MODULE_TESTS_PASSED"]
        # Baseline-invalid projects are environmental exclusions, not failures
        # of a prompt/LLM. They remain in raw records for auditability but are
        # excluded consistently from every RQ1 rate denominator.
        total = len(eligible)
        baseline_ok = total
        compiled = sum(as_bool(item.get("compilation")) for item in eligible)
        # Target verification classifies assertion/runtime failures only after
        # the framework discovered and executed the generated test. This is
        # the correct ESR denominator; target_test_passed alone would make
        # ESR accidentally identical to TPR.
        executed_states = {"RUNTIME_FAILED", "ASSERTION_FAILED", "TARGET_TEST_PASSED"}
        executed = sum(str(item.get("initial_failure_state", "")) in executed_states for item in eligible)
        target_passed = sum(as_bool(item.get("target_test_passed")) for item in eligible)
        valid = sum(as_bool(item.get("module_tests_passed")) for item in eligible)
        def values(field: str) -> list[float]:
            return [value for item in eligible if as_bool(item.get("module_tests_passed")) for value in [number(item.get(field))] if value is not None]
        line_values, branch_values, method_values, mutation_values = (values(field) for field in ("coverage_line", "coverage_branch", "coverage_method", "mutation_score"))
        def end_to_end(field: str) -> float | str:
            # Missing metric on an otherwise valid test is kept missing, not invented as zero.
            if total == 0 or any(number(item.get(field)) is None for item in eligible if as_bool(item.get("module_tests_passed"))):
                return ""
            return round(sum(number(item.get(field)) or 0 for item in eligible if as_bool(item.get("module_tests_passed"))) / total, 2)
        output.append({
            "agent_name": agent, "model": model, "prompt_strategy": prompt,
            "expected_manifest_samples": expected_samples, "records_written": raw_total,
            "baseline_invalid_excluded_n": raw_total - total,
            "baseline_valid_evaluable_n": total,
            "missing_records": max(0, expected_samples - raw_total),
            "baseline_passed_n": baseline_ok, "baseline_passed_pct": pct(baseline_ok, total),
            "compilation_success_n": compiled, "CSR_pct": pct(compiled, total),
            "execution_success_n": executed, "ESR_pct": pct(executed, total),
            "target_pass_n": target_passed, "TPR_pct": pct(target_passed, total),
            "valid_test_n": valid, "valid_rate_pct": pct(valid, total),
            "line_coverage_valid_only_pct": round(fmean(line_values), 2) if line_values else "",
            "branch_coverage_valid_only_pct": round(fmean(branch_values), 2) if branch_values else "",
            "method_coverage_valid_only_pct": round(fmean(method_values), 2) if method_values else "",
            "mutation_score_valid_only_pct": round(fmean(mutation_values), 2) if mutation_values else "",
            "line_coverage_end_to_end_pct": end_to_end("coverage_line"),
            "branch_coverage_end_to_end_pct": end_to_end("coverage_branch"),
            "method_coverage_end_to_end_pct": end_to_end("coverage_method"),
            "mutation_score_end_to_end_pct": end_to_end("mutation_score"),
        })
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    manifest = args.manifest.resolve()
    config = args.config.resolve()
    samples = read_manifest(manifest, args.start_rank, args.limit)
    if args.workers < 1:
        raise ValueError("--workers phải lớn hơn hoặc bằng 1")
    run_id = args.run_id or f"rq1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    shard_id = f"rq1-{run_id}"
    output_dir = ROOT / "runs" / "rq1_exports" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_lock = output_dir / "manifest_locked.csv"
    shutil.copy2(manifest, manifest_lock)
    provenance = {
        "run_id": run_id, "shard_id": shard_id, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest), "manifest_sha256": sha256_file(manifest),
        "config": str(config), "config_sha256": sha256_file(config),
        "samples_selected": len(samples), "unique_repositories": len({item['project_id'] for item in samples}),
        "agents": args.agent, "prompts": list(DEFAULT_PROMPTS), "adaptive_repair": False,
        "metrics_enabled": not args.skip_metrics, "workers": min(args.workers, len(samples)),
    }
    (output_dir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"RQ1 run_id={run_id}; samples={len(samples)}; agents={', '.join(args.agent)}")
    print(f"Manifest lock: {manifest_lock}")
    with tempfile.TemporaryDirectory(prefix="arrow-rq1-") as temp:
        rq1_config = Path(temp) / "pipeline_rq1.yaml"
        write_rq1_config(config, rq1_config, run_id)
        def run_sample(index: int, sample: dict[str, str]) -> tuple[int, dict[str, str], int]:
            command = [sys.executable, "-m", "src.run_pipeline", "--config", str(rq1_config), "--project-id", sample["project_id"], "--sample-file", sample["sample_file"], "--limit", "1", "--shard-id", shard_id]
            for agent in args.agent:
                command.extend(["--agent", agent])
            for prompt in DEFAULT_PROMPTS:
                command.extend(["--generation-prompt", prompt])
            if args.java_home:
                command.extend(["--java-home", args.java_home])
            if args.keep_workspace:
                command.append("--keep-workspace")
            if args.keep_repo_cache:
                command.append("--keep-repo-cache")
            if args.skip_metrics:
                command.append("--skip-metrics")
            if args.dry_run:
                command.append("--dry-run")
            print(f"[{index}/{len(samples)}] {sample['project_id']}/{sample['sample_file']}", flush=True)
            completed = subprocess.run(command, cwd=ROOT)
            return index, sample, completed.returncode

        failures: list[tuple[int, dict[str, str], int]] = []
        worker_count = min(args.workers, len(samples))
        if worker_count == 1:
            completed_samples = (run_sample(index, sample) for index, sample in enumerate(samples, 1))
            for index, sample, exit_code in completed_samples:
                if exit_code != 0:
                    failures.append((index, sample, exit_code))
        else:
            print(f"Chạy song song {worker_count} sample; mỗi sample gồm {len(DEFAULT_PROMPTS)} prompt.", flush=True)
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rq1") as executor:
                pending = [executor.submit(run_sample, index, sample) for index, sample in enumerate(samples, 1)]
                for future in as_completed(pending):
                    index, sample, exit_code = future.result()
                    if exit_code != 0:
                        failures.append((index, sample, exit_code))
        for index, sample, exit_code in sorted(failures):
            print(f"WARNING: sample [{index}] {sample['project_id']}/{sample['sample_file']} exit={exit_code}; tiếp tục batch.", file=sys.stderr)
    if args.dry_run:
        return
    rows = load_rows(ROOT / "runs", run_id, shard_id)
    write_csv(output_dir / "rq1_raw_records.csv", rows)
    summary = summarize(rows, len(samples))
    if summary:
        write_csv(output_dir / "rq1_summary_full.csv", summary)
    (output_dir / "run_status.json").write_text(json.dumps({**provenance, "records_found": len(rows), "summary_groups": len(summary), "failed_subprocesses": len(failures)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Raw records: {output_dir / 'rq1_raw_records.csv'}")
    print(f"Summary:     {output_dir / 'rq1_summary_full.csv'}")
    print(f"Provenance:  {output_dir / 'provenance.json'}")
    if len(rows) != len(samples) * len(args.agent) * len(DEFAULT_PROMPTS):
        print("WARNING: thiếu record. Xem rq1_raw_records.csv, run_status.json và logs trong runs/ trước khi kết luận RQ1.", file=sys.stderr)


if __name__ == "__main__":
    main()
