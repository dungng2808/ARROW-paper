from __future__ import annotations

import csv
import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .build_runner import BuildContext, run_command, select_maven_command
from .models import VerificationResult


@dataclass
class MetricsResult:
    coverage_instruction: str = ""
    coverage_branch: str = ""
    coverage_line: str = ""
    coverage_method: str = ""
    mutation_score: str = ""
    mutations_total: str = ""
    mutations_killed: str = ""
    mutations_survived: str = ""
    test_smell_total: str = ""
    test_smell_details: str = ""
    coverage_error: str = ""
    mutation_error: str = ""
    smell_error: str = ""
    smell_values: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_maven_metrics(
    ctx: BuildContext,
    focal_class_name: str,
    focal_class_fqcn: str,
    testing_framework: str = "unknown",
    project_id: str = "",
    generated_test_path: Path | None = None,
    focal_class_path: str = "",
    smells_enabled: bool = False,
) -> tuple[MetricsResult, dict[str, VerificationResult]]:
    result = MetricsResult()
    verifications: dict[str, VerificationResult] = {}

    coverage = _run_jacoco(ctx)
    verifications["coverage"] = coverage
    if coverage.exit_code == 0:
        coverage_csv = _find_coverage_csv(ctx)
        if coverage_csv is None and _pom_declares_jacoco(ctx):
            coverage = _run_jacoco(ctx, force_prepare_agent=True)
            verifications["coverage_retry_prepare_agent"] = coverage
            if coverage.exit_code == 0:
                coverage_csv = _find_coverage_csv(ctx)
        if coverage_csv:
            result.artifacts["coverage_csv"] = str(coverage_csv)
            _read_jacoco_csv(coverage_csv, focal_class_name, result)
        else:
            result.coverage_error = "jacoco csv not found"
    else:
        result.coverage_error = _metric_error(coverage, "jacoco command failed")

    if testing_framework == "junit5":
        patched_pom = _patch_pom_for_pitest_junit5(ctx)
        if patched_pom:
            result.artifacts["pitest_junit5_patched_pom"] = str(patched_pom)
    if testing_framework == "testng":
        patched_pom = _patch_pom_for_pitest_testng(ctx)
        if patched_pom:
            result.artifacts["pitest_testng_patched_pom"] = str(patched_pom)

    mutation_prepare = _run_pitest_dependency_prepare(ctx)
    if mutation_prepare is not None:
        verifications["mutation_prepare"] = mutation_prepare
        if mutation_prepare.exit_code != 0:
            result.mutation_error = "pitest dependency prepare failed: " + _metric_error(mutation_prepare, "maven install failed")
            if smells_enabled and generated_test_path and focal_class_path:
                _run_test_smells(
                    ctx=ctx,
                    result=result,
                    project_id=project_id,
                    generated_test_path=generated_test_path,
                    focal_class_path=focal_class_path,
                )
            return result, verifications

    mutation = _run_pitest(ctx, focal_class_fqcn, testing_framework)
    verifications["mutation"] = mutation
    if mutation.exit_code == 0:
        mutation_csv = _find_first(
            [
                ctx.module_root / "target" / "pit-reports" / "mutations.csv",
                *sorted((ctx.module_root / "target" / "pit-reports").glob("*/mutations.csv")),
            ]
        )
        if mutation_csv:
            result.artifacts["mutation_csv"] = str(mutation_csv)
            _read_pitest_csv(mutation_csv, focal_class_name, result)
            if result.mutation_error:
                _read_pitest_summary(mutation.raw_output, result)
        else:
            _read_pitest_summary(mutation.raw_output, result)
            if not result.mutation_score:
                result.mutation_error = "pitest mutations.csv not found"
    else:
        result.mutation_error = _metric_error(mutation, "pitest command failed")

    if smells_enabled and generated_test_path and focal_class_path:
        _run_test_smells(
            ctx=ctx,
            result=result,
            project_id=project_id,
            generated_test_path=generated_test_path,
            focal_class_path=focal_class_path,
        )

    return result, verifications


def _coverage_csv_candidates(ctx: BuildContext) -> list[Path]:
    return [
        ctx.module_root / "target" / "site" / "jacoco" / "jacoco.csv",
        ctx.module_root / "target" / "site" / "jacoco-ut" / "jacoco.csv",
    ]


