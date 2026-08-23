from __future__ import annotations

import json
from pathlib import Path

from select_clean_samples import (
    _probe_source,
    analyze_metadata_testability,
    analyze_semantic_testability,
    build_candidates,
    deterministic_sample_file,
    export_candidate_dataset,
    manifest_rows,
    merge_shard_audits,
    select_final_and_reserve,
    shard_candidates,
    write_csv,
    write_partitioned_manifests,
)
from src.models import SampleInput


def _sample(project: Path, name: str) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / name).write_text(
        json.dumps(
            {
                "repository": {"url": f"https://example.test/{project.name}"},
                "focal_class": {"identifier": "Thing", "file": "src/main/java/demo/Thing.java"},
                "test_class": {"identifier": "ThingTest", "file": "src/test/java/demo/ThingTest.java"},
            }
        ),
        encoding="utf-8",
    )


def test_candidate_selection_is_deterministic_and_one_per_project(tmp_path: Path) -> None:
    for project_id in ("10", "20", "30", "40"):
        _sample(tmp_path / project_id, "a.json")
        _sample(tmp_path / project_id, "b.json")

    first = build_candidates(tmp_path, seed=42, candidate_count=3)
    second = build_candidates(tmp_path, seed=42, candidate_count=3)

    assert first == second
    assert len({row["project_id"] for row in first}) == 3
    assert [row["candidate_rank"] for row in first] == ["1", "2", "3"]


def test_sample_choice_is_stable_for_project_and_seed(tmp_path: Path) -> None:
    project = tmp_path / "100"
    for name in ("100_0.json", "100_1.json", "100_2.json"):
        _sample(project, name)

    assert deterministic_sample_file(project, 7) == deterministic_sample_file(project, 7)


