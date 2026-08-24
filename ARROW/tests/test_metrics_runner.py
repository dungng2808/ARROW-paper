from __future__ import annotations

from src.build_runner import BuildContext
from src.metrics_runner import MetricsResult, _metric_error, _pom_declares_jacoco, _read_jacoco_csv, _read_pitest_csv, _read_pitest_summary, _run_jacoco, run_maven_metrics
from src.metrics_runner import _find_test_smell_detector, _patch_pom_for_pitest_junit5, _patch_pom_for_pitest_testng, _read_smell_csv, _run_pitest, _run_pitest_dependency_prepare, _strip_repo_prefix
from src.models import VerificationResult


def test_read_jacoco_csv_extracts_focal_class_percentages(tmp_path):
    report = tmp_path / "jacoco.csv"
    report.write_text(
        "\n".join(
            [
                "GROUP,PACKAGE,CLASS,INSTRUCTION_MISSED,INSTRUCTION_COVERED,BRANCH_MISSED,BRANCH_COVERED,LINE_MISSED,LINE_COVERED,COMPLEXITY_MISSED,COMPLEXITY_COVERED,METHOD_MISSED,METHOD_COVERED",
                "graph,org/gephi/graph/impl,EdgeTypeStore,0,10,1,3,2,8,0,1,1,4",
            ]
        ),
        encoding="utf-8",
    )
    result = MetricsResult()

    _read_jacoco_csv(report, "EdgeTypeStore", result)

    assert result.coverage_instruction == "100.00"
    assert result.coverage_branch == "75.00"
    assert result.coverage_line == "80.00"
    assert result.coverage_method == "80.00"


def test_read_pitest_csv_extracts_mutation_score_from_headered_csv(tmp_path):
    report = tmp_path / "mutations.csv"
    report.write_text(
        "\n".join(
            [
                "CLASS,MUTATOR,METHOD,LINE,DESCRIPTION,KILLED_BY,STATUS,TESTS",
                "org.gephi.graph.impl.EdgeTypeStore,mutator,a,1,desc,test,KILLED,1",
                "org.gephi.graph.impl.EdgeTypeStore,mutator,b,2,desc,,SURVIVED,1",
                "org.gephi.graph.impl.OtherStore,mutator,c,3,desc,test,KILLED,1",
            ]
        ),
        encoding="utf-8",
    )
    result = MetricsResult()

    _read_pitest_csv(report, "EdgeTypeStore", result)

    assert result.mutations_total == "2"
    assert result.mutations_killed == "1"
    assert result.mutations_survived == "1"
    assert result.mutation_score == "50.00"


def test_read_pitest_csv_supports_headerless_117_layout(tmp_path):
    report = tmp_path / "mutations.csv"
    report.write_text(
        "\n".join(
            [
                "Foo.java,demo.Foo,mutator,a,10,KILLED,demo.Foo_ESTest.test0",
                "Foo.java,demo.Foo,mutator,b,11,NO_COVERAGE,none",
            ]
        ),
        encoding="utf-8",
    )
    result = MetricsResult()

    _read_pitest_csv(report, "Foo", result)

    assert result.mutations_total == "2"
    assert result.mutations_killed == "1"
    assert result.mutation_score == "50.00"


