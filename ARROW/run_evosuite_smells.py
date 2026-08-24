#!/usr/bin/env python3
"""Run tsDetect on all VALID EvoSuite tests and export paper-ready metrics."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.evosuite_runner import load_jsonl, prepare_repository, resolve_focal_path, sha256_file
from src.input_selector import load_sample
from src.metrics_runner import SMELL_COLUMNS, _smell_value


ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Đo Smell Density và Smell-free Tests cho EvoSuite VALID bằng tsDetect.")
    parser.add_argument(
        "--evosuite-run",
        type=Path,
        default=ROOT / "runs" / "evosuite" / "evosuite-rq3-seed42",
    )
    parser.add_argument(
        "--detector-jar",
        type=Path,
        default=ROOT.parent / "classes2test" / "AgoneTest" / "TestSmellDetector.jar",
    )
    parser.add_argument(
        "--java-home",
        type=Path,
        default=ROOT / "Java-version" / "java-11" / "Contents" / "Home",
    )
    parser.add_argument("--timeout", type=int, default=900)
    return parser.parse_args()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
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


def _test_source(artifact_dir: Path) -> Path:
    candidates = [
        path
        for path in (artifact_dir / "evosuite-tests").rglob("*_ESTest.java")
        if not path.stem.endswith("_scaffolding")
    ]
    if len(candidates) != 1:
        raise ValueError(f"Expected exactly one EvoSuite test source in {artifact_dir}; found {len(candidates)}")
    return candidates[0]


def _read_detector_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    args = parse_args()
    run_dir = args.evosuite_run.resolve()
    detector = args.detector_jar.resolve()
    java = (args.java_home.resolve() / "bin" / "java")
    if not detector.is_file():
        raise FileNotFoundError(f"Không tìm thấy tsDetect: {detector}")
    if not java.is_file():
        raise FileNotFoundError(f"Không tìm thấy Java: {java}")
    source_records = load_jsonl(run_dir / "evosuite_records.jsonl")
    valid = sorted(
        (row for row in source_records if row.get("valid") is True),
        key=lambda row: (int(row.get("manifest_rank") or 0), int(row.get("seed") or 0)),
    )
    if not valid:
        raise ValueError("Không có EvoSuite record VALID")

    output_dir = run_dir / "evosuite_smells"
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_root = ROOT / "repos"
    dataset_dir = ROOT.parent / "classes2test" / "dataset"
    prepared: list[dict[str, Any]] = []
    for row in valid:
        sample_path = dataset_dir / str(row["project_id"]) / str(row["sample_file"])
        sample = load_sample(sample_path, dataset_dir)
        cached_repo = cache_root / sample.project_id
        repository = prepare_repository(sample, cached_repo, cache_root, True)
        focal_source = resolve_focal_path(repository, sample.focal_class_path)
        test_source = _test_source(Path(str(row["artifact_dir"])))
        if not focal_source.is_file():
            raise FileNotFoundError(f"Không tìm thấy focal source: {focal_source}")
        prepared.append(
            {
                "source": row,
                "detector_id": f"{row['project_id']}:{row['input_id']}:seed-{row['seed']}",
                "test_source": test_source.resolve(),
                "focal_source": focal_source.resolve(),
            }
        )

    input_csv = output_dir / "pathToInputFile.csv"
    with input_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for item in prepared:
            writer.writerow([item["detector_id"], item["test_source"], item["focal_source"]])

    for stale in output_dir.glob("Output_TestSmellDetection*.csv"):
        stale.unlink()
    command = [str(java), "-jar", str(detector), str(input_csv)]
    completed = subprocess.run(
        command,
        cwd=output_dir,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=args.timeout,
    )
    (output_dir / "tsdetect.log").write_text(
        (completed.stdout or "") + ("\n" if completed.stdout and completed.stderr else "") + (completed.stderr or ""),
        encoding="utf-8",
    )
    (output_dir / "tsdetect_command.json").write_text(json.dumps(command, indent=2), encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"tsDetect failed with exit code {completed.returncode}; xem {output_dir / 'tsdetect.log'}")
    outputs = sorted(output_dir.glob("Output_TestSmellDetection*.csv"), key=lambda path: path.stat().st_mtime)
    if not outputs:
        raise FileNotFoundError("tsDetect không tạo Output_TestSmellDetection*.csv")
    detected = _read_detector_rows(outputs[-1])
    if len(detected) != len(prepared):
        raise ValueError(f"tsDetect returned {len(detected)} rows for {len(prepared)} valid tests")
    detected_by_id = {str(row.get("App") or ""): row for row in detected}
    expected_ids = {str(item["detector_id"]) for item in prepared}
    if set(detected_by_id) != expected_ids:
        missing = sorted(expected_ids - set(detected_by_id))
        extra = sorted(set(detected_by_id) - expected_ids)
        raise ValueError(f"tsDetect ID mismatch; missing={missing}, extra={extra}")

    records: list[dict[str, Any]] = []
    for item in prepared:
        raw = detected_by_id[str(item["detector_id"])]
        values = {smell: _smell_value(raw, smell) for smell in SMELL_COLUMNS}
        total = sum(int(float(value or 0)) for value in values.values())
        source = item["source"]
        records.append(
            {
                "manifest_rank": source.get("manifest_rank", ""),
                "project_id": source.get("project_id", ""),
                "input_id": source.get("input_id", ""),
                "seed": source.get("seed", ""),
                "test_source": str(item["test_source"]),
                "focal_source": str(item["focal_source"]),
                "smell_instances": total,
                "smell_free": total == 0,
                **values,
            }
        )
    total_instances = sum(int(row["smell_instances"]) for row in records)
    smell_free_n = sum(row["smell_free"] is True for row in records)
    n_valid = len(records)
    summary = {
        "model": "EvoSuite",
        "prompt": "Search-based baseline",
        "valid_tests_n": n_valid,
        "smell_instances_total": total_instances,
        "smell_density": round(total_instances / n_valid, 2),
        "smell_free_tests_n": smell_free_n,
        "smell_free_tests_pct": round(smell_free_n * 100 / n_valid, 2),
        "smell_free_tests_display": f"{smell_free_n} ({smell_free_n * 100 / n_valid:.2f}%)",
        "smell_free_yield_pct": round(smell_free_n * 100 / len(source_records), 2),
        "records_total": len(source_records),
        "smell_complete_n": n_valid,
        "smell_ready": n_valid == len(valid),
        "detector": "tsDetect/TestSmellDetector.jar",
        "detector_sha256": sha256_file(detector),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_csv(output_dir / "evosuite_smell_records.csv", records)
    (output_dir / "evosuite_smell_records.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8"
    )
    write_csv(output_dir / "evosuite_smell_summary.csv", [summary])
    (output_dir / "evosuite_smell_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Kết quả: {output_dir / 'evosuite_smell_summary.csv'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Đã dừng tsDetect.", file=sys.stderr)
        raise SystemExit(130)