def _find_coverage_csv(ctx: BuildContext) -> Path | None:
    return _find_first(_coverage_csv_candidates(ctx))


def _run_jacoco(ctx: BuildContext, force_prepare_agent: bool = False) -> VerificationResult:
    command = select_maven_command(ctx)
    goals = []
    if force_prepare_agent or not _pom_declares_jacoco(ctx):
        goals.append("org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent")
    goals.extend(["test", "org.jacoco:jacoco-maven-plugin:0.8.12:report"])
    command.extend(
        [
            "-DfailIfNoTests=false",
            "-Dsurefire.failIfNoSpecifiedTests=false",
            f"-Dtest={ctx.generated_test_class_name}",
            *goals,
        ]
    )
    return run_command(ctx, command, ctx.module_root, "maven", target_only=True)


def _run_pitest(ctx: BuildContext, focal_class_fqcn: str, testing_framework: str) -> VerificationResult:
    command = _select_maven_module_command(ctx)
    command.extend(
        [
            "-DfailIfNoTests=false",
            "-Dsurefire.failIfNoSpecifiedTests=false",
            f"-DtargetClasses={focal_class_fqcn}",
            f"-DtargetTests={ctx.generated_test_fqcn}",
            "-DoutputFormats=CSV",
            "-DtimestampedReports=false",
            "-DfailWhenNoMutations=false",
            "org.pitest:pitest-maven:1.17.4:mutationCoverage",
        ]
    )
    return run_command(ctx, command, ctx.module_root, "maven", target_only=True)


def _run_pitest_dependency_prepare(ctx: BuildContext) -> VerificationResult | None:
    if ctx.repository_root.resolve() == ctx.module_root.resolve():
        return None
    if not (ctx.repository_root / "pom.xml").is_file():
        return None
    command = select_maven_command(ctx)
    command.extend(
        [
            "-DskipTests",
            "-Ddocker.skip=true",
            "-DfailIfNoTests=false",
            "-Dsurefire.failIfNoSpecifiedTests=false",
            "install",
        ]
    )
    return run_command(ctx, command, ctx.module_root, "maven", target_only=False)


def _select_maven_module_command(ctx: BuildContext) -> list[str]:
    module_ctx = replace(ctx, maven_multi_module_strategy="module_only")
    return select_maven_command(module_ctx)


def _patch_pom_for_pitest_junit5(ctx: BuildContext) -> Path | None:
    return _patch_pom_for_pitest_plugin(
        ctx=ctx,
        dependency_artifact_id="pitest-junit5-plugin",
        dependency_version="1.2.2",
    )


def _patch_pom_for_pitest_testng(ctx: BuildContext) -> Path | None:
    return _patch_pom_for_pitest_plugin(
        ctx=ctx,
        dependency_artifact_id="pitest-testng-plugin",
        dependency_version="1.0.0",
        test_plugin="testng",
    )


def _patch_pom_for_pitest_plugin(
    *,
    ctx: BuildContext,
    dependency_artifact_id: str,
    dependency_version: str,
    test_plugin: str = "",
) -> Path | None:
    pom = ctx.module_root / "pom.xml"
    if not pom.is_file():
        return None
    original = pom.read_text(encoding="utf-8", errors="ignore")
    if dependency_artifact_id in original and (not test_plugin or f"<testPlugin>{test_plugin}</testPlugin>" in original):
        return pom

    tree = ET.parse(pom)
    root = tree.getroot()
    namespace = _xml_namespace(root)
    if namespace:
        ET.register_namespace("", namespace)

    build = _child(root, "build", namespace)
    plugins = _child(build, "plugins", namespace)
    plugin = _find_plugin(plugins, namespace, "org.pitest", "pitest-maven")
    if plugin is None:
        plugin = ET.SubElement(plugins, _tag("plugin", namespace))
        ET.SubElement(plugin, _tag("groupId", namespace)).text = "org.pitest"
        ET.SubElement(plugin, _tag("artifactId", namespace)).text = "pitest-maven"
        ET.SubElement(plugin, _tag("version", namespace)).text = "1.17.4"
    elif _find_child(plugin, "version", namespace) is None:
        ET.SubElement(plugin, _tag("version", namespace)).text = "1.17.4"

    dependencies = _child(plugin, "dependencies", namespace)
    dependency = _find_dependency(dependencies, namespace, "org.pitest", dependency_artifact_id)
    if dependency is None:
        dependency = ET.SubElement(dependencies, _tag("dependency", namespace))
        ET.SubElement(dependency, _tag("groupId", namespace)).text = "org.pitest"
        ET.SubElement(dependency, _tag("artifactId", namespace)).text = dependency_artifact_id
        ET.SubElement(dependency, _tag("version", namespace)).text = dependency_version

    if test_plugin:
        configuration = _child(plugin, "configuration", namespace)
        test_plugin_node = _find_child(configuration, "testPlugin", namespace)
        if test_plugin_node is None:
            test_plugin_node = ET.SubElement(configuration, _tag("testPlugin", namespace))
        test_plugin_node.text = test_plugin

    _indent(root)
    tree.write(pom, encoding="utf-8", xml_declaration=True)
    return pom


