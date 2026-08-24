from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from .evosuite_quality import QualityTools, run_quality_sample
from .evosuite_runner import (
    EvoSuiteTools,
    experiment_key,
    java_executable,
    resolve_focal_path,
    run_sample as run_evosuite_sample,
    sha256_file,
)
from .input_selector import load_sample
from .metrics_runner import SMELL_ALIASES, SMELL_COLUMNS
from .repo_manager import safe_remove_tree


TSDETECT_VERSION = "2.2"
TSDETECT_URL = (
    "https://github.com/TestSmells/TestSmellDetector/releases/download/"
    f"v{TSDETECT_VERSION}/TestSmellDetector.jar"
)
TSDETECT_SHA256 = "6a12de6d1613c7ee9e845a2b69d876834f8ee98379e85d38c0e6027f7110f7bc"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def partition_manifest_rows(
    rows: list[dict[str, str]], shard_count: int, shard_index: int
) -> list[dict[str, str]]:
    """Split the final manifest evenly by its locked row order.

    The clean-sample manifests inherited candidate ranks, so partitioning by
    those ranks can yield highly uneven EvoSuite shards.  Position-based
    partitioning gives exactly 40 rows per machine for a 200-row/5-machine run.
    """
    if shard_count < 1:
        raise ValueError("shard_count phải >= 1")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError("shard_index phải nằm trong [0, shard_count)")
    return [row for position, row in enumerate(rows) if position % shard_count == shard_index]


def validate_manifest_coverage(
    all_rows: list[dict[str, str]], shard_rows: list[dict[str, str]], shard_count: int, shard_index: int
) -> None:
    expected = partition_manifest_rows(all_rows, shard_count, shard_index)
    expected_keys = [(row["project_id"], row["sample_file"]) for row in expected]
    actual_keys = [(row["project_id"], row["sample_file"]) for row in shard_rows]
    if actual_keys != expected_keys:
        raise ValueError(f"Manifest shard {shard_index}/{shard_count} không khớp phân vùng đã khóa")


def _normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def strict_smell_values(row: dict[str, str]) -> dict[str, int]:
    """Parse every paper smell column without fabricating missing zeroes."""
    normalized = {_normalized_header(str(key)): value for key, value in row.items()}
    result: dict[str, int] = {}
    missing: list[str] = []
    for smell in SMELL_COLUMNS:
        raw: Any = None
        for alias in [smell, *SMELL_ALIASES.get(smell, [])]:
            value = normalized.get(_normalized_header(alias))
            if value not in {None, ""}:
                raw = value
                break
        if raw is None:
            missing.append(smell)
            continue
        try:
            numeric = float(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"tsDetect trả giá trị không phải số cho {smell}: {raw!r}") from exc
        if numeric < 0 or not numeric.is_integer():
            raise ValueError(f"tsDetect trả giá trị không hợp lệ cho {smell}: {raw!r}")
        result[smell] = int(numeric)
    if missing:
        raise ValueError("tsDetect thiếu entity bắt buộc: " + ", ".join(missing))
    return result


def _evosuite_test_source(artifact_dir: Path) -> Path:
    candidates = [
        path
        for path in (artifact_dir / "evosuite-tests").rglob("*_ESTest.java")
        if not path.stem.endswith("_scaffolding")
    ]
    if len(candidates) != 1:
        raise ValueError(f"Cần đúng một EvoSuite test source trong {artifact_dir}; tìm thấy {len(candidates)}")
    return candidates[0]


def not_applicable_quality(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": source.get("run_id", ""),
        "manifest_rank": source.get("manifest_rank", ""),
        "project_id": source.get("project_id", ""),
        "sample_file": source.get("sample_file", ""),
        "input_id": source.get("input_id", ""),
        "seed": source.get("seed", ""),
        "source_valid": False,
        "quality_status": "NOT_APPLICABLE_INVALID",
        "coverage_complete": False,
        "mutation_complete": False,
        "coverage_error": "",
        "mutation_error": "",
        "not_applicable_reason": f"EvoSuite status={source.get('status', 'UNKNOWN')}",
        "started_at_utc": utc_now(),
        "finished_at_utc": utc_now(),
    }


