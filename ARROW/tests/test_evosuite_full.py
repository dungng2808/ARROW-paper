from __future__ import annotations

import json
from argparse import Namespace

import pytest

import src.evosuite_full as full_module
from run_evosuite_full import merge_runs
from src.evosuite_full import (
    partition_manifest_rows,
    run_full_sample,
    strict_smell_values,
    summarize_full_run,
    table_iii_row,
)
from src.metrics_runner import SMELL_COLUMNS


def _manifest_rows(count: int) -> list[dict[str, str]]:
    return [
        {"rank": str(index + 1), "project_id": str(index + 1), "sample_file": f"{index + 1}_0.json"}
        for index in range(count)
    ]


def test_partition_manifest_rows_splits_200_evenly_across_five_machines():
    rows = _manifest_rows(200)
    shards = [partition_manifest_rows(rows, 5, index) for index in range(5)]

    assert [len(shard) for shard in shards] == [40, 40, 40, 40, 40]
    assert {row["project_id"] for shard in shards for row in shard} == {
        row["project_id"] for row in rows
    }


def test_strict_smell_values_accepts_alias_and_requires_every_entity():
    raw = {entity: "0" for entity in SMELL_COLUMNS}
    raw.pop("Exception Handling")
    raw["Exception Catching Throwing"] = "2"

    result = strict_smell_values(raw)

    assert len(result) == 21
    assert result["Exception Handling"] == 2

    raw.pop("Magic Number Test")
    with pytest.raises(ValueError, match="Magic Number Test"):
        strict_smell_values(raw)


def test_strict_smell_values_rejects_non_numeric_instead_of_fabricating_zero():
    raw = {entity: "0" for entity in SMELL_COLUMNS}
    raw["Lazy Test"] = ""

    with pytest.raises(ValueError, match="Lazy Test"):
        strict_smell_values(raw)


def test_run_full_sample_runs_all_metrics_then_removes_repo(monkeypatch, tmp_path):
    root = tmp_path / "ARROW"
    cached_repo = root / "repos" / "p1"
    cached_repo.mkdir(parents=True)
    order: list[str] = []
    generation = {
        "run_id": "r",
        "manifest_rank": "1",
        "project_id": "p1",
        "sample_file": "p1_0.json",
        "input_id": "p1_0",
        "seed": 42,
        "status": "VALID",
        "valid": True,
        "baseline_eligible": True,
        "artifact_dir": str(tmp_path / "artifact"),
    }

    def fake_generation(**_kwargs):
        order.append("evosuite")
        assert cached_repo.is_dir()
        return [generation]

    def fake_quality(**_kwargs):
        order.extend(["jacoco", "pit"])
        assert cached_repo.is_dir()
        return {
            "project_id": "p1",
            "sample_file": "p1_0.json",
            "seed": 42,
            "quality_status": "COMPLETE",
            "coverage_complete": True,
            "mutation_complete": True,
        }

    def fake_smell(**_kwargs):
        order.append("tsdetect")
        assert cached_repo.is_dir()
        return {
            "project_id": "p1",
            "sample_file": "p1_0.json",
            "seed": 42,
            "smell_status": "COMPLETE",
            "smell_complete": True,
            "smell_instances": 0,
            "smell_free": True,
            **{entity: 0 for entity in SMELL_COLUMNS},
        }

    monkeypatch.setattr(full_module, "run_evosuite_sample", fake_generation)
    monkeypatch.setattr(full_module, "run_quality_sample", fake_quality)
    monkeypatch.setattr(full_module, "run_smell_sample", fake_smell)

    generations, qualities, smells, full = run_full_sample(
        root=root,
        dataset_dir=tmp_path / "dataset",
        manifest_row={"rank": "1", "project_id": "p1", "sample_file": "p1_0.json"},
        config={"repo": {"repos_dir": "repos"}},
        run_id="r",
        seeds=[42],
        evosuite_tools=None,  # type: ignore[arg-type]
        quality_tools=None,  # type: ignore[arg-type]
        detector_jar=tmp_path / "detector.jar",
        smell_java_home="",
        output_dir=tmp_path / "out",
        search_budget=1,
        generation_timeout=1,
        build_timeout=1,
        test_timeout=1,
        coverage_timeout=1,
        mutation_timeout=1,
        smell_timeout=1,
        memory_mb=128,
        pit_threads=1,
        criterion="BRANCH",
        manual_java_home="",
        keep_workspace=False,
    )

    assert order == ["evosuite", "jacoco", "pit", "tsdetect"]
    assert not cached_repo.exists()
    assert generations[0]["repo_cache_removed"] is True
    assert qualities[0]["repo_cache_removed"] is True
    assert smells[0]["repo_cache_removed"] is True
    assert full[0]["full_status"] == "COMPLETE"