def _xml_namespace(root: ET.Element) -> str:
    if root.tag.startswith("{"):
        return root.tag[1:].split("}", 1)[0]
    return ""


def _tag(name: str, namespace: str) -> str:
    return f"{{{namespace}}}{name}" if namespace else name


def _find_child(parent: ET.Element, name: str, namespace: str) -> ET.Element | None:
    return parent.find(_tag(name, namespace))


def _child(parent: ET.Element, name: str, namespace: str) -> ET.Element:
    child = _find_child(parent, name, namespace)
    if child is None:
        child = ET.SubElement(parent, _tag(name, namespace))
    return child


def _text(parent: ET.Element, name: str, namespace: str) -> str:
    child = _find_child(parent, name, namespace)
    return (child.text or "").strip() if child is not None else ""


def _find_plugin(plugins: ET.Element, namespace: str, group_id: str, artifact_id: str) -> ET.Element | None:
    for plugin in plugins.findall(_tag("plugin", namespace)):
        plugin_group = _text(plugin, "groupId", namespace) or "org.apache.maven.plugins"
        if plugin_group == group_id and _text(plugin, "artifactId", namespace) == artifact_id:
            return plugin
    return None


def _find_dependency(dependencies: ET.Element, namespace: str, group_id: str, artifact_id: str) -> ET.Element | None:
    for dependency in dependencies.findall(_tag("dependency", namespace)):
        if _text(dependency, "groupId", namespace) == group_id and _text(dependency, "artifactId", namespace) == artifact_id:
            return dependency
    return None


def _indent(element: ET.Element, level: int = 0) -> None:
    indent_text = "\n" + level * "  "
    if len(element):
        if not element.text or not element.text.strip():
            element.text = indent_text + "  "
        for child in element:
            _indent(child, level + 1)
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = indent_text
    if level and (not element.tail or not element.tail.strip()):
        element.tail = indent_text


def _pom_declares_jacoco(ctx: BuildContext) -> bool:
    candidates = [ctx.module_root / "pom.xml"]
    if ctx.repository_root != ctx.module_root:
        candidates.append(ctx.repository_root / "pom.xml")
    for pom in candidates:
        if pom.is_file() and "jacoco-maven-plugin" in pom.read_text(encoding="utf-8", errors="ignore"):
            return True
    return False