def not_applicable_smell(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": source.get("run_id", ""),
        "manifest_rank": source.get("manifest_rank", ""),
        "project_id": source.get("project_id", ""),
        "sample_file": source.get("sample_file", ""),
        "input_id": source.get("input_id", ""),
        "seed": source.get("seed", ""),
        "source_valid": False,
        "smell_status": "NOT_APPLICABLE_INVALID",
        "smell_complete": False,
        "smell_error": "",
        "not_applicable_reason": f"EvoSuite status={source.get('status', 'UNKNOWN')}",
        "started_at_utc": utc_now(),
        "finished_at_utc": utc_now(),
    }


def run_smell_sample(
    *,
    root: Path,
    dataset_dir: Path,
    source_record: dict[str, Any],
    config: dict[str, Any],
    detector_jar: Path,
    java_home: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    if source_record.get("valid") is not True:
        return not_applicable_smell(source_record)
    record: dict[str, Any] = {
        "run_id": source_record.get("run_id", ""),
        "manifest_rank": source_record.get("manifest_rank", ""),
        "project_id": source_record.get("project_id", ""),
        "sample_file": source_record.get("sample_file", ""),
        "input_id": source_record.get("input_id", ""),
        "seed": source_record.get("seed", ""),
        "source_valid": source_record.get("valid") is True,
        "smell_status": "FAILED",
        "smell_complete": False,
        "smell_error": "",
        "started_at_utc": utc_now(),
        "finished_at_utc": "",
    }
    try:
        detector = detector_jar.resolve()
        if not detector.is_file():
            raise FileNotFoundError(f"Không tìm thấy tsDetect: {detector}")
        artifact_dir = Path(str(source_record.get("artifact_dir") or ""))
        smell_root = artifact_dir / "tsdetect"
        smell_root.mkdir(parents=True, exist_ok=True)
        cache_root = root / str(config.get("repo", {}).get("repos_dir", "repos"))
        cached_repo = cache_root / str(source_record.get("project_id") or "")
        if not cached_repo.is_dir():
            raise FileNotFoundError(f"Repo cache đã bị xóa trước tsDetect: {cached_repo}")
        sample_path = dataset_dir / str(source_record["project_id"]) / str(source_record["sample_file"])
        sample = load_sample(sample_path, dataset_dir)
        focal_source = resolve_focal_path(cached_repo, sample.focal_class_path)
        test_source = _evosuite_test_source(artifact_dir)
        if not focal_source.is_file():
            raise FileNotFoundError(f"Không tìm thấy focal source: {focal_source}")
        detector_id = (
            f"{source_record.get('project_id')}:{source_record.get('input_id')}:"
            f"seed-{source_record.get('seed')}"
        )
        input_csv = smell_root / "pathToInputFile.csv"
        with input_csv.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([detector_id, test_source.resolve(), focal_source.resolve()])
        for stale in smell_root.glob("Output_TestSmellDetection*.csv"):
            stale.unlink()
        selected_java_home = java_home or str(source_record.get("java_home") or "")
        command = [java_executable(selected_java_home or None, "java"), "-jar", str(detector), str(input_csv)]
        (smell_root / "tsdetect_command.json").write_text(
            json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        completed = subprocess.run(
            command,
            cwd=smell_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
        )
        (smell_root / "tsdetect.log").write_text(
            (completed.stdout or "")
            + ("\n" if completed.stdout and completed.stderr else "")
            + (completed.stderr or ""),
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"tsDetect exit code {completed.returncode}")
        outputs = sorted(smell_root.glob("Output_TestSmellDetection*.csv"), key=lambda path: path.stat().st_mtime)
        if len(outputs) != 1:
            raise ValueError(f"tsDetect phải tạo đúng một CSV; tìm thấy {len(outputs)}")
        with outputs[0].open(encoding="utf-8-sig", newline="") as handle:
            detected = list(csv.DictReader(handle))
        if len(detected) != 1:
            raise ValueError(f"tsDetect phải trả đúng một row; nhận {len(detected)}")
        if str(detected[0].get("App") or "") != detector_id:
            raise ValueError(
                f"tsDetect ID mismatch: expected={detector_id!r}, actual={detected[0].get('App')!r}"
            )
        values = strict_smell_values(detected[0])
        total = sum(values.values())
        record.update(
            {
                "smell_status": "COMPLETE",
                "smell_complete": True,
                "smell_error": "",
                "smell_instances": total,
                "smell_free": total == 0,
                "smell_java_home": selected_java_home,
                "smell_artifact_dir": str(smell_root),
                "smell_raw_csv": str(outputs[0]),
                **values,
            }
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        record["smell_error"] = f"{type(exc).__name__}: {exc}"
        record["smell_status"] = "FAILED"
        record["smell_complete"] = False
    finally:
        record["finished_at_utc"] = utc_now()
    return record


def combine_full_record(
    generation: dict[str, Any], quality: dict[str, Any], smell: dict[str, Any]
) -> dict[str, Any]:
    common = {"run_id", "manifest_rank", "project_id", "sample_file", "input_id", "seed"}
    record = dict(generation)
    record.update({key: value for key, value in quality.items() if key not in common})
    record.update({key: value for key, value in smell.items() if key not in common})
    if generation.get("valid") is not True:
        infrastructure_statuses = {
            "TOOL_ERROR",
            "BASELINE_FAILED",
            "CLASSPATH_FAILED",
            "CLASS_NOT_COMPILED",
            "GENERATION_TIMEOUT",
            "COMPILE_TIMEOUT",
            "TEST_TIMEOUT",
        }
        record["full_status"] = (
            "INFRA_ERROR"
            if str(generation.get("status") or "").upper() in infrastructure_statuses
            else "GENERATION_INVALID"
        )
        record["full_metrics_complete"] = False
    else:
        complete = (
            quality.get("coverage_complete") is True
            and quality.get("mutation_complete") is True
            and smell.get("smell_complete") is True
        )
        record["full_status"] = "COMPLETE" if complete else "PARTIAL"
        record["full_metrics_complete"] = complete
    return record


def run_full_sample(
    *,
    root: Path,
    dataset_dir: Path,
    manifest_row: dict[str, str],
    config: dict[str, Any],
    run_id: str,
    seeds: list[int],
    evosuite_tools: EvoSuiteTools,
    quality_tools: QualityTools,
    detector_jar: Path,
    smell_java_home: str,
    output_dir: Path,
    search_budget: int,
    generation_timeout: int,
    build_timeout: int,
    test_timeout: int,
    coverage_timeout: int,
    mutation_timeout: int,
    smell_timeout: int,
    memory_mb: int,
    pit_threads: int,
    criterion: str,
    manual_java_home: str,
    keep_workspace: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    cache_root = root / str(config.get("repo", {}).get("repos_dir", "repos"))
    cached_repo = cache_root / str(manifest_row.get("project_id") or "")
    generations: list[dict[str, Any]] = []
    qualities: list[dict[str, Any]] = []
    smells: list[dict[str, Any]] = []
    full: list[dict[str, Any]] = []
    cleanup_warning = ""
    try:
        # Keep the cache only inside this function. The outer finally always
        # removes it after JaCoCo, PIT and tsDetect finish for this sample.
        generations = run_evosuite_sample(
            root=root,
            dataset_dir=dataset_dir,
            manifest_row=manifest_row,
            config=config,
            run_id=run_id,
            seeds=seeds,
            tools=evosuite_tools,
            output_dir=output_dir,
            search_budget=search_budget,
            generation_timeout=generation_timeout,
            build_timeout=build_timeout,
            test_timeout=test_timeout,
            memory_mb=memory_mb,
            criterion=criterion,
            manual_java_home=manual_java_home,
            keep_workspace=keep_workspace,
            keep_repo_cache=True,
        )
        for generation in generations:
            if generation.get("valid") is True:
                quality = run_quality_sample(
                    root=root,
                    dataset_dir=dataset_dir,
                    source_record=generation,
                    config=config,
                    evosuite_tools=evosuite_tools,
                    quality_tools=quality_tools,
                    coverage_timeout=coverage_timeout,
                    mutation_timeout=mutation_timeout,
                    memory_mb=memory_mb,
                    pit_threads=pit_threads,
                    keep_workspace=keep_workspace,
                    keep_repo_cache=True,
                )
                smell = run_smell_sample(
                    root=root,
                    dataset_dir=dataset_dir,
                    source_record=generation,
                    config=config,
                    detector_jar=detector_jar,
                    java_home=smell_java_home,
                    timeout_seconds=smell_timeout,
                )
            else:
                quality = not_applicable_quality(generation)
                smell = not_applicable_smell(generation)
            qualities.append(quality)
            smells.append(smell)
            full.append(combine_full_record(generation, quality, smell))
    finally:
        if cached_repo.exists():
            try:
                safe_remove_tree(cached_repo, cache_root)
            except Exception as exc:  # Persist cleanup failure for audit/retry.
                cleanup_warning = f"repo cleanup: {type(exc).__name__}: {exc}"
        removed = not cached_repo.exists()
        for collection in (generations, qualities, smells, full):
            for record in collection:
                record["repo_cache_removed"] = removed
                if cleanup_warning:
                    record["cleanup_warning"] = cleanup_warning
    return generations, qualities, smells, full


def _percent(numerator: int, denominator: int) -> float | str:
    return round(numerator * 100 / denominator, 2) if denominator else ""


def _numeric_mean(rows: list[dict[str, Any]], field: str) -> float | str:
    values: list[float] = []
    for row in rows:
        value = row.get(field, "")
        if value in {None, ""}:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return round(fmean(values), 2) if values else ""


def _weighted_coverage(rows: list[dict[str, Any]], prefix: str) -> float | str:
    covered = sum(int(row.get(f"{prefix}_covered") or 0) for row in rows)
    missed = sum(int(row.get(f"{prefix}_missed") or 0) for row in rows)
    return _percent(covered, covered + missed)


def summarize_full_run(
    generation_records: list[dict[str, Any]],
    quality_records: list[dict[str, Any]],
    smell_records: list[dict[str, Any]],
    full_records: list[dict[str, Any]],
    *,
    expected_records: int,
) -> dict[str, Any]:
    valid = [row for row in generation_records if row.get("valid") is True]
    valid_keys = {experiment_key(row) for row in valid}
    coverage = [row for row in quality_records if row.get("coverage_complete") is True]
    mutation = [row for row in quality_records if row.get("mutation_complete") is True]
    def valid_smell_record(row: dict[str, Any]) -> bool:
        if row.get("smell_complete") is not True:
            return False
        try:
            entity_total = sum(int(row[entity]) for entity in SMELL_COLUMNS)
            return entity_total == int(row["smell_instances"])
        except (KeyError, TypeError, ValueError):
            return False

    smell = [row for row in smell_records if valid_smell_record(row)]
    complete = [row for row in full_records if row.get("full_metrics_complete") is True]
    all_keys = [experiment_key(row) for row in generation_records]
    duplicate_key_n = len(all_keys) - len(set(all_keys))
    total_smells = sum(int(row.get("smell_instances") or 0) for row in smell)
    smell_free_n = sum(row.get("smell_free") is True for row in smell)
    mutation_total = sum(int(row.get("mutations_total") or 0) for row in mutation)
    mutation_killed = sum(int(row.get("mutations_killed") or 0) for row in mutation)
    summary: dict[str, Any] = {
        "model": "EvoSuite",
        "prompt": "Search-based baseline",
        "expected_records": expected_records,
        "generation_records_n": len(generation_records),
        "valid_test_n": len(valid),
        "valid_rate_end_to_end_pct": _percent(len(valid), len(generation_records)),
        "coverage_complete_n": len(coverage),
        "mutation_complete_or_not_applicable_n": len(mutation),
        "smell_complete_n": len(smell),
        "full_metric_record_n": len(complete),
        "duplicate_key_n": duplicate_key_n,
        "missing_generation_record_n": max(0, expected_records - len(set(all_keys))),
        "smell_instances_total": total_smells,
        "smell_density": round(total_smells / len(valid), 2) if valid else "",
        "smell_free_tests_n": smell_free_n,
        "smell_free_tests_pct": _percent(smell_free_n, len(valid)),
        "smell_free_tests_display": f"{smell_free_n} ({smell_free_n * 100 / len(valid):.2f}%)" if valid else "",
        "IC_macro_mean_pct": _numeric_mean(coverage, "coverage_instruction"),
        "LC_macro_mean_pct": _numeric_mean(coverage, "coverage_line"),
        "BC_macro_mean_pct": _numeric_mean(coverage, "coverage_branch"),
        "MC_macro_mean_pct": _numeric_mean(coverage, "coverage_method"),
        "MS_macro_mean_pct": _numeric_mean(mutation, "mutation_score"),
        "IC_weighted_pct": _weighted_coverage(coverage, "instruction"),
        "LC_weighted_pct": _weighted_coverage(coverage, "line"),
        "BC_weighted_pct": _weighted_coverage(coverage, "branch"),
        "MC_weighted_pct": _weighted_coverage(coverage, "method"),
        "MS_weighted_pct": _percent(mutation_killed, mutation_total),
        "mutations_total": mutation_total,
        "mutations_killed": mutation_killed,
        "repo_cache_removed_n": sum(row.get("repo_cache_removed") is True for row in generation_records),
    }
    summary["generation_status_counts"] = {
        status: sum(str(row.get("status") or "UNKNOWN") == status for row in generation_records)
        for status in sorted({str(row.get("status") or "UNKNOWN") for row in generation_records})
    }
    summary["full_status_counts"] = {
        status: sum(str(row.get("full_status") or "UNKNOWN") == status for row in full_records)
        for status in sorted({str(row.get("full_status") or "UNKNOWN") for row in full_records})
    }
    summary["smell_entity_totals"] = {
        entity: sum(int(row.get(entity) or 0) for row in smell) for entity in SMELL_COLUMNS
    }
    summary["table_iii_ready"] = bool(valid) and len(smell) == len(valid) and {
        experiment_key(row) for row in smell
    } == valid_keys
    summary["full_quality_ready"] = (
        len(generation_records) == expected_records
        and duplicate_key_n == 0
        and bool(valid)
        and len(coverage) == len(valid)
        and len(mutation) == len(valid)
        and len(smell) == len(valid)
        and len(complete) == len(valid)
    )
    return summary


def table_iii_row(summary: dict[str, Any]) -> dict[str, Any]:
    if not summary.get("table_iii_ready"):
        return {
            "Model": "EvoSuite",
            "Prompt": "Search-based baseline",
            "Valid tests": summary.get("valid_test_n", 0),
            "Smell Density": "",
            "Smell-free Tests": "",
        }
    return {
        "Model": "EvoSuite",
        "Prompt": "Search-based baseline",
        "Valid tests": summary["valid_test_n"],
        "Smell Density": summary["smell_density"],
        "Smell-free Tests": summary["smell_free_tests_display"],
    }


def ensure_detector(detector_jar: Path, download: bool) -> dict[str, Any]:
    detector = detector_jar.resolve()
    if not detector.is_file():
        if not download:
            raise FileNotFoundError(
                f"Không tìm thấy tsDetect: {detector}\n"
                "Chạy lại với --download-tools --setup-only để tải bản phát hành chính thức."
            )
        detector.parent.mkdir(parents=True, exist_ok=True)
        temporary = detector.with_suffix(detector.suffix + ".part")
        try:
            urllib.request.urlretrieve(TSDETECT_URL, temporary)
            actual = sha256_file(temporary)
            if actual != TSDETECT_SHA256:
                raise RuntimeError(f"tsDetect SHA256 mismatch: expected={TSDETECT_SHA256}, actual={actual}")
            temporary.replace(detector)
        finally:
            temporary.unlink(missing_ok=True)
    actual = sha256_file(detector)
    if actual != TSDETECT_SHA256:
        raise RuntimeError(
            f"tsDetect không đúng bản v{TSDETECT_VERSION}: expected={TSDETECT_SHA256}, actual={actual}"
        )
    return {
        "name": "tsDetect/TestSmellDetector",
        "version": TSDETECT_VERSION,
        "url": TSDETECT_URL,
        "path": str(detector),
        "sha256": actual,
        "bytes": detector.stat().st_size,
    }


def default_smell_java_home(root: Path) -> str:
    configured = os.environ.get("JAVA_11_HOME", "").strip()
    candidates = [
        Path(configured) if configured else Path("__missing__"),
        root / "Java-version" / "java-11" / "Contents" / "Home",
        root / "Java-version" / "java-11",
    ]
    for candidate in candidates:
        executable = candidate / "bin" / ("java.exe" if os.name == "nt" else "java")
        if executable.is_file():
            return str(candidate.resolve())
    return ""
