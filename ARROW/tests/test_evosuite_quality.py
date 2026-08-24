from __future__ import annotations

import csv

from src.evosuite_quality import (
    prepare_instrumentable_tests,
    read_jacoco_counters,
    read_pitest_counters,
    read_saved_classpath,
    summarize_table_iii,
)


def test_prepare_instrumentable_tests_preserves_body_and_removes_evorunner(tmp_path):
    source = tmp_path / "source" / "demo" / "Foo_ESTest.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        """package demo;
import org.evosuite.runtime.EvoRunner;
import org.junit.runner.RunWith;
@RunWith(EvoRunner.class) @EvoRunnerParameters(separateClassLoader = true)
public class Foo_ESTest { int answer() { return 42; } }
""",
        encoding="utf-8",
    )

    files, patched = prepare_instrumentable_tests(tmp_path / "source", tmp_path / "copy")

    assert patched == 2
    assert len(files) == 2
    copied = files[0].read_text(encoding="utf-8")
    assert "@RunWith(EvoRunner.class)" not in copied
    assert "@RunWith(arrow.quality.ArrowEvoRunner.class)" in copied
    assert "separateClassLoader = false" in copied
    assert "return 42" in copied
    assert source.read_text(encoding="utf-8").count("separateClassLoader = true") == 1


def test_read_saved_classpath_keeps_existing_jars_and_drops_stale_workspace(tmp_path):
    dependency = tmp_path / "cache" / "dependency.jar"
    dependency.parent.mkdir()
    dependency.write_bytes(b"jar")
    classpath_file = tmp_path / "maven_classpath.txt"
    classpath_file.write_text(
        str(tmp_path / "deleted-workspace" / "target" / "classes")
        + ":"
        + str(dependency),
        encoding="utf-8",
    )

    assert read_saved_classpath(classpath_file) == [dependency.resolve()]


def test_read_jacoco_counters_aggregates_inner_classes(tmp_path):
    report = tmp_path / "jacoco.csv"
    with report.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "GROUP", "PACKAGE", "CLASS",
                "INSTRUCTION_MISSED", "INSTRUCTION_COVERED",
                "BRANCH_MISSED", "BRANCH_COVERED",
                "LINE_MISSED", "LINE_COVERED",
                "COMPLEXITY_MISSED", "COMPLEXITY_COVERED",
                "METHOD_MISSED", "METHOD_COVERED",
            ]
        )
        writer.writerow(["g", "demo", "Foo", 2, 8, 1, 3, 1, 4, 0, 0, 1, 4])
        writer.writerow(["g", "demo", "Foo$Inner", 0, 10, 0, 2, 0, 5, 0, 0, 0, 5])

    result = read_jacoco_counters(report, "Foo")

    assert result["instruction_covered"] == 18
    assert result["coverage_instruction"] == 90.0
    assert result["coverage_branch"] == 83.33
    assert result["coverage_line"] == 90.0
    assert result["coverage_method"] == 90.0


def test_read_pitest_counters_supports_headerless_117_csv(tmp_path):
    report = tmp_path / "mutations.csv"
    report.write_text(
        "\n".join(
            [
                "Foo.java,demo.Foo,mutator,a,10,KILLED,demo.Foo_ESTest.test0",
                "Foo.java,demo.Foo,mutator,b,11,SURVIVED,none",
                "Foo.java,demo.Foo,mutator,c,12,NO_COVERAGE,none",
            ]
        ),
        encoding="utf-8",
    )

    result = read_pitest_counters(report, "Foo")

    assert result["mutations_total"] == 3
    assert result["mutations_killed"] == 1
    assert result["mutation_score"] == 33.33


def test_table_iii_summary_uses_end_to_end_validity_and_weighted_counters():
    sources = [
        {"baseline_eligible": True, "valid": True},
        {"baseline_eligible": True, "valid": True},
        {"baseline_eligible": False, "valid": False},
        {"baseline_eligible": False, "valid": False},
    ]
    quality = [
        {
            "quality_status": "COMPLETE", "coverage_complete": True, "mutation_complete": True,
            "instruction_covered": 9, "instruction_missed": 1,
            "line_covered": 8, "line_missed": 2, "branch_covered": 3, "branch_missed": 1,
            "method_covered": 4, "method_missed": 1, "mutations_total": 4, "mutations_killed": 1,
            "coverage_instruction": 90, "coverage_line": 80, "coverage_branch": 75,
            "coverage_method": 80, "mutation_score": 25,
        },
        {
            "quality_status": "COMPLETE", "coverage_complete": True, "mutation_complete": True,
            "instruction_covered": 1, "instruction_missed": 9,
            "line_covered": 2, "line_missed": 8, "branch_covered": 1, "branch_missed": 3,
            "method_covered": 1, "method_missed": 4, "mutations_total": 6, "mutations_killed": 3,
            "coverage_instruction": 10, "coverage_line": 20, "coverage_branch": 25,
            "coverage_method": 20, "mutation_score": 50,
        },
    ]

    summary = summarize_table_iii(sources, quality)

    assert summary["valid_rate_end_to_end_pct"] == 50.0
    assert summary["valid_rate_conditional_pct"] == 100.0
    assert summary["IC_weighted_pct"] == 50.0
    assert summary["MS_weighted_pct"] == 40.0
    assert summary["MS_macro_mean_pct"] == 37.5
    assert summary["table_iii_ready"] is True