def _find_first(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _metric_error(verification: VerificationResult, fallback: str) -> str:
    lowered = verification.raw_output.lower()
    if "testng is on the classpath but the pitest testng plugin is not installed" in lowered:
        return "pitest TestNG plugin is not installed"
    if "pitest could not run any tests" in lowered:
        return "pitest could not run any tests; check the test framework plugin"
    if "please check you have correctly installed the pitest plugin for your project's test library" in lowered:
        return "pitest test framework plugin is missing"
    return verification.primary_error or fallback


def _pct(covered: str, missed: str) -> str:
    try:
        covered_num = float(covered)
        missed_num = float(missed)
    except ValueError:
        return ""
    total = covered_num + missed_num
    if total <= 0:
        return ""
    return f"{covered_num / total * 100:.2f}"


def _read_jacoco_csv(path: Path, focal_class_name: str, result: MetricsResult) -> None:
    with path.open("r", encoding="utf-8", newline="") as input_file:
        rows = list(csv.DictReader(input_file))
    focal_rows = [row for row in rows if row.get("CLASS", "").split("$")[0] == focal_class_name]
    if not focal_rows:
        result.coverage_error = f"focal class {focal_class_name} not found in jacoco csv"
        return
    instruction_covered = sum(int(row.get("INSTRUCTION_COVERED") or 0) for row in focal_rows)
    instruction_missed = sum(int(row.get("INSTRUCTION_MISSED") or 0) for row in focal_rows)
    branch_covered = sum(int(row.get("BRANCH_COVERED") or 0) for row in focal_rows)
    branch_missed = sum(int(row.get("BRANCH_MISSED") or 0) for row in focal_rows)
    line_covered = sum(int(row.get("LINE_COVERED") or 0) for row in focal_rows)
    line_missed = sum(int(row.get("LINE_MISSED") or 0) for row in focal_rows)
    method_covered = sum(int(row.get("METHOD_COVERED") or 0) for row in focal_rows)
    method_missed = sum(int(row.get("METHOD_MISSED") or 0) for row in focal_rows)
    result.coverage_instruction = _pct(str(instruction_covered), str(instruction_missed))
    result.coverage_branch = _pct(str(branch_covered), str(branch_missed))
    result.coverage_line = _pct(str(line_covered), str(line_missed))
    result.coverage_method = _pct(str(method_covered), str(method_missed))


def _read_pitest_csv(path: Path, focal_class_name: str, result: MetricsResult) -> None:
    with path.open("r", encoding="utf-8", newline="") as input_file:
        rows = list(csv.reader(input_file))
    if not rows:
        result.mutation_error = "pitest mutations.csv is empty"
        return
    header = [cell.strip().lower() for cell in rows[0]]
    data_rows = rows[1:] if "status" in header or "result" in header else rows
    status_index = header.index("status") if "status" in header else header.index("result") if "result" in header else 5
    class_index = header.index("class") if "class" in header else header.index("focal_class") if "focal_class" in header else (
        1 if data_rows and len(data_rows[0]) > 1 and data_rows[0][0].strip().endswith(".java") else 0
    )
    focal_rows = [row for row in data_rows if len(row) > max(class_index, status_index) and row[class_index].split(".")[-1].replace(".java", "") == focal_class_name]
    total = len(focal_rows)
    killed = sum(1 for row in focal_rows if row[status_index].strip().upper() in {"KILLED", "TIMED_OUT"})
    survived = sum(1 for row in focal_rows if row[status_index].strip().upper() == "SURVIVED")
    if total == 0:
        result.mutation_error = f"focal class {focal_class_name} not found in pitest csv"
        return
    result.mutations_total = str(total)
    result.mutations_killed = str(killed)
    result.mutations_survived = str(survived)
    result.mutation_score = f"{killed / total * 100:.2f}"
    result.mutation_error = ""


def _read_pitest_summary(raw_output: str, result: MetricsResult) -> None:
    match = re.search(r"Generated\s+(\d+)\s+mutations\s+Killed\s+(\d+)\s+\((\d+)%\)", raw_output, re.IGNORECASE)
    if not match:
        return
    total = int(match.group(1))
    killed = int(match.group(2))
    if total <= 0:
        return
    result.mutations_total = str(total)
    result.mutations_killed = str(killed)
    result.mutations_survived = ""
    result.mutation_score = f"{killed / total * 100:.2f}"
    result.mutation_error = ""


SMELL_COLUMNS = [
    "Assertion Roulette",
    "Conditional Test Logic",
    "Constructor Initialization",
    "Default Test",
    "EmptyTest",
    "Exception Handling",
    "General Fixture",
    "Mystery Guest",
    "Print Statement",
    "Redundant Assertion",
    "Sensitive Equality",
    "Verbose Test",
    "Sleepy Test",
    "Eager Test",
    "Lazy Test",
    "Duplicate Assert",
    "Unknown Test",
    "IgnoredTest",
    "Resource Optimism",
    "Magic Number Test",
    "Dependent Test",
]

SMELL_ALIASES = {
    "Exception Handling": ["Exception Handling", "Exception Catching Throwing"],
}


def _run_test_smells(
    *,
    ctx: BuildContext,
    result: MetricsResult,
    project_id: str,
    generated_test_path: Path,
    focal_class_path: str,
) -> None:
    jar = _find_test_smell_detector(ctx.repository_root)
    if not jar.is_file():
        result.smell_error = f"TestSmellDetector.jar not found: {jar}"
        return
    focal_path = _strip_repo_prefix(ctx.repository_root, focal_class_path)
    if not generated_test_path.is_file():
        result.smell_error = f"generated test not found: {generated_test_path}"
        return
    if not focal_path.is_file():
        result.smell_error = f"focal class not found: {focal_path}"
        return

    smell_dir = ctx.repository_root.parent / "test_smells"
    smell_dir.mkdir(parents=True, exist_ok=True)
    input_csv = smell_dir / "pathToInputFile.csv"
    with input_csv.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow([project_id or ctx.repository_root.name, str(generated_test_path.resolve()), str(focal_path.resolve())])
    result.artifacts["smell_input_csv"] = str(input_csv)

    try:
        completed = subprocess.run(
            ["java", "-jar", str(jar), str(input_csv)],
            cwd=smell_dir,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=ctx.timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.smell_error = f"{type(exc).__name__}: {exc}"
        return

    smell_output_log = smell_dir / "test_smell_output.txt"
    smell_output_log.write_text((completed.stdout or "") + ("\n" if completed.stdout and completed.stderr else "") + (completed.stderr or ""), encoding="utf-8")
    result.artifacts["smell_output_log"] = str(smell_output_log)
    if completed.returncode != 0:
        result.smell_error = f"tsDetect failed with exit code {completed.returncode}"
        return

    output_csv = _find_latest_smell_output(smell_dir)
    if output_csv is None:
        result.smell_error = "tsDetect output csv not found"
        return
    result.artifacts["smell_output_csv"] = str(output_csv)
    _read_smell_csv(output_csv, result)


def _find_latest_smell_output(smell_dir: Path) -> Path | None:
    candidates = sorted(smell_dir.glob("Output_TestSmellDetection*.csv"), key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _find_test_smell_detector(start: Path) -> Path:
    for parent in [start, *start.parents]:
        if parent.name in {"ARROW", "agone-paper-lite"}:
            return parent.parent / "classes2test" / "AgoneTest" / "TestSmellDetector.jar"
    return start.parent / "classes2test" / "AgoneTest" / "TestSmellDetector.jar"


def _strip_repo_prefix(workspace: Path, relative: str) -> Path:
    path = Path(relative)
    candidate = workspace / path
    if candidate.exists():
        return candidate
    if len(path.parts) > 1:
        candidate = workspace / Path(*path.parts[1:])
        if candidate.exists():
            return candidate
    return workspace / path


def _read_smell_csv(path: Path, result: MetricsResult) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as input_file:
        rows = list(csv.DictReader(input_file))
    if not rows:
        result.smell_error = "tsDetect output csv is empty"
        return
    row = rows[0]
    values: dict[str, str] = {}
    total = 0
    for smell in SMELL_COLUMNS:
        raw = _smell_value(row, smell)
        values[smell] = raw
        total += _int_or_zero(raw)
    result.smell_values = values
    result.test_smell_total = str(total)
    result.test_smell_details = ";".join(f"{key}={value}" for key, value in values.items())
    result.smell_error = ""


def _smell_value(row: dict[str, str], smell: str) -> str:
    for key in [smell, *SMELL_ALIASES.get(smell, [])]:
        if key in row and row[key] not in {None, ""}:
            return str(row[key])
    normalized = {_normalize_header(key): value for key, value in row.items()}
    for key in [smell, *SMELL_ALIASES.get(smell, [])]:
        value = normalized.get(_normalize_header(key))
        if value not in {None, ""}:
            return str(value)
    return "0"


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _int_or_zero(value: str) -> int:
    try:
        return int(float(value))
    except ValueError:
        return 0
