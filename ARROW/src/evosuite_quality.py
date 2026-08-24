"""Measure Table III quality metrics for already-generated EvoSuite tests.

The EvoSuite search coverage saved by :mod:`evosuite_runner` is deliberately
not reused here.  This module executes the saved JUnit bytecode under JaCoCo
and runs PIT against the same focal class, for both Maven and Gradle projects.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from .build_runner import BuildContext, verify_baseline
from .evosuite_runner import (
    CommandResult,
    EvoSuiteTools,
    compile_generated_tests,
    find_focal_bytecode,
    generated_java_files,
    generated_test_classes,
    java_environment,
    java_executable,
    load_manifest_sample,
    prepare_repository,
    resolve_focal_path,
    resolve_project_classpath,
    run_process,
    sha256_file,
    utc_now,
)
from .java_resolver import resolve_java_home
from .project_analyzer import analyze_experiment
from .repo_manager import ensure_experiment_workspace, safe_remove_tree


JACOCO_VERSION = "0.8.12"
PITEST_VERSION = "1.17.4"


@dataclass(frozen=True)
class QualityTools:
    jacoco_agent: Path
    jacoco_cli: Path
    pitest_dir: Path

    @property
    def pitest_jars(self) -> list[Path]:
        return sorted(self.pitest_dir.glob("*.jar"))


QUALITY_TOOL_URLS = {
    "jacoco_agent": (
        f"https://repo.maven.apache.org/maven2/org/jacoco/org.jacoco.agent/{JACOCO_VERSION}/"
        f"org.jacoco.agent-{JACOCO_VERSION}-runtime.jar"
    ),
    "jacoco_cli": (
        f"https://repo.maven.apache.org/maven2/org/jacoco/org.jacoco.cli/{JACOCO_VERSION}/"
        f"org.jacoco.cli-{JACOCO_VERSION}-nodeps.jar"
    ),
}

PITEST_ARTIFACTS = {
    f"pitest-command-line-{PITEST_VERSION}.jar": (
        f"https://repo.maven.apache.org/maven2/org/pitest/pitest-command-line/{PITEST_VERSION}/"
        f"pitest-command-line-{PITEST_VERSION}.jar"
    ),
    f"pitest-entry-{PITEST_VERSION}.jar": (
        f"https://repo.maven.apache.org/maven2/org/pitest/pitest-entry/{PITEST_VERSION}/"
        f"pitest-entry-{PITEST_VERSION}.jar"
    ),
    f"pitest-html-report-{PITEST_VERSION}.jar": (
        f"https://repo.maven.apache.org/maven2/org/pitest/pitest-html-report/{PITEST_VERSION}/"
        f"pitest-html-report-{PITEST_VERSION}.jar"
    ),
    f"pitest-{PITEST_VERSION}.jar": (
        f"https://repo.maven.apache.org/maven2/org/pitest/pitest/{PITEST_VERSION}/pitest-{PITEST_VERSION}.jar"
    ),
    "commons-text-1.12.0.jar": (
        "https://repo.maven.apache.org/maven2/org/apache/commons/commons-text/1.12.0/commons-text-1.12.0.jar"
    ),
    "commons-lang3-3.14.0.jar": (
        "https://repo.maven.apache.org/maven2/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar"
    ),
}


def default_quality_tools(tools_dir: Path) -> QualityTools:
    return QualityTools(
        jacoco_agent=tools_dir / f"org.jacoco.agent-{JACOCO_VERSION}-runtime.jar",
        jacoco_cli=tools_dir / f"org.jacoco.cli-{JACOCO_VERSION}-nodeps.jar",
        pitest_dir=tools_dir / "pitest",
    )


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        urllib.request.urlretrieve(url, temporary)
        if temporary.stat().st_size < 1024:
            raise RuntimeError(f"Downloaded file is unexpectedly small: {temporary}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_quality_tools(tools: QualityTools, download: bool) -> dict[str, Any]:
    destinations = {
        "jacoco_agent": tools.jacoco_agent,
        "jacoco_cli": tools.jacoco_cli,
        **{name: tools.pitest_dir / name for name in PITEST_ARTIFACTS},
    }
    urls = {**QUALITY_TOOL_URLS, **PITEST_ARTIFACTS}
    missing = [(name, path) for name, path in destinations.items() if not path.is_file()]
    if missing and not download:
        lines = "\n".join(f"  - {path}" for _, path in missing)
        raise FileNotFoundError(
            "Thiếu JaCoCo/PIT cho Table III:\n"
            f"{lines}\n"
            "Chạy: python3 run_evosuite_quality.py --download-tools --setup-only"
        )
    for name, path in missing:
        _download(urls[name], path)
    return {
        "jacoco_version": JACOCO_VERSION,
        "pitest_version": PITEST_VERSION,
        "files": {
            name: {"path": str(path.resolve()), "sha256": sha256_file(path), "bytes": path.stat().st_size}
            for name, path in destinations.items()
        },
    }


def _dedupe_existing(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if not (path.is_dir() or (path.is_file() and path.suffix.lower() in {".jar", ".zip"})):
            continue
        value = str(path.resolve())
        if value not in seen:
            seen.add(value)
            result.append(Path(value))
    return result


def read_saved_classpath(path: Path) -> list[Path]:
    """Recover dependency jars from the classpath used for EvoSuite generation.

    Target/build directories in this file belonged to the old temporary
    workspace and are ignored when absent. Maven/Gradle cache jars remain valid
    and preserve test/provided dependencies that a runtime-only recomputation
    can omit.
    """
    if not path.is_file():
        return []
    return _dedupe_existing(Path(item) for item in path.read_text(encoding="utf-8", errors="replace").split(os.pathsep))


def _write_command_artifacts(root: Path, name: str, command_result: CommandResult) -> None:
    (root / f"{name}.log").write_text(command_result.output or "", encoding="utf-8")
    (root / f"{name}_command.json").write_text(
        json.dumps(command_result.command, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _focal_source_root(focal_path: Path, fqcn: str) -> Path:
    expected = Path(*fqcn.split(".")).with_suffix(".java")
    try:
        if focal_path.parts[-len(expected.parts) :] == expected.parts:
            root = focal_path
            for _ in expected.parts:
                root = root.parent
            return root
    except (IndexError, ValueError):
        pass
    return focal_path.parent


def _copy_focal_bytecode(bytecode: Path, fqcn: str, destination: Path) -> Path:
    package_parts = fqcn.split(".")[:-1]
    class_name = fqcn.split(".")[-1]
    package_dir = destination.joinpath(*package_parts)
    package_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source in bytecode.parent.glob(f"{class_name}*.class"):
        shutil.copy2(source, package_dir / source.name)
        copied += 1
    if copied == 0:
        raise FileNotFoundError(f"Không tìm thấy bytecode của focal class: {bytecode}")
    return destination


def prepare_instrumentable_tests(test_dir: Path, destination: Path) -> tuple[list[Path], int]:
    """Copy EvoSuite sources and disable its isolated classloader.

    EvoRunner's separate classloader hides the focal class from both JaCoCo and
    PIT. The copied source uses a small EvoRunner subclass that keeps EvoSuite's
    lifecycle but selects the pre-attached agent and shared classloader. The
    generated statements and assertions remain unchanged, and original
    artifacts are never modified.
    """
    if destination.exists():
        safe_remove_tree(destination, destination.parent)
    destination.mkdir(parents=True, exist_ok=True)
    patched = 0
    output: list[Path] = []
    for source in generated_java_files(test_dir):
        relative = source.relative_to(test_dir)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        code = source.read_text(encoding="utf-8", errors="replace")
        updated, replacements = re.subn(
            r"\bseparateClassLoader\s*=\s*true\b",
            "separateClassLoader = false",
            code,
        )
        updated, runner_replacements = re.subn(
            r"@RunWith\s*\(\s*EvoRunner\.class\s*\)\s*",
            "@RunWith(arrow.quality.ArrowEvoRunner.class) ",
            updated,
        )
        target.write_text(updated, encoding="utf-8")
        patched += replacements + runner_replacements
        output.append(target)
    if not output:
        raise FileNotFoundError(f"Không có EvoSuite Java source trong {test_dir}")
    helper = destination / "arrow" / "quality" / "ArrowEvoRunner.java"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text(
        """package arrow.quality;

