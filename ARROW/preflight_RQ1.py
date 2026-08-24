#!/usr/bin/env python3
"""Kiểm tra môi trường repository trước khi gọi LLM cho RQ1.

Script không thêm generated test và không sửa source của repository. Nó chỉ
clone/checkout đúng revision, chọn JDK, xác định Maven/Gradle/JUnit và chạy
baseline. Kết quả là eligible_manifest.csv dùng làm input an toàn cho run_RQ1.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.build_runner import BuildContext, verify_baseline
from src.java_resolver import resolve_java_home
from src.repo_manager import ensure_experiment_workspace, safe_remove_tree
from src.run_pipeline import _prepare_repo, dataset_dir, load_config
from src.input_selector import select_inputs
from src.project_analyzer import analyze_experiment


ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight RQ1: chỉ kiểm tra baseline, không gọi LLM.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "pipeline.yaml")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--java-home", default="")
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--keep-repo-cache", action="store_true")
    return parser.parse_args()


def load_manifest(path: Path, limit: int) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        rows = list(reader)
    if not {"project_id", "sample_file"}.issubset(headers):
        raise ValueError("Manifest phải có project_id và sample_file")
    seen: set[str] = set()
    for row in rows:
        project = str(row.get("project_id") or "").strip()
        sample = Path(str(row.get("sample_file") or "").strip()).name
        if not project or not sample:
            raise ValueError(f"Hàng manifest thiếu project_id/sample_file: {row}")
        if project in seen:
            raise ValueError(f"project_id {project} xuất hiện nhiều lần; RQ1 cần tối đa một sample/repository")
        seen.add(project)
        row["sample_file"] = sample
    return headers, rows[:limit] if limit > 0 else rows


def classify(exc: Exception | None, baseline: Any | None) -> tuple[str, str]:
    if exc is not None:
        return "PREFLIGHT_ERROR", f"{type(exc).__name__}: {exc}"
    state = baseline.state.value if baseline and baseline.state else "UNKNOWN"
    origin = baseline.failure_origin.value if baseline and baseline.failure_origin else "UNKNOWN"
    if state == "MODULE_TESTS_PASSED":
        return "ELIGIBLE", "baseline module tests passed"
    return f"EXCLUDE_{origin}_{state}", (baseline.primary_error or baseline.normalized_error_signature or "baseline did not pass")[:1000]


def preflight_one(row: dict[str, str], config: dict[str, Any], args: argparse.Namespace, root: Path) -> dict[str, str]:
    project_id, sample_file = row["project_id"], row["sample_file"]
    started = time.monotonic()
    workspace = root / "workspaces" / project_id / Path(sample_file).stem
    cached: Path | None = None
    result = dict(row)
    result.update({
        "build_tool": "", "testing_framework": "", "detected_java_version": "", "selected_java_home": "",
        "java_selection_reason": "", "baseline_state": "", "baseline_origin": "", "preflight_status": "",
        "preflight_reason": "", "elapsed_seconds": "",
    })
    try:
        samples = select_inputs(dataset_dir(), config, start_index=0, project_id=project_id, sample_file=sample_file, limit=1)
        if len(samples) != 1:
            raise RuntimeError("dataset sample không tồn tại hoặc không duy nhất")
        sample = samples[0]
        cached = _prepare_repo(sample, config)
        ensure_experiment_workspace(cached_repo=cached, experiment_workspace=workspace)
        context, module_root = analyze_experiment(sample=sample, workspace=workspace, run_id="preflight", shard_id="preflight", agent_name="preflight", generation_prompt="baseline")
        selection = resolve_java_home(workspace, module_root, config, manual_java_home=args.java_home or None)
        build_cfg = config.get("build", {})
        maven_cfg = build_cfg.get("maven", {})
        build_context = BuildContext(
            repository_root=workspace, module_root=module_root, build_tool=context.build_tool,
            generated_test_class_name=context.generated_test_class_name,
            generated_test_fqcn=f"{context.package_name}.{context.generated_test_class_name}" if context.package_name else context.generated_test_class_name,
            timeout_seconds=int(build_cfg.get("test_timeout_seconds", 900)),
            prefer_wrapper=bool(build_cfg.get("prefer_wrapper", True)), java_home=selection.java_home or None,
            maven_multi_module_strategy=maven_cfg.get("multi_module_strategy", "module_only"),
            maven_use_also_make=bool(maven_cfg.get("use_also_make", True)),
            maven_fail_if_no_specified_tests=bool(maven_cfg.get("fail_if_no_specified_tests", False)),
        )
        baseline = verify_baseline(build_context)
        status, reason = classify(None, baseline)
        result.update({
            "build_tool": context.build_tool, "testing_framework": context.testing_framework,
            "detected_java_version": context.java_version, "selected_java_home": selection.java_home,
            "java_selection_reason": selection.reason, "baseline_state": baseline.state.value if baseline.state else "",
            "baseline_origin": baseline.failure_origin.value if baseline.failure_origin else "", "preflight_status": status,
            "preflight_reason": reason,
        })
        (root / "logs" / project_id / f"{Path(sample_file).stem}_baseline.txt").parent.mkdir(parents=True, exist_ok=True)
        (root / "logs" / project_id / f"{Path(sample_file).stem}_baseline.txt").write_text(baseline.raw_output, encoding="utf-8")
    except Exception as exc:
        status, reason = classify(exc, None)
        result.update({"preflight_status": status, "preflight_reason": reason})
    finally:
        result["elapsed_seconds"] = f"{time.monotonic() - started:.3f}"
        if workspace.exists() and not args.keep_workspace:
            safe_remove_tree(workspace, root / "workspaces")
        if cached is not None and cached.exists() and not args.keep_repo_cache:
            repos_dir = ROOT / str(config.get("repo", {}).get("repos_dir", "repos"))
            safe_remove_tree(cached, repos_dir)
    return result


def write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers phải >= 1")
    config = load_config(args.config.resolve())
    original_headers, rows = load_manifest(args.manifest.resolve(), args.limit)
    run_id = args.run_id or f"preflight-rq1-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    root = ROOT / "runs" / "rq1_preflight" / run_id
    root.mkdir(parents=True, exist_ok=True)
    print(f"Preflight {len(rows)} sample với {min(args.workers, len(rows))} worker; không gọi LLM.")
    results: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(args.workers, len(rows))) as executor:
        futures = {executor.submit(preflight_one, row, config, args, root): row for row in rows}
        for future in as_completed(futures):
            result = future.result(); results.append(result)
            print(f"{result['project_id']}/{result['sample_file']}: {result['preflight_status']}", flush=True)
    results.sort(key=lambda item: (int(item.get("rank") or 0), item["project_id"]))
    added = ["build_tool", "testing_framework", "detected_java_version", "selected_java_home", "java_selection_reason", "baseline_state", "baseline_origin", "preflight_status", "preflight_reason", "elapsed_seconds"]
    headers = [*original_headers, *[name for name in added if name not in original_headers]]
    write_csv(root / "preflight_results.csv", headers, results)
    eligible = [item for item in results if item["preflight_status"] == "ELIGIBLE"]
    write_csv(root / "eligible_manifest.csv", original_headers, eligible)
    summary = {"run_id": run_id, "total": len(results), "eligible": len(eligible), "excluded": len(results)-len(eligible), "by_status": {}}
    for item in results: summary["by_status"][item["preflight_status"]] = summary["by_status"].get(item["preflight_status"], 0) + 1
    (root / "preflight_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Eligible manifest: {root / 'eligible_manifest.csv'}")
    print(f"Full audit:       {root / 'preflight_results.csv'}")
    print(f"Summary:          {root / 'preflight_summary.json'}")


if __name__ == "__main__":
    main()
