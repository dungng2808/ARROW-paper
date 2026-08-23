from pathlib import Path

import pytest

from src.evosuite_runner import (
    _dedupe_existing,
    generated_test_classes,
    gradle_init_script_text,
    parse_gradle_classpath,
    read_manifest,
    summarize,
)


def test_read_manifest_preserves_one_sample_per_repository(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "rank,project_id,sample_file\n1,10,10_0.json\n2,20,path/20_0.json\n",
        encoding="utf-8",
    )

    assert read_manifest(manifest) == [
        {"rank": "1", "project_id": "10", "sample_file": "10_0.json"},
        {"rank": "2", "project_id": "20", "sample_file": "20_0.json"},
    ]


def test_read_manifest_rejects_second_sample_from_same_repository(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "rank,project_id,sample_file\n1,10,10_0.json\n2,10,10_1.json\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="one-sample-per-repository"):
        read_manifest(manifest)


def test_parse_gradle_classpath_uses_last_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.evosuite_runner.os.pathsep", ":")
    output = "noise\nARROW_CLASSPATH=/first.jar\nARROW_CLASSPATH=/a.jar:/b.jar\n"

    assert parse_gradle_classpath(output) == [Path("/a.jar"), Path("/b.jar")]
    assert "arrowPrintClasspath" in gradle_init_script_text()


def test_generated_test_classes_excludes_scaffolding(tmp_path: Path) -> None:
    test = tmp_path / "Thing_ESTest.java"
    scaffolding = tmp_path / "Thing_ESTest_scaffolding.java"
    test.write_text("package example; public class Thing_ESTest {}", encoding="utf-8")
    scaffolding.write_text("package example; public class Thing_ESTest_scaffolding {}", encoding="utf-8")

    assert generated_test_classes([test, scaffolding]) == ["example.Thing_ESTest"]


def test_summary_excludes_baseline_invalid_from_rates() -> None:
    rows = [
        {"status": "VALID", "baseline_eligible": True, "compilation": True, "execution_success": True, "test_passed": True, "valid": True},
        {"status": "COMPILE_FAILED", "baseline_eligible": True, "compilation": False, "execution_success": False, "test_passed": False, "valid": False},
        {"status": "BASELINE_FAILED", "baseline_eligible": False, "compilation": False, "execution_success": False, "test_passed": False, "valid": False},
    ]

    summary = summarize(rows)

    assert summary["baseline_valid_evaluable_n"] == 2
    assert summary["baseline_invalid_excluded_n"] == 1
    assert summary["CSR_pct"] == 50.0
    assert summary["valid_rate_pct"] == 50.0
    assert summary["end_to_end_valid_rate_pct"] == 33.33


def test_classpath_filter_rejects_pom_and_keeps_jar_and_directory(tmp_path: Path) -> None:
    classes = tmp_path / "classes"
    classes.mkdir()
    jar = tmp_path / "dependency.jar"
    pom = tmp_path / "dependency.pom"
    jar.write_bytes(b"jar")
    pom.write_text("<project/>", encoding="utf-8")

    assert _dedupe_existing([pom, classes, jar, jar]) == [classes.resolve(), jar.resolve()]