import org.evosuite.runtime.EvoRunner;
import org.junit.runners.model.InitializationError;

/** Reuses pre-attached JaCoCo/EvoSuite/PIT agents without an isolated loader. */
public final class ArrowEvoRunner extends EvoRunner {
    public ArrowEvoRunner(Class<?> testClass) throws InitializationError {
        super(configure(testClass));
    }

    private static Class<?> configure(Class<?> testClass) {
        EvoRunner.useAgent = false;
        EvoRunner.useClassLoader = false;
        return testClass;
    }
}
""",
        encoding="utf-8",
    )
    output.append(helper)
    return output, patched


COUNTER_PREFIXES = ("INSTRUCTION", "LINE", "BRANCH", "METHOD")


def read_jacoco_counters(path: Path, focal_class_name: str) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    focal_rows = [row for row in rows if str(row.get("CLASS", "")).split("$")[0] == focal_class_name]
    if not focal_rows:
        raise ValueError(f"focal class {focal_class_name} not found in JaCoCo CSV")
    output: dict[str, Any] = {}
    for prefix in COUNTER_PREFIXES:
        covered = sum(int(row.get(f"{prefix}_COVERED") or 0) for row in focal_rows)
        missed = sum(int(row.get(f"{prefix}_MISSED") or 0) for row in focal_rows)
        total = covered + missed
        key = prefix.lower()
        output[f"{key}_covered"] = covered
        output[f"{key}_missed"] = missed
        output[f"coverage_{key}"] = round(covered * 100 / total, 2) if total else ""
    return output


def read_pitest_counters(path: Path, focal_class_name: str) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError("PIT mutations.csv is empty")
    header = [cell.strip().lower() for cell in rows[0]]
    has_header = "status" in header or "result" in header
    data = rows[1:] if has_header else rows
    status_index = header.index("status") if "status" in header else header.index("result") if "result" in header else 5
    # PIT 1.17 command-line CSV is headerless and starts with
    # sourceFile,className,...,status. Older PIT CSVs start with className.
    class_index = header.index("class") if "class" in header else (
        1 if not has_header and len(rows[0]) > 1 and rows[0][0].strip().endswith(".java") else 0
    )
    focal_rows = [
        row
        for row in data
        if len(row) > max(class_index, status_index)
        and row[class_index].split(".")[-1].replace(".java", "").split("$")[0] == focal_class_name
    ]
    if not focal_rows:
        raise ValueError(f"focal class {focal_class_name} not found in PIT CSV")
    statuses: dict[str, int] = {}
    for row in focal_rows:
        status = row[status_index].strip().upper()
        statuses[status] = statuses.get(status, 0) + 1
    total = len(focal_rows)
    killed = statuses.get("KILLED", 0) + statuses.get("TIMED_OUT", 0)
    return {
        "mutations_total": total,
        "mutations_killed": killed,
        "mutations_survived": statuses.get("SURVIVED", 0),
        "mutations_no_coverage": statuses.get("NO_COVERAGE", 0),
        "mutations_timed_out": statuses.get("TIMED_OUT", 0),
        "mutations_error": statuses.get("RUN_ERROR", 0)
        + statuses.get("MEMORY_ERROR", 0)
        + statuses.get("NON_VIABLE", 0),
        "mutation_score": round(killed * 100 / total, 2),
        "mutation_status_counts": statuses,
    }


def _run_jacoco(
    *,
    quality_root: Path,
    java_home: str | None,
    memory_mb: int,
    timeout_seconds: int,
    fqcn: str,
    test_classes: list[str],
    runtime_classpath: list[Path],
    focal_bytecode: Path,
    focal_source: Path,
    tools: QualityTools,
    evosuite_runtime: Path,
) -> tuple[dict[str, Any], str]:
    jacoco_root = quality_root / "jacoco"
    if jacoco_root.exists():
        safe_remove_tree(jacoco_root, quality_root)
    jacoco_root.mkdir(parents=True, exist_ok=True)
    exec_file = jacoco_root / "jacoco.exec"
    csv_file = jacoco_root / "jacoco.csv"
    focal_classes = _copy_focal_bytecode(focal_bytecode, fqcn, jacoco_root / "focal-classes")
    command = [
        java_executable(java_home, "java"),
        f"-Xmx{memory_mb}m",
        f"-javaagent:{tools.jacoco_agent}=destfile={exec_file},append=false,includes={fqcn}:{fqcn}$*",
        f"-javaagent:{evosuite_runtime}",
        "-cp",
        os.pathsep.join(str(path) for path in runtime_classpath),
        "org.junit.runner.JUnitCore",
        *test_classes,
    ]
    execution = run_process(command, quality_root, java_environment(java_home), timeout_seconds)
    _write_command_artifacts(quality_root, "jacoco_test_execution", execution)
    if execution.timed_out:
        return {}, "JaCoCo test execution timed out"
    if execution.exit_code != 0:
        return {}, f"JaCoCo test execution failed with exit code {execution.exit_code}"
    if not exec_file.is_file():
        return {}, "JaCoCo did not create jacoco.exec"
    report_command = [
        java_executable(java_home, "java"),
        "-jar",
        str(tools.jacoco_cli),
        "report",
        str(exec_file),
        "--classfiles",
        str(focal_classes),
        "--sourcefiles",
        str(_focal_source_root(focal_source, fqcn)),
        "--csv",
        str(csv_file),
    ]
    report = run_process(report_command, quality_root, java_environment(java_home), timeout_seconds)
    _write_command_artifacts(quality_root, "jacoco_report", report)
    if report.timed_out or report.exit_code != 0 or not csv_file.is_file():
        return {}, "JaCoCo report generation failed"
    try:
        return read_jacoco_counters(csv_file, fqcn.split(".")[-1]), ""
    except (OSError, ValueError) as exc:
        return {}, str(exc)


def _run_pitest(
    *,
    quality_root: Path,
    java_home: str | None,
    memory_mb: int,
    timeout_seconds: int,
    pit_threads: int,
    fqcn: str,
    test_classes: list[str],
    runtime_classpath: list[Path],
    mutable_class_root: Path,
    focal_source: Path,
    tools: QualityTools,
    evosuite_runtime: Path,
) -> tuple[dict[str, Any], str]:
    pit_root = quality_root / "pitest"
    if pit_root.exists():
        safe_remove_tree(pit_root, quality_root)
    pit_root.mkdir(parents=True, exist_ok=True)
    classpath_file = pit_root / "classpath.txt"
    classpath_file.write_text("\n".join(str(path) for path in runtime_classpath) + "\n", encoding="utf-8")
    command = [
        java_executable(java_home, "java"),
        f"-Xmx{memory_mb}m",
        "-cp",
        os.pathsep.join(str(path) for path in tools.pitest_jars),
        "org.pitest.mutationtest.commandline.MutationCoverageReport",
        "--reportDir",
        str(pit_root),
        "--sourceDirs",
        str(_focal_source_root(focal_source, fqcn)),
        "--targetClasses",
        fqcn,
        "--targetTests",
        ",".join(test_classes),
        "--classPathFile",
        str(classpath_file),
        "--mutableCodePaths",
        str(mutable_class_root),
        "--outputFormats",
        "CSV",
        "--timestampedReports",
        "false",
        "--failWhenNoMutations",
        "false",
        # Match the original EvoSuite JUnit execution. PIT otherwise enables
        # Java assertions (-ea), which can make an originally green test fail
        # inside production code before mutation analysis begins.
        "--features",
        "-auto_assertions",
        "--threads",
        str(pit_threads),
        "--jvmPath",
        java_executable(java_home, "java"),
        "--jvmArgs",
        f"-javaagent:{evosuite_runtime}",
        "--useClasspathJar",
        "true",
        "--verbose",
        "true",
    ]
    result = run_process(command, quality_root, java_environment(java_home), timeout_seconds)
    _write_command_artifacts(quality_root, "pitest", result)
    mutation_files = sorted(pit_root.glob("**/mutations.csv"))
    if result.timed_out:
        return {}, "PIT timed out"
    if result.exit_code != 0:
        return {}, f"PIT failed with exit code {result.exit_code}"
    if not mutation_files:
        if "no mutations found" in result.output.lower():
            return {
                "mutations_total": 0,
                "mutations_killed": 0,
                "mutations_survived": 0,
                "mutations_no_coverage": 0,
                "mutations_timed_out": 0,
                "mutations_error": 0,
                "mutation_score": "",
                "mutation_not_applicable": True,
                "mutation_status_counts": {},
            }, ""
        return {}, "PIT did not create mutations.csv"
    try:
        return read_pitest_counters(mutation_files[-1], fqcn.split(".")[-1]), ""
    except (OSError, ValueError) as exc:
        return {}, str(exc)


def quality_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return str(row.get("project_id", "")), str(row.get("sample_file", "")), int(row.get("seed", 0))


def run_quality_sample(
    *,
    root: Path,
    dataset_dir: Path,
    source_record: dict[str, Any],
    config: dict[str, Any],
    evosuite_tools: EvoSuiteTools,
    quality_tools: QualityTools,
    coverage_timeout: int,
    mutation_timeout: int,
    memory_mb: int,
    pit_threads: int,
    keep_workspace: bool,
    keep_repo_cache: bool,
) -> dict[str, Any]:
    started_at = utc_now()
    record: dict[str, Any] = {
        "run_id": source_record.get("run_id", ""),
        "manifest_rank": source_record.get("manifest_rank", ""),
        "project_id": source_record.get("project_id", ""),
        "sample_file": source_record.get("sample_file", ""),
        "input_id": source_record.get("input_id", ""),
        "seed": source_record.get("seed", ""),
        "build_tool": source_record.get("build_tool", ""),
        "focal_class": source_record.get("focal_class", ""),
        "focal_class_fqcn": source_record.get("focal_class_fqcn", ""),
        "source_valid": source_record.get("valid") is True,
        "quality_status": "FAILED",
        "coverage_error": "",
        "mutation_error": "",
        "started_at_utc": started_at,
        "finished_at_utc": "",
    }
    artifact_dir = Path(str(source_record.get("artifact_dir") or ""))
    quality_root = artifact_dir / "t3"
    workspace = quality_root / "w"
    record["quality_artifact_dir"] = str(quality_root)
    cache_root = root / str(config.get("repo", {}).get("repos_dir", "repos"))
    cached_repo = cache_root / str(source_record.get("project_id") or "")
    try:
        if source_record.get("valid") is not True:
            raise ValueError("Table III quality metrics are only defined for VALID EvoSuite tests")
        manifest_row = {
            "project_id": str(source_record.get("project_id") or ""),
            "sample_file": str(source_record.get("sample_file") or ""),
        }
        sample = load_manifest_sample(dataset_dir, manifest_row)
        test_dir = artifact_dir / "tst"
        original_compiled_tests = artifact_dir / "bin"
        java_files = generated_java_files(test_dir)
        test_classes = generated_test_classes(java_files)
        if not java_files or not test_classes or not original_compiled_tests.is_dir():
            raise FileNotFoundError(f"Thiếu EvoSuite source/compiled artifacts trong {artifact_dir}")

        quality_root.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            safe_remove_tree(workspace, quality_root)
        repository = prepare_repository(
            sample,
            cached_repo,
            cache_root,
            bool(config.get("repo", {}).get("checkout_commit", True)),
        )
        ensure_experiment_workspace(cached_repo=repository, experiment_workspace=workspace)
        context, module_root = analyze_experiment(
            sample=sample,
            workspace=workspace,
            run_id=str(source_record.get("run_id") or "evosuite"),
            shard_id="evosuite-table-iii",
            agent_name="evosuite",
            generation_prompt="search-based",
        )
        fqcn = str(source_record.get("focal_class_fqcn") or "")
        if not fqcn:
            raise ValueError("EvoSuite record is missing focal_class_fqcn")
        java_selection = resolve_java_home(
            workspace,
            module_root,
            config,
            manual_java_home=str(source_record.get("java_home") or "") or None,
        )
        java_home = java_selection.java_home or None
        build_cfg = config.get("build", {})
        maven_cfg = build_cfg.get("maven", {})
        build_context = BuildContext(
            repository_root=workspace,
            module_root=module_root,
            build_tool=context.build_tool,
            generated_test_class_name=test_classes[0].split(".")[-1],
            generated_test_fqcn=test_classes[0],
            timeout_seconds=max(coverage_timeout, mutation_timeout),
            prefer_wrapper=bool(build_cfg.get("prefer_wrapper", True)),
            java_home=java_home,
            maven_multi_module_strategy=maven_cfg.get("multi_module_strategy", "module_only"),
            maven_use_also_make=bool(maven_cfg.get("use_also_make", True)),
            maven_fail_if_no_specified_tests=bool(maven_cfg.get("fail_if_no_specified_tests", False)),
        )
        baseline = verify_baseline(build_context)
        (quality_root / "baseline.log").write_text(baseline.raw_output or "", encoding="utf-8")
        if baseline.state is None or baseline.state.value != "MODULE_TESTS_PASSED":
            raise RuntimeError(f"Fresh baseline build failed: {baseline.primary_error or baseline.state}")
        classpath, classpath_result = resolve_project_classpath(
            build_context,
            quality_root,
            java_environment(java_home),
            coverage_timeout,
        )
        _write_command_artifacts(quality_root, "classpath", classpath_result)
        saved_classpath = read_saved_classpath(artifact_dir.parent / "maven_classpath.txt")
        classpath = _dedupe_existing([*classpath, *saved_classpath])
        bytecode = find_focal_bytecode(classpath, fqcn)
        if bytecode is None:
            raise FileNotFoundError(f"Không tìm thấy bytecode mới của {fqcn}")
        mutable_root = next(
            (entry for entry in classpath if entry.is_dir() and bytecode.is_relative_to(entry)),
            bytecode.parent,
        )
        focal_source = resolve_focal_path(workspace, sample.focal_class_path)
        instrumentable_sources, patched_count = prepare_instrumentable_tests(
            test_dir, quality_root / "instrumentable-tests"
        )
        compiled_tests = quality_root / "compiled-tests"
        compilation = compile_generated_tests(
            instrumentable_sources,
            compiled_tests,
            classpath,
            evosuite_tools,
            java_home,
            module_root,
            coverage_timeout,
        )
        _write_command_artifacts(quality_root, "instrumentable_test_compilation", compilation)
        if compilation.timed_out or compilation.exit_code != 0:
            raise RuntimeError("Không compile được bản EvoSuite test dành cho JaCoCo/PIT")
        runtime_classpath = _dedupe_existing(
            [compiled_tests, *classpath, evosuite_tools.runtime, evosuite_tools.junit, evosuite_tools.hamcrest]
        )
        record.update(
            {
                "java_home": java_home or "",
                "test_classes": ",".join(test_classes),
                "instrumentation_adapter_replacements": patched_count,
                "saved_generation_classpath_entries_reused": len(saved_classpath),
                "runtime_classpath_entries": len(runtime_classpath),
            }
        )

        coverage, coverage_error = _run_jacoco(
            quality_root=quality_root,
            java_home=java_home,
            memory_mb=memory_mb,
            timeout_seconds=coverage_timeout,
            fqcn=fqcn,
            test_classes=test_classes,
            runtime_classpath=runtime_classpath,
            focal_bytecode=bytecode,
            focal_source=focal_source,
            tools=quality_tools,
            evosuite_runtime=evosuite_tools.runtime,
        )
        record.update(coverage)
        record["coverage_error"] = coverage_error

        mutation, mutation_error = _run_pitest(
            quality_root=quality_root,
            java_home=java_home,
            memory_mb=memory_mb,
            timeout_seconds=mutation_timeout,
            pit_threads=pit_threads,
            fqcn=fqcn,
            test_classes=test_classes,
            runtime_classpath=runtime_classpath,
            mutable_class_root=mutable_root,
            focal_source=focal_source,
            tools=quality_tools,
            evosuite_runtime=evosuite_tools.runtime,
        )
        record.update(mutation)
        record["mutation_error"] = mutation_error
        # A focal class can legitimately contain no branches.  Successful CSV
        # parsing, not the presence of every denominator, defines completion.
        coverage_complete = not coverage_error and bool(coverage)
        mutation_complete = not mutation_error and bool(mutation)
        record["coverage_complete"] = coverage_complete
        record["mutation_complete"] = mutation_complete
        record["quality_status"] = "COMPLETE" if coverage_complete and mutation_complete else "PARTIAL"
    except Exception as exc:
        record["setup_error"] = f"{type(exc).__name__}: {exc}"
        record["quality_status"] = "FAILED"
    finally:
        record["finished_at_utc"] = utc_now()
        if not keep_workspace and workspace.exists():
            try:
                safe_remove_tree(workspace, quality_root)
            except Exception as exc:
                record["cleanup_warning"] = f"workspace cleanup: {type(exc).__name__}: {exc}"
        if not keep_repo_cache and cached_repo.exists():
            try:
                safe_remove_tree(cached_repo, cache_root)
            except Exception as exc:
                record["cleanup_warning"] = f"repo cleanup: {type(exc).__name__}: {exc}"
    return record


def _numeric(rows: list[dict[str, Any]], field: str) -> list[float]:
    output: list[float] = []
    for row in rows:
        try:
            value = row.get(field, "")
            if value != "":
                output.append(float(value))
        except (TypeError, ValueError):
            continue
    return output


def _percent(numerator: int, denominator: int) -> float | str:
    return round(numerator * 100 / denominator, 2) if denominator else ""


def _weighted(rows: list[dict[str, Any]], prefix: str) -> float | str:
    covered = sum(int(row.get(f"{prefix}_covered") or 0) for row in rows)
    missed = sum(int(row.get(f"{prefix}_missed") or 0) for row in rows)
    return _percent(covered, covered + missed)


def summarize_table_iii(source_records: list[dict[str, Any]], quality_records: list[dict[str, Any]]) -> dict[str, Any]:
    valid_sources = [row for row in source_records if row.get("valid") is True]
    eligible = [row for row in source_records if row.get("baseline_eligible") is True]
    coverage_rows = [row for row in quality_records if row.get("coverage_complete") is True]
    mutation_rows = [row for row in quality_records if row.get("mutation_complete") is True]
    complete_rows = [row for row in quality_records if row.get("quality_status") == "COMPLETE"]
    statuses: dict[str, int] = {}
    for row in quality_records:
        status = str(row.get("quality_status") or "UNKNOWN")
        statuses[status] = statuses.get(status, 0) + 1
    target_n = len(valid_sources)
    mutation_total = sum(int(row.get("mutations_total") or 0) for row in mutation_rows)
    mutation_killed = sum(int(row.get("mutations_killed") or 0) for row in mutation_rows)
    result: dict[str, Any] = {
        "model": "EvoSuite",
        "prompt": "Search-based baseline",
        "records_total": len(source_records),
        "baseline_valid_evaluable_n": len(eligible),
        "valid_test_n": target_n,
        "valid_rate_end_to_end_pct": _percent(target_n, len(source_records)),
        "valid_rate_conditional_pct": _percent(target_n, len(eligible)),
        "quality_target_n": target_n,
        "quality_records_n": len(quality_records),
        "coverage_complete_n": len(coverage_rows),
        "mutation_complete_n": len(mutation_rows),
        "all_metrics_complete_n": len(complete_rows),
        "quality_status_counts": statuses,
        "IC_weighted_pct": _weighted(coverage_rows, "instruction"),
        "LC_weighted_pct": _weighted(coverage_rows, "line"),
        "BC_weighted_pct": _weighted(coverage_rows, "branch"),
        "MC_weighted_pct": _weighted(coverage_rows, "method"),
        "MS_weighted_pct": _percent(mutation_killed, mutation_total),
        "mutations_total": mutation_total,
        "mutations_killed": mutation_killed,
    }
    for abbreviation, field in (
        ("IC", "coverage_instruction"),
        ("LC", "coverage_line"),
        ("BC", "coverage_branch"),
        ("MC", "coverage_method"),
        ("MS", "mutation_score"),
    ):
        values = _numeric(quality_records, field)
        result[f"{abbreviation}_macro_mean_pct"] = round(fmean(values), 2) if values else ""
        result[f"{abbreviation}_n"] = len(values)
    result["table_iii_ready"] = (
        target_n > 0 and len(coverage_rows) == target_n and len(mutation_rows) == target_n
    )
    result["table_iii_values_note"] = (
        "Use end-to-end valid rate (valid_test_n/records_total) and weighted IC/LC/BC/MC/MS. "
        "Do not copy quality values until table_iii_ready=true; *_n fields expose missing measurements."
    )
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
    result["summary_sha256"] = hashlib.sha256(payload).hexdigest()
    return result