def test_jacoco_prepare_agent_is_skipped_when_pom_already_declares_jacoco(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<artifactId>jacoco-maven-plugin</artifactId>", encoding="utf-8")
    ctx = BuildContext(repo, repo, "maven", "GeneratedTest", "demo.GeneratedTest")
    captured = {}

    def fake_run_command(ctx, command, cwd, tool_name, target_only):
        captured["command"] = command
        return type("Result", (), {"exit_code": 0})()

    monkeypatch.setattr("src.metrics_runner.run_command", fake_run_command)

    assert _pom_declares_jacoco(ctx) is True
    _run_jacoco(ctx)

    assert "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent" not in captured["command"]
    assert "org.jacoco:jacoco-maven-plugin:0.8.12:report" in captured["command"]


def test_jacoco_prepare_agent_can_be_forced_when_declared_plugin_does_not_emit_csv(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<artifactId>jacoco-maven-plugin</artifactId>", encoding="utf-8")
    ctx = BuildContext(repo, repo, "maven", "GeneratedTest", "demo.GeneratedTest")
    captured = {}

    def fake_run_command(ctx, command, cwd, tool_name, target_only):
        captured["command"] = command
        return type("Result", (), {"exit_code": 0})()

    monkeypatch.setattr("src.metrics_runner.run_command", fake_run_command)

    _run_jacoco(ctx, force_prepare_agent=True)

    assert "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent" in captured["command"]
    assert "org.jacoco:jacoco-maven-plugin:0.8.12:report" in captured["command"]


def test_run_maven_metrics_retries_jacoco_with_prepare_agent_when_csv_is_missing(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<artifactId>jacoco-maven-plugin</artifactId>", encoding="utf-8")
    ctx = BuildContext(repo, repo, "maven", "GeneratedTest", "demo.GeneratedTest")
    commands = []

    def fake_run_command(ctx, command, cwd, tool_name, target_only):
        commands.append(command)
        if len(commands) == 2:
            coverage_csv = repo / "target" / "site" / "jacoco" / "jacoco.csv"
            coverage_csv.parent.mkdir(parents=True)
            coverage_csv.write_text(
                "\n".join(
                    [
                        "GROUP,PACKAGE,CLASS,INSTRUCTION_MISSED,INSTRUCTION_COVERED,BRANCH_MISSED,BRANCH_COVERED,LINE_MISSED,LINE_COVERED,COMPLEXITY_MISSED,COMPLEXITY_COVERED,METHOD_MISSED,METHOD_COVERED",
                        "demo,demo,Focal,0,10,1,3,2,8,0,1,1,4",
                    ]
                ),
                encoding="utf-8",
            )
        return VerificationResult(state=None, exit_code=0)

    monkeypatch.setattr("src.metrics_runner.run_command", fake_run_command)
    monkeypatch.setattr("src.metrics_runner._run_pitest_dependency_prepare", lambda ctx: None)
    monkeypatch.setattr(
        "src.metrics_runner._run_pitest",
        lambda ctx, focal_class_fqcn, testing_framework: VerificationResult(state=None, exit_code=1, primary_error="pitest skipped in test"),
    )

    result, verifications = run_maven_metrics(ctx, "Focal", "demo.Focal")

    assert "coverage_retry_prepare_agent" in verifications
    assert "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent" not in commands[0]
    assert "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent" in commands[1]
    assert result.coverage_branch == "75.00"
    assert result.coverage_line == "80.00"
    assert result.coverage_method == "80.00"
    assert result.coverage_error == ""


def test_pitest_prepare_installs_multimodule_dependencies(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    module = repo / "solutions"
    module.mkdir(parents=True)
    (repo / "pom.xml").write_text(
        "<project><modules><module>solutions</module></modules></project>",
        encoding="utf-8",
    )
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    ctx = BuildContext(
        repo,
        module,
        "maven",
        "GeneratedTest",
        "demo.GeneratedTest",
        maven_multi_module_strategy="root_with_pl_am",
        maven_use_also_make=True,
    )
    captured = {}

    def fake_run_command(ctx, command, cwd, tool_name, target_only):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["target_only"] = target_only
        return type("Result", (), {"exit_code": 0})()

    monkeypatch.setattr("src.metrics_runner.run_command", fake_run_command)

    _run_pitest_dependency_prepare(ctx)

    assert captured["command"][captured["command"].index("-f") + 1] == str(repo / "pom.xml")
    assert "-pl" in captured["command"]
    assert captured["command"][captured["command"].index("-pl") + 1] == "solutions"
    assert "-am" in captured["command"]
    assert "install" in captured["command"]
    assert "-DskipTests" in captured["command"]
    assert "-Ddocker.skip=true" in captured["command"]
    assert captured["cwd"] == module
    assert captured["target_only"] is False


def test_pitest_runs_against_module_pom_after_prepare(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    module = repo / "solutions"
    module.mkdir(parents=True)
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    ctx = BuildContext(
        repo,
        module,
        "maven",
        "GeneratedTest",
        "demo.GeneratedTest",
        maven_multi_module_strategy="root_with_pl_am",
        maven_use_also_make=True,
    )
    captured = {}

    def fake_run_command(ctx, command, cwd, tool_name, target_only):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["target_only"] = target_only
        return type("Result", (), {"exit_code": 0})()

    monkeypatch.setattr("src.metrics_runner.run_command", fake_run_command)

    _run_pitest(ctx, "demo.Foo", "junit5")

    assert captured["command"][captured["command"].index("-f") + 1] == str(module / "pom.xml")
    assert "-pl" not in captured["command"]
    assert "-am" not in captured["command"]
    assert "org.pitest:pitest-maven:1.17.4:mutationCoverage" in captured["command"]
    assert captured["cwd"] == module
    assert captured["target_only"] is True


def test_metric_error_reports_missing_pitest_testng_plugin():
    verification = VerificationResult(
        state=None,
        primary_error="[info] build failure",
        raw_output="PIT >> WARNING : TestNG is on the classpath but the pitest TestNG plugin is not installed.",
    )

    assert _metric_error(verification, "fallback") == "pitest TestNG plugin is not installed"


def test_read_pitest_summary_extracts_overall_mutation_score():
    result = MetricsResult(mutation_error="focal class EdgeTypeStore not found in pitest csv")

    _read_pitest_summary(">> Generated 96 mutations Killed 7 (7%)", result)

    assert result.mutations_total == "96"
    assert result.mutations_killed == "7"
    assert result.mutation_score == "7.29"
    assert result.mutation_error == ""


def test_patch_pom_for_pitest_testng_adds_plugin_dependency_and_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pom = repo / "pom.xml"
    pom.write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>demo</groupId>
  <artifactId>demo</artifactId>
  <version>1.0</version>
</project>
""",
        encoding="utf-8",
    )
    ctx = BuildContext(repo, repo, "maven", "GeneratedTest", "demo.GeneratedTest")

    patched = _patch_pom_for_pitest_testng(ctx)

    assert patched == pom
    content = pom.read_text(encoding="utf-8")
    assert "<artifactId>pitest-maven</artifactId>" in content
    assert "<artifactId>pitest-testng-plugin</artifactId>" in content
    assert "<testPlugin>testng</testPlugin>" in content


def test_patch_pom_for_pitest_junit5_adds_plugin_dependency(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pom = repo / "pom.xml"
    pom.write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>demo</groupId>
  <artifactId>demo</artifactId>
  <version>1.0</version>
</project>
""",
        encoding="utf-8",
    )
    ctx = BuildContext(repo, repo, "maven", "GeneratedTest", "demo.GeneratedTest")

    patched = _patch_pom_for_pitest_junit5(ctx)

    assert patched == pom
    content = pom.read_text(encoding="utf-8")
    assert "<artifactId>pitest-maven</artifactId>" in content
    assert "<artifactId>pitest-junit5-plugin</artifactId>" in content
    assert "<version>1.2.2</version>" in content
    assert "<testPlugin>" not in content


def test_read_smell_csv_maps_tsdetect_columns_to_paper_columns(tmp_path):
    report = tmp_path / "Output_TestSmellDetection.csv"
    report.write_text(
        "\n".join(
            [
                "App,TestClass,NumberOfMethods,Assertion Roulette,Exception Catching Throwing,Magic Number Test",
                "demo,FooTest,3,1,2,4",
            ]
        ),
        encoding="utf-8",
    )
    result = MetricsResult()

    _read_smell_csv(report, result)

    assert result.smell_values["Assertion Roulette"] == "1"
    assert result.smell_values["Exception Handling"] == "2"
    assert result.smell_values["Magic Number Test"] == "4"
    assert result.test_smell_total == "7"
    assert result.smell_error == ""


def test_find_test_smell_detector_from_arrow_tree(tmp_path):
    root = tmp_path / "workspace"
    arrow = root / "ARROW"
    start = arrow / "runs" / "project" / "sample" / "agent" / "prompt" / "workspace"
    start.mkdir(parents=True)

    jar = _find_test_smell_detector(start)

    assert jar == root / "classes2test" / "AgoneTest" / "TestSmellDetector.jar"


def test_strip_repo_prefix_resolves_dataset_path_with_extra_leading_segment(tmp_path):
    focal = tmp_path / "src" / "main" / "java" / "demo" / "Foo.java"
    focal.parent.mkdir(parents=True)
    focal.write_text("class Foo {}", encoding="utf-8")

    assert _strip_repo_prefix(tmp_path, "store/src/main/java/demo/Foo.java") == focal