def test_export_candidate_dataset_preserves_layout_and_hash(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _sample(source / "100", "100_0.json")
    rows = build_candidates(source, seed=42, candidate_count=1)
    destination = tmp_path / "exported"

    provenance = export_candidate_dataset(
        rows,
        source_dataset=source,
        destination_dataset=destination,
    )

    assert (destination / "100" / "100_0.json").is_file()
    assert provenance["sample_count"] == 1
    assert provenance["total_bytes"] > 0


def test_final_and_reserve_follow_candidate_rank_not_completion_order() -> None:
    rows = [
        {"candidate_rank": "4", "project_id": "p4", "sample_file": "4.json", "eligibility_status": "ELIGIBLE"},
        {"candidate_rank": "1", "project_id": "p1", "sample_file": "1.json", "eligibility_status": "EXCLUDED"},
        {"candidate_rank": "3", "project_id": "p3", "sample_file": "3.json", "eligibility_status": "ELIGIBLE"},
        {"candidate_rank": "2", "project_id": "p2", "sample_file": "2.json", "eligibility_status": "ELIGIBLE"},
    ]

    final, reserve, eligible = select_final_and_reserve(rows, target=2, reserve=1)

    assert [row["project_id"] for row in final] == ["p2", "p3"]
    assert [row["project_id"] for row in reserve] == ["p4"]
    assert [row["rank"] for row in manifest_rows(eligible)] == [1, 2, 3]


def test_probe_source_supports_known_frameworks() -> None:
    assert "org.junit.Test" in _probe_source("demo", "Probe", "junit4")
    assert "org.junit.jupiter.api.Test" in _probe_source("demo", "Probe", "junit5")
    assert "org.testng.annotations.Test" in _probe_source("", "Probe", "testng")


def _semantic_sample(
    *,
    class_name: str = "Calculator",
    method_name: str = "calculate",
    body: str = "{ return value > 0 ? value * 2 : 0; }",
    modifiers: str = "public",
    return_type: str = "int",
    parameters: str = "(int value)",
) -> SampleInput:
    return SampleInput(
        project_id="1",
        sample_file=Path("1.json"),
        repository_url="https://example.test/repo",
        focal_class_name=class_name,
        focal_class_path=f"module/src/main/java/demo/{class_name}.java",
        test_class_name=f"{class_name}Test",
        test_class_path=f"module/src/test/java/demo/{class_name}Test.java",
        raw={
            "focal_class": {"methods": []},
            "focal_method": {
                "identifier": method_name,
                "body": body,
                "modifiers": modifiers,
                "return": return_type,
                "parameters": parameters,
            },
        },
    )


def _semantic(sample: SampleInput, source: str):
    return analyze_semantic_testability(
        sample,
        source,
        min_score=7,
        include_non_concrete=False,
        allow_external_risk=False,
        allow_nonstandard_source_layout=False,
    )


def test_semantic_filter_accepts_concrete_deterministic_logic() -> None:
    sample = _semantic_sample()
    source = "package demo; public class Calculator { public int calculate(int value) { return value > 0 ? value * 2 : 0; } }"

    result = _semantic(sample, source)

    assert result["semantic_status"] == "ELIGIBLE"
    assert result["class_kind"] == "class"
    assert result["has_control_flow"] is True
    assert result["testability_score"] >= 7


def test_semantic_filter_rejects_interface_and_trivial_getter() -> None:
    interface_sample = _semantic_sample(class_name="CalculatorApi")
    interface = _semantic(interface_sample, "package demo; public interface CalculatorApi { int calculate(int value); }")
    getter_sample = _semantic_sample(method_name="getValue", body="{ return value; }", parameters="()")
    getter = _semantic(getter_sample, "package demo; public class Calculator { private int value; public int getValue() { return value; } }")

    assert interface["semantic_status"] == "EXCLUDED"
    assert "non_concrete:interface" in interface["semantic_exclusion_reason"]
    assert getter["semantic_status"] == "EXCLUDED"
    assert "trivial_focal_method" in getter["semantic_exclusion_reason"]


def test_metadata_filter_rejects_trivial_method_before_clone() -> None:
    sample = _semantic_sample(method_name="getValue", body="{ return value; }", parameters="()")

    result = analyze_metadata_testability(sample, allow_nonstandard_source_layout=False)

    assert result["semantic_status"] == "EXCLUDED"
    assert "trivial_focal_method" in result["semantic_exclusion_reason"]

    constant = _semantic_sample(method_name="label", body='{ return "fixed"; }', return_type="String", parameters="()")
    constant_result = analyze_metadata_testability(constant, allow_nonstandard_source_layout=False)
    assert "trivial_focal_method" in constant_result["semantic_exclusion_reason"]


def test_semantic_filter_rejects_repository_external_risk() -> None:
    sample = _semantic_sample(class_name="UserRepository", method_name="find", body="{ return entityManager.find(User.class, id); }", return_type="User", parameters="(int id)")
    source = "package demo; import jakarta.persistence.EntityManager; public class UserRepository { EntityManager entityManager; public User find(int id) { return entityManager.find(User.class, id); } }"

    result = _semantic(sample, source)

    assert result["external_dependency_risk"] is True
    assert result["semantic_status"] == "EXCLUDED"
    assert "external_dependency_risk" in result["semantic_exclusion_reason"]


def test_shard_assignment_is_disjoint_and_complete() -> None:
    rows = [{"candidate_rank": str(rank), "project_id": str(rank), "sample_file": f"{rank}.json"} for rank in range(1, 16)]
    shards = [shard_candidates(rows, 5, index) for index in range(5)]

    flattened = [row["candidate_rank"] for shard in shards for row in shard]

    assert sorted(flattened, key=int) == [str(rank) for rank in range(1, 16)]
    assert all(len(shard) == 3 for shard in shards)


def test_merge_shard_audits_validates_hash_and_partition(tmp_path: Path) -> None:
    for index in range(2):
        directory = tmp_path / f"shard-{index}"
        directory.mkdir()
        (directory / "provenance.json").write_text(
            json.dumps({"run_id": f"run-{index}", "candidate_manifest_sha256": "locked-hash", "shard_count": 2, "shard_index": index}),
            encoding="utf-8",
        )
        rank = index + 1
        write_csv(
            directory / "preflight_audit.csv",
            [{"candidate_rank": str(rank), "project_id": str(rank), "sample_file": f"{rank}.json", "eligibility_status": "ELIGIBLE"}],
        )

    rows, metadata = merge_shard_audits(
        [tmp_path / "shard-0", tmp_path / "shard-1"],
        candidate_manifest_sha256="locked-hash",
    )

    assert [row["candidate_rank"] for row in rows] == ["1", "2"]
    assert metadata["source_shard_indexes"] == [0, 1]


def test_partitioned_final_manifests_follow_qualification_shards(tmp_path: Path) -> None:
    rows = [
        {"candidate_rank": str(rank), "project_id": str(rank), "sample_file": f"{rank}.json", "eligibility_status": "ELIGIBLE"}
        for rank in range(1, 7)
    ]

    write_partitioned_manifests(tmp_path, rows, target=4, reserve=2, shard_count=2)

    shard_zero = (tmp_path / "final_manifest_4_shard_0_of_2.csv").read_text(encoding="utf-8")
    shard_one = (tmp_path / "final_manifest_4_shard_1_of_2.csv").read_text(encoding="utf-8")
    assert ",1,1,1.json," in shard_zero
    assert ",3,3,3.json," in shard_zero
    assert ",2,2,2.json," in shard_one
    assert ",4,4,4.json," in shard_one