def test_summary_requires_all_metrics_for_every_valid_suite():
    generation = [
        {"project_id": "p1", "sample_file": "p1.json", "seed": 42, "valid": True},
        {"project_id": "p2", "sample_file": "p2.json", "seed": 42, "valid": False},
    ]
    quality = [
        {
            "project_id": "p1",
            "sample_file": "p1.json",
            "seed": 42,
            "coverage_complete": True,
            "mutation_complete": True,
            "instruction_covered": 8,
            "instruction_missed": 2,
            "line_covered": 4,
            "line_missed": 1,
            "branch_covered": 1,
            "branch_missed": 1,
            "method_covered": 2,
            "method_missed": 0,
            "mutations_total": 4,
            "mutations_killed": 3,
            "coverage_instruction": 80,
            "coverage_line": 80,
            "coverage_branch": 50,
            "coverage_method": 100,
            "mutation_score": 75,
        }
    ]
    smell = [
        {
            "project_id": "p1",
            "sample_file": "p1.json",
            "seed": 42,
            "smell_complete": True,
            "smell_instances": 2,
            "smell_free": False,
            **{entity: (2 if entity == "Assertion Roulette" else 0) for entity in SMELL_COLUMNS},
        }
    ]
    full = [
        {
            "project_id": "p1",
            "sample_file": "p1.json",
            "seed": 42,
            "full_metrics_complete": True,
        },
        {
            "project_id": "p2",
            "sample_file": "p2.json",
            "seed": 42,
            "full_metrics_complete": False,
        },
    ]

    summary = summarize_full_run(generation, quality, smell, full, expected_records=2)

    assert summary["table_iii_ready"] is True
    assert summary["full_quality_ready"] is True
    assert summary["smell_density"] == 2.0
    assert summary["MS_weighted_pct"] == 75.0
    assert table_iii_row(summary) == {
        "Model": "EvoSuite",
        "Prompt": "Search-based baseline",
        "Valid tests": 1,
        "Smell Density": 2.0,
        "Smell-free Tests": "0 (0.00%)",
    }

    summary = summarize_full_run(generation, quality, [], full, expected_records=2)
    assert summary["table_iii_ready"] is False
    assert summary["full_quality_ready"] is False
    assert table_iii_row(summary)["Smell Density"] == ""

    inconsistent_smell = [dict(smell[0], smell_instances=999)]
    summary = summarize_full_run(generation, quality, inconsistent_smell, full, expected_records=2)
    assert summary["smell_complete_n"] == 0
    assert summary["table_iii_ready"] is False


def test_merge_runs_accepts_exact_five_shards_and_writes_ten_records(tmp_path):
    manifest_rows = _manifest_rows(10)
    manifest_hash = "locked-hash"
    run_dirs = []
    for index in range(5):
        run_dir = tmp_path / f"shard-{index}"
        run_dir.mkdir()
        run_dirs.append(run_dir)
        (run_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "manifest_sha256": manifest_hash,
                    "shard_count": 5,
                    "shard_index": index,
                    "seeds": [42],
                }
            ),
            encoding="utf-8",
        )
        rows = []
        for row in partition_manifest_rows(manifest_rows, 5, index):
            rows.append(
                {
                    "manifest_rank": row["rank"],
                    "project_id": row["project_id"],
                    "sample_file": row["sample_file"],
                    "seed": 42,
                    "valid": False,
                    "status": "GENERATION_FAILED",
                    "full_status": "GENERATION_INVALID",
                    "full_metrics_complete": False,
                    "quality_status": "NOT_APPLICABLE_INVALID",
                    "smell_status": "NOT_APPLICABLE_INVALID",
                }
            )
        payload = "".join(json.dumps(row) + "\n" for row in rows)
        for filename in (
            "evosuite_records.jsonl",
            "quality_records.jsonl",
            "smell_records.jsonl",
            "evosuite_full_quality_records.jsonl",
        ):
            (run_dir / filename).write_text(payload, encoding="utf-8")

    output_dir = tmp_path / "merged"
    args = Namespace(
        merge_run_dir=run_dirs,
        shard_count=5,
        run_id="merged",
        output_dir=output_dir,
        compact_dir=None,
        manifest=tmp_path / "manifest.csv",
    )

    result = merge_runs(args, manifest_rows, manifest_hash)

    assert result == 2  # No valid EvoSuite suite means the quality gate stays false.
    merged = (output_dir / "evosuite_records.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(merged) == 10
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["mode"] == "merge"
    assert provenance["shard_count"] == 5
