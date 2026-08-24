from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .build_runner import BuildContext, select_gradle_command, select_maven_command, verify_baseline
from .input_selector import load_sample
from .java_resolver import _java_version_from_home, resolve_java_home
from .models import FailureOrigin, FailureState, SampleInput
from .project_analyzer import analyze_experiment
from .repo_manager import checkout_dataset_revision, clone_repo, ensure_experiment_workspace, safe_remove_tree


EVOSUITE_VERSION = "1.2.0"
TOOL_URLS = {
    "evosuite": f"https://github.com/EvoSuite/evosuite/releases/download/v{EVOSUITE_VERSION}/evosuite-{EVOSUITE_VERSION}.jar",
    "runtime": f"https://github.com/EvoSuite/evosuite/releases/download/v{EVOSUITE_VERSION}/evosuite-standalone-runtime-{EVOSUITE_VERSION}.jar",
    "junit": "https://repo1.maven.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar",
    "hamcrest": "https://repo1.maven.org/maven2/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar",
}


@dataclass(frozen=True)
class EvoSuiteTools:
    evosuite: Path
    runtime: Path
    junit: Path
    hamcrest: Path

    def paths(self) -> tuple[Path, ...]:
        return self.evosuite, self.runtime, self.junit, self.hamcrest


@dataclass
class CommandResult:
    command: list[str]
    exit_code: int | None
    output: str
    elapsed_seconds: float
    timed_out: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_tools(tools_dir: Path) -> EvoSuiteTools:
    return EvoSuiteTools(
        evosuite=tools_dir / f"evosuite-{EVOSUITE_VERSION}.jar",
        runtime=tools_dir / f"evosuite-standalone-runtime-{EVOSUITE_VERSION}.jar",
        junit=tools_dir / "junit-4.13.2.jar",
        hamcrest=tools_dir / "hamcrest-core-1.3.jar",
    )


def ensure_tools(tools: EvoSuiteTools, download: bool) -> dict[str, dict[str, Any]]:
    names = ("evosuite", "runtime", "junit", "hamcrest")
    missing = [(name, path) for name, path in zip(names, tools.paths()) if not path.is_file()]
    if missing and not download:
        lines = "\n".join(f"  - {name}: {path}" for name, path in missing)
        raise FileNotFoundError(
            "Thiếu công cụ EvoSuite/JUnit:\n"
            f"{lines}\n"
            "Chạy lại với --download-tools --setup-only để tải từ bản phát hành chính thức."
        )
    for name, destination in missing:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            urllib.request.urlretrieve(TOOL_URLS[name], temporary)
            if temporary.stat().st_size < 1024:
                raise RuntimeError(f"File tải về quá nhỏ: {temporary}")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        name: {"path": str(path.resolve()), "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for name, path in zip(names, tools.paths())
    }


def read_manifest(path: Path, start_rank: int = 0, limit: int = 0) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy manifest: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not {"project_id", "sample_file"}.issubset(rows[0]):
        raise ValueError("Manifest phải có cột project_id và sample_file")
    selected: list[dict[str, str]] = []
    seen_projects: set[str] = set()
    seen_samples: set[tuple[str, str]] = set()
    for row in rows:
        project_id = str(row.get("project_id", "")).strip()
        sample_file = Path(str(row.get("sample_file", "")).strip()).name
        rank_text = str(row.get("rank", "")).strip()
        rank = int(rank_text) if rank_text.isdigit() else 0
        if start_rank and rank and rank < start_rank:
            continue
        key = (project_id, sample_file)
        if not project_id or not sample_file:
            raise ValueError(f"Manifest có hàng thiếu project_id/sample_file: {row}")
        if key in seen_samples:
            raise ValueError(f"Manifest có sample lặp: {project_id}/{sample_file}")
        if project_id in seen_projects:
            raise ValueError(f"Manifest không còn one-sample-per-repository: {project_id}")
        seen_samples.add(key)
        seen_projects.add(project_id)
        selected.append({"rank": rank_text, "project_id": project_id, "sample_file": sample_file})
    return selected[:limit] if limit > 0 else selected


def load_manifest_sample(dataset_dir: Path, row: dict[str, str]) -> SampleInput:
    path = dataset_dir / row["project_id"] / row["sample_file"]
    if not path.is_file():
        raise FileNotFoundError(f"Manifest trỏ tới sample không tồn tại: {path}")
    return load_sample(path, dataset_dir)


def package_from_java(path: Path) -> str:
    source = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?m)^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", source)
    return match.group(1) if match else ""


def resolve_focal_path(workspace: Path, relative: str) -> Path:
    path = Path(relative)
    candidates = [workspace / path]
    if len(path.parts) > 1:
        candidates.append(workspace / Path(*path.parts[1:]))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def focal_fqcn(sample: SampleInput, workspace: Path) -> str:
    focal_path = resolve_focal_path(workspace, sample.focal_class_path)
    package = package_from_java(focal_path)
    return f"{package}.{sample.focal_class_name}" if package else sample.focal_class_name


EVOSUITE_ADD_OPENS = (
    "--add-opens=java.base/java.lang=ALL-UNNAMED",
    "--add-opens=java.base/java.lang.reflect=ALL-UNNAMED",
    "--add-opens=java.base/java.util=ALL-UNNAMED",
    "--add-opens=java.base/java.util.concurrent=ALL-UNNAMED",
    "--add-opens=java.base/java.net=ALL-UNNAMED",
    "--add-opens=java.base/java.io=ALL-UNNAMED",
    "--add-opens=java.base/java.nio=ALL-UNNAMED",
    "--add-opens=java.base/java.text=ALL-UNNAMED",
    "--add-opens=java.base/sun.reflect.annotation=ALL-UNNAMED",
    "--add-opens=java.base/sun.security.action=ALL-UNNAMED",
    "--add-opens=java.desktop/java.awt=ALL-UNNAMED",
    "--add-opens=java.desktop/java.awt.font=ALL-UNNAMED",
)


def _is_java_9_plus(java_home: str | None) -> bool:
    if not java_home:
        return True
    version_str = _java_version_from_home(java_home)
    try:
        return int(version_str) >= 9
    except (TypeError, ValueError):
        return True


def java_environment(java_home: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = str(Path(java_home) / "bin") + os.pathsep + env.get("PATH", "")
    if _is_java_9_plus(java_home):
        existing_jdk_opts = env.get("JDK_JAVA_OPTIONS", "").strip()
        add_opens_str = " ".join(EVOSUITE_ADD_OPENS)
        env["JDK_JAVA_OPTIONS"] = f"{existing_jdk_opts} {add_opens_str}".strip()
    return env


def java_executable(java_home: str | None, name: str) -> str:
    executable = f"{name}.exe" if os.name == "nt" else name
    if java_home:
        candidate = Path(java_home) / "bin" / executable
        if candidate.is_file():
            return str(candidate)
    return shutil.which(executable) or executable


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, text=True)
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def run_process(command: list[str], cwd: Path, env: dict[str, str], timeout_seconds: int) -> CommandResult:
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            preexec_fn=None if os.name == "nt" else os.setsid,
        )
        try:
            output, _ = proc.communicate(timeout=timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            _stop_process(proc)
            output, _ = proc.communicate()
            timed_out = True
        return CommandResult(command, proc.returncode, output or "", round(time.monotonic() - started, 3), timed_out)
    except OSError as exc:
        return CommandResult(command, None, f"{type(exc).__name__}: {exc}", round(time.monotonic() - started, 3), False)


def _path_entries(text: str) -> list[Path]:
    return [Path(item) for item in text.strip().split(os.pathsep) if item.strip()]


def _dedupe_existing(paths: Iterable[Path]) -> list[Path]:
    output: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        value = str(path.resolve()) if path.exists() else str(path)
        is_classpath_entry = path.is_dir() or (path.is_file() and path.suffix.lower() in {".jar", ".zip"})
        if value not in seen and is_classpath_entry:
            output.append(Path(value))
            seen.add(value)
    return output


def _compiled_class_dirs(workspace: Path, module_root: Path) -> list[Path]:
    preferred = [
        module_root / "target" / "classes",
        module_root / "target" / "test-classes",
        module_root / "build" / "classes" / "java" / "main",
        module_root / "build" / "classes" / "java" / "test",
        module_root / "build" / "classes" / "kotlin" / "main",
    ]
    discovered = list(workspace.glob("**/target/classes")) + list(workspace.glob("**/build/classes/java/main"))
    return _dedupe_existing([*preferred, *discovered])


def resolve_maven_classpath(
    ctx: BuildContext,
    destination: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[list[Path], CommandResult]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A relative output path is resolved separately under every reactor module.
    # This prevents upstream modules from overwriting one shared absolute file.
    relative_output = Path("target") / "arrow-evosuite-classpath.txt"
    for stale in ctx.repository_root.glob(f"**/{relative_output.as_posix()}"):
        stale.unlink(missing_ok=True)
    module_output = ctx.module_root / relative_output
    command = select_maven_command(ctx)
    command.extend(
        [
            # EvoSuite/JUnit are supplied by this runner. Runtime scope avoids
            # unrelated, often unavailable historical test-only dependencies.
            "-DincludeScope=runtime",
            "-Dmdep.outputAbsoluteArtifactFilename=true",
            f"-Dmdep.outputFile={relative_output.as_posix()}",
            "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:build-classpath",
        ]
    )
    result = run_process(command, ctx.module_root, env, timeout_seconds)
    dependencies = _path_entries(module_output.read_text(encoding="utf-8", errors="replace")) if module_output.is_file() else []
    if module_output.is_file():
        destination.write_text(module_output.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    return _dedupe_existing([*_compiled_class_dirs(ctx.repository_root, ctx.module_root), *dependencies]), result


def gradle_project_path(repository_root: Path, module_root: Path) -> str:
    if repository_root.resolve() == module_root.resolve():
        return ":"
    relative = module_root.resolve().relative_to(repository_root.resolve())
    return ":" + ":".join(relative.parts)


def gradle_init_script_text() -> str:
    return """allprojects { p ->
    if (p.path == System.getProperty('arrowTargetProject') && p.tasks.findByName('arrowPrintClasspath') == null) {
        p.tasks.create(name: 'arrowPrintClasspath') {
            doLast {
                def sets = p.extensions.findByName('sourceSets')
                if (sets == null) {
                    throw new GradleException('Java sourceSets not found for ' + p.path)
                }
                println 'ARROW_CLASSPATH=' + sets.test.runtimeClasspath.asPath
            }
        }
    }
}
"""


def parse_gradle_classpath(output: str) -> list[Path]:
    values = [line.split("=", 1)[1].strip() for line in output.splitlines() if line.startswith("ARROW_CLASSPATH=")]
    return _path_entries(values[-1]) if values else []


def resolve_gradle_classpath(
    ctx: BuildContext,
    init_script: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[list[Path], CommandResult]:
    project_path = gradle_project_path(ctx.repository_root, ctx.module_root)
    command = select_gradle_command(ctx)
    command.extend(["-I", str(init_script), f"-DarrowTargetProject={project_path}"])
    task = "arrowPrintClasspath" if project_path == ":" else f"{project_path}:arrowPrintClasspath"
    command.append(task)
    result = run_process(command, ctx.repository_root, env, timeout_seconds)
    paths = parse_gradle_classpath(result.output)
    if not paths and ctx.module_root != ctx.repository_root:
        fallback = select_gradle_command(ctx)
        fallback.extend(["-p", str(ctx.module_root), "-I", str(init_script), "-DarrowTargetProject=:", "arrowPrintClasspath"])
        fallback_result = run_process(fallback, ctx.module_root, env, timeout_seconds)
        result = CommandResult(
            fallback_result.command,
            fallback_result.exit_code,
            result.output + "\n--- standalone module fallback ---\n" + fallback_result.output,
            round(result.elapsed_seconds + fallback_result.elapsed_seconds, 3),
            result.timed_out or fallback_result.timed_out,
        )
        paths = parse_gradle_classpath(fallback_result.output)
    return _dedupe_existing([*_compiled_class_dirs(ctx.repository_root, ctx.module_root), *paths]), result


def resolve_project_classpath(
    ctx: BuildContext,
    artifact_dir: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[list[Path], CommandResult]:
    if ctx.build_tool == "maven":
        return resolve_maven_classpath(ctx, artifact_dir / "maven_classpath.txt", env, timeout_seconds)
    if ctx.build_tool == "gradle":
        init_script = artifact_dir / "evosuite_classpath.init.gradle"
        init_script.write_text(gradle_init_script_text(), encoding="utf-8")
        return resolve_gradle_classpath(ctx, init_script, env, timeout_seconds)
    return [], CommandResult([], None, "Unsupported or undetected build tool", 0.0)


def find_focal_bytecode(classpath: list[Path], fqcn: str) -> Path | None:
    relative = Path(*fqcn.split(".")).with_suffix(".class")
    for entry in classpath:
        if entry.is_dir() and (entry / relative).is_file():
            return entry / relative
    return None


def sanitize_classpath_for_evosuite(classpath: list[Path], cache_dir: Path) -> list[Path]:
    """Strip classes with bytecode version > 65 (Java 21+) from multi-release JARs so ASM in EvoSuite does not crash."""
    sanitized: list[Path] = []
    clean_dir = cache_dir / ".clean_jars"
    for entry in classpath:
        if not entry.is_file() or not entry.name.lower().endswith(".jar"):
            sanitized.append(entry)
            continue
        try:
            has_unsupported = False
            with zipfile.ZipFile(entry, "r") as zf:
                for info in zf.infolist():
                    if info.filename.endswith(".class"):
                        with zf.open(info) as f:
                            header = f.read(8)
                            if len(header) == 8 and header[:4] == b"\xca\xfe\xba\xbe":
                                major = int.from_bytes(header[6:8], "big")
                                if major > 65:
                                    has_unsupported = True
                                    break
            if not has_unsupported:
                sanitized.append(entry)
                continue

            clean_dir.mkdir(parents=True, exist_ok=True)
            clean_jar = clean_dir / f"clean_{entry.name}"
            if not clean_jar.is_file():
                with zipfile.ZipFile(entry, "r") as zin, zipfile.ZipFile(clean_jar, "w") as zout:
                    for item in zin.infolist():
                        if item.filename.endswith(".class"):
                            data = zin.read(item.filename)
                            if len(data) >= 8 and data[:4] == b"\xca\xfe\xba\xbe":
                                major = int.from_bytes(data[6:8], "big")
                                if major > 65:
                                    continue
                        zout.writestr(item, zin.read(item.filename))
            sanitized.append(clean_jar)
        except Exception:
            sanitized.append(entry)
    return sanitized


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def evosuite_command(
    tools: EvoSuiteTools,
    java_home: str | None,
    fqcn: str,
    classpath: list[Path],
    test_dir: Path,
    report_dir: Path,
    seed: int,
    search_budget: int,
    memory_mb: int,
    criterion: str,
) -> list[str]:
    cmd = [
        java_executable(java_home, "java"),
        f"-Xmx{memory_mb}m",
    ]
    if _is_java_9_plus(java_home):
        cmd.extend(EVOSUITE_ADD_OPENS)
    port = find_free_port()
    cmd.extend([
        "-jar",
        str(tools.evosuite),
        f"-Dprocess_communication_port={port}",
        "-class",
        fqcn,
        "-projectCP",
        os.pathsep.join(str(path) for path in classpath),
        "-criterion",
        criterion,
        "-seed",
        str(seed),
        f"-Dsearch_budget={search_budget}",
        "-Dstopping_condition=MaxTime",
        f"-Dtest_dir={test_dir}",
        f"-Dreport_dir={report_dir}",
        "-Dstatistics_backend=CSV",
        "-Doutput_variables=TARGET_CLASS,criterion,Coverage,Total_Goals,Covered_Goals,Size,Length,Total_Time",
        "-Dassertions=true",
        "-Dminimize=true",
    ])
    return cmd


def generated_java_files(test_dir: Path) -> list[Path]:
    return sorted(path for path in test_dir.rglob("*.java") if path.is_file())


def generated_test_classes(java_files: list[Path]) -> list[str]:
    output: list[str] = []
    for path in java_files:
        if path.stem.endswith("_scaffolding"):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        package_match = re.search(r"(?m)^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", source)
        class_match = re.search(r"\bpublic\s+class\s+([A-Za-z_$][A-Za-z0-9_$]*)", source)
        if class_match:
            package = package_match.group(1) if package_match else ""
            output.append(f"{package}.{class_match.group(1)}" if package else class_match.group(1))
    return output


def count_test_methods(java_files: list[Path]) -> int:
    return sum(len(re.findall(r"(?m)^\s*@Test\b", path.read_text(encoding="utf-8", errors="replace"))) for path in java_files)


def compile_generated_tests(
    java_files: list[Path],
    output_dir: Path,
    classpath: list[Path],
    tools: EvoSuiteTools,
    java_home: str | None,
    cwd: Path,
    timeout_seconds: int,
) -> CommandResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    compile_cp = _dedupe_existing([*classpath, tools.runtime, tools.junit, tools.hamcrest])
    command = [
        java_executable(java_home, "javac"),
        "-encoding",
        "UTF-8",
        "-cp",
        os.pathsep.join(str(path) for path in compile_cp),
        "-d",
        str(output_dir),
        *(str(path) for path in java_files),
    ]
    return run_process(command, cwd, java_environment(java_home), timeout_seconds)


def execute_generated_tests(
    test_classes: list[str],
    compiled_tests: Path,
    classpath: list[Path],
    tools: EvoSuiteTools,
    java_home: str | None,
    cwd: Path,
    timeout_seconds: int,
    memory_mb: int,
) -> CommandResult:
    runtime_cp = _dedupe_existing([compiled_tests, *classpath, tools.runtime, tools.junit, tools.hamcrest])
    command = [
        java_executable(java_home, "java"),
        f"-Xmx{memory_mb}m",
        "-cp",
        os.pathsep.join(str(path) for path in runtime_cp),
        "org.junit.runner.JUnitCore",
        *test_classes,
    ]
    return run_process(command, cwd, java_environment(java_home), timeout_seconds)


def read_evosuite_statistics(report_dir: Path) -> dict[str, str]:
    candidates = sorted(report_dir.rglob("statistics.csv"))
    if not candidates:
        return {}
    with candidates[-1].open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {str(key): str(value) for key, value in rows[-1].items()} if rows else {}


def baseline_is_eligible(state: FailureState | None, origin: FailureOrigin) -> bool:
    return state == FailureState.MODULE_TESTS_PASSED and origin not in {
        FailureOrigin.BUILD_CONFIGURATION,
        FailureOrigin.INFRASTRUCTURE,
    }


def prepare_repository(
    sample: SampleInput,
    cached_repo: Path,
    cache_root: Path,
    checkout_revision: bool,
    attempts: int = 3,
) -> Path:
    """Clone/checkout with bounded retries for transient Git and partial-cache failures."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            repository = clone_repo(sample.repository_url, cached_repo)
            if checkout_revision:
                checkout_dataset_revision(repository, sample.focal_class_path, sample.test_class_path)
            return repository
        except (OSError, subprocess.CalledProcessError) as exc:
            last_error = exc
            if cached_repo.exists():
                safe_remove_tree(cached_repo, cache_root)
            if attempt < attempts:
                time.sleep(attempt)
    assert last_error is not None
    raise last_error


def base_record(run_id: str, manifest_row: dict[str, str], sample: SampleInput, seed: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "manifest_rank": manifest_row.get("rank", ""),
        "project_id": sample.project_id,
        "sample_file": sample.sample_file.name,
        "input_id": sample.input_id,
        "repository_url": sample.repository_url,
        "focal_class": sample.focal_class_name,
        "focal_class_path": sample.focal_class_path,
        "focal_class_fqcn": "",
        "seed": seed,
        "evosuite_version": EVOSUITE_VERSION,
        "status": "TOOL_ERROR",
        "failure_stage": "setup",
        "failure_reason": "",
        "baseline_state": "",
        "baseline_eligible": False,
        "build_tool": "",
        "module_path": "",
        "java_version": "",
        "java_home": "",
        "classpath_resolved": False,
        "focal_bytecode_found": False,
        "generation_exit_code": "",
        "generation_timed_out": False,
        "generated_java_files": 0,
        "generated_test_classes": 0,
        "generated_test_methods": 0,
        "compilation": False,
        "execution_success": False,
        "test_passed": False,
        "valid": False,
        "evosuite_coverage": "",
        "evosuite_total_goals": "",
        "evosuite_covered_goals": "",
        "baseline_elapsed_seconds": "",
        "classpath_elapsed_seconds": "",
        "generation_elapsed_seconds": "",
        "compilation_elapsed_seconds": "",
        "execution_elapsed_seconds": "",
        "elapsed_seconds": "",
        "artifact_dir": "",
        "started_at_utc": utc_now(),
        "finished_at_utc": "",
    }


def run_sample(
    *,
    root: Path,
    dataset_dir: Path,
    manifest_row: dict[str, str],
    config: dict[str, Any],
    run_id: str,
    seeds: list[int],
    tools: EvoSuiteTools,
    output_dir: Path,
    search_budget: int,
    generation_timeout: int,
    build_timeout: int,
    test_timeout: int,
    memory_mb: int,
    criterion: str,
    manual_java_home: str,
    keep_workspace: bool,
    keep_repo_cache: bool,
) -> list[dict[str, Any]]:
    sample = load_manifest_sample(dataset_dir, manifest_row)
    records = [base_record(run_id, manifest_row, sample, seed) for seed in seeds]
    sample_started = time.monotonic()
    cache_root = root / str(config.get("repo", {}).get("repos_dir", "repos"))
    cached_repo = cache_root / sample.project_id
    sample_root = output_dir / "samples" / f"{sample.project_id}_{sample.input_id}"
    baseline_workspace = sample_root / "baseline_workspace"
    sample_root.mkdir(parents=True, exist_ok=True)
    try:
        repository = prepare_repository(
            sample,
            cached_repo,
            cache_root,
            bool(config.get("repo", {}).get("checkout_commit", True)),
        )
        ensure_experiment_workspace(cached_repo=repository, experiment_workspace=baseline_workspace)
        context, module_root = analyze_experiment(
            sample=sample,
            workspace=baseline_workspace,
            run_id=run_id,
            shard_id="evosuite",
            agent_name="evosuite",
            generation_prompt="search-based",
        )
        fqcn = focal_fqcn(sample, baseline_workspace)
        java_selection = resolve_java_home(baseline_workspace, module_root, config, manual_java_home or None)
        java_home = java_selection.java_home or None
        build_cfg = config.get("build", {})
        maven_cfg = build_cfg.get("maven", {})
        build_context = BuildContext(
            repository_root=baseline_workspace,
            module_root=module_root,
            build_tool=context.build_tool,
            generated_test_class_name=f"{sample.focal_class_name}_ESTest",
            generated_test_fqcn=f"{fqcn}_ESTest",
            timeout_seconds=build_timeout,
            prefer_wrapper=bool(build_cfg.get("prefer_wrapper", True)),
            java_home=java_home,
            maven_multi_module_strategy=maven_cfg.get("multi_module_strategy", "module_only"),
            maven_use_also_make=bool(maven_cfg.get("use_also_make", True)),
            maven_fail_if_no_specified_tests=bool(maven_cfg.get("fail_if_no_specified_tests", False)),
        )
        for record in records:
            record.update(
                {
                    "focal_class_fqcn": fqcn,
                    "build_tool": context.build_tool,
                    "module_path": context.module_path,
                    "java_version": context.java_version,
                    "java_home": java_home or "",
                }
            )
        baseline_started = time.monotonic()
        baseline = verify_baseline(build_context)
        baseline_elapsed = round(time.monotonic() - baseline_started, 3)
        (sample_root / "baseline.log").write_text(baseline.raw_output, encoding="utf-8")
        eligible = baseline_is_eligible(baseline.state, baseline.failure_origin)
        for record in records:
            record["baseline_state"] = baseline.state.value if baseline.state else ""
            record["baseline_eligible"] = eligible
            record["baseline_elapsed_seconds"] = baseline_elapsed
        if not eligible:
            reason = baseline.primary_error or baseline.normalized_error_signature or "baseline module tests did not pass"
            for record in records:
                record.update(status="BASELINE_FAILED", failure_stage="baseline", failure_reason=reason)
            return records

        classpath_started = time.monotonic()
        classpath, cp_result = resolve_project_classpath(
            build_context,
            sample_root,
            java_environment(java_home),
            build_timeout,
        )
        classpath_elapsed = round(time.monotonic() - classpath_started, 3)
        (sample_root / "classpath.log").write_text(cp_result.output, encoding="utf-8")
        bytecode = find_focal_bytecode(classpath, fqcn)
        for record in records:
            record["classpath_resolved"] = bool(classpath)
            record["focal_bytecode_found"] = bytecode is not None
            record["classpath_elapsed_seconds"] = classpath_elapsed
        # Some Maven reactors return non-zero because an unrelated upstream
        # module cannot resolve a test-only artifact even after the target
        # module wrote a usable classpath. Continue when the target bytecode and
        # at least one valid classpath entry are present; preserve the command
        # output in classpath.log for audit.
        if not classpath:
            reason = "Không lấy được test runtime classpath" + (f": {cp_result.output[-500:]}" if cp_result.output else "")
            for record in records:
                record.update(status="CLASSPATH_FAILED", failure_stage="classpath", failure_reason=reason)
            return records
        if bytecode is None:
            for record in records:
                record.update(status="CLASS_NOT_COMPILED", failure_stage="classpath", failure_reason=f"Không tìm thấy bytecode {fqcn}")
            return records

        for record in records:
            seed = int(record["seed"])
            seed_root = sample_root / f"seed_{seed}"
            if seed_root.exists():
                safe_remove_tree(seed_root, sample_root)
            test_dir = seed_root / "evosuite-tests"
            report_dir = seed_root / "evosuite-report"
            compiled_dir = seed_root / "compiled-tests"
            seed_root.mkdir(parents=True, exist_ok=True)
            record["artifact_dir"] = str(seed_root)
            evosuite_cp = sanitize_classpath_for_evosuite(classpath, sample_root)
            command = evosuite_command(
                tools,
                java_home,
                fqcn,
                evosuite_cp,
                test_dir,
                report_dir,
                seed,
                search_budget,
                memory_mb,
                criterion,
            )
            generation = run_process(command, module_root, java_environment(java_home), generation_timeout)
            (seed_root / "generation.log").write_text(generation.output, encoding="utf-8")
            (seed_root / "generation_command.json").write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
            record["generation_exit_code"] = generation.exit_code if generation.exit_code is not None else ""
            record["generation_timed_out"] = generation.timed_out
            record["generation_elapsed_seconds"] = generation.elapsed_seconds
            java_files = generated_java_files(test_dir)
            test_classes = generated_test_classes(java_files)
            record["generated_java_files"] = len(java_files)
            record["generated_test_classes"] = len(test_classes)
            record["generated_test_methods"] = count_test_methods(java_files)
            stats = read_evosuite_statistics(report_dir)
            record["evosuite_coverage"] = stats.get("Coverage", "")
            record["evosuite_total_goals"] = stats.get("Total_Goals", "")
            record["evosuite_covered_goals"] = stats.get("Covered_Goals", "")
            if generation.timed_out:
                record.update(status="GENERATION_TIMEOUT", failure_stage="generation", failure_reason="EvoSuite process timeout")
                continue
            if generation.exit_code != 0 or not java_files or not test_classes:
                reason = generation.output[-1000:] if generation.output else "EvoSuite did not produce a test class"
                record.update(status="GENERATION_FAILED", failure_stage="generation", failure_reason=reason)
                continue

            compilation = compile_generated_tests(
                java_files,
                compiled_dir,
                classpath,
                tools,
                java_home,
                module_root,
                build_timeout,
            )
            (seed_root / "compilation.log").write_text(compilation.output, encoding="utf-8")
            record["compilation_elapsed_seconds"] = compilation.elapsed_seconds
            record["compilation"] = compilation.exit_code == 0 and not compilation.timed_out
            if not record["compilation"]:
                status = "COMPILE_TIMEOUT" if compilation.timed_out else "COMPILE_FAILED"
                record.update(status=status, failure_stage="compilation", failure_reason=compilation.output[-1000:])
                continue

            execution = execute_generated_tests(
                test_classes,
                compiled_dir,
                classpath,
                tools,
                java_home,
                module_root,
                test_timeout,
                memory_mb,
            )
            (seed_root / "execution.log").write_text(execution.output, encoding="utf-8")
            record["execution_elapsed_seconds"] = execution.elapsed_seconds
            record["execution_success"] = not execution.timed_out and execution.exit_code is not None
            record["test_passed"] = execution.exit_code == 0 and not execution.timed_out
            record["valid"] = bool(record["compilation"] and record["test_passed"])
            if record["valid"]:
                record.update(status="VALID", failure_stage="", failure_reason="")
            else:
                status = "TEST_TIMEOUT" if execution.timed_out else "TEST_FAILED"
                record.update(status=status, failure_stage="execution", failure_reason=execution.output[-1000:])
        return records
    except Exception as exc:  # Keep every manifest sample in the denominator/audit log.
        for record in records:
            record.update(status="TOOL_ERROR", failure_stage=record.get("failure_stage") or "setup", failure_reason=f"{type(exc).__name__}: {exc}")
        return records
    finally:
        elapsed = round(time.monotonic() - sample_started, 3)
        for record in records:
            record["elapsed_seconds"] = elapsed
            record["finished_at_utc"] = utc_now()
        cleanup_warnings: list[str] = []
        if not keep_workspace and baseline_workspace.exists():
            try:
                safe_remove_tree(baseline_workspace, sample_root)
            except Exception as exc:
                cleanup_warnings.append(f"workspace cleanup: {type(exc).__name__}: {exc}")
        if not keep_repo_cache and cached_repo.exists():
            try:
                safe_remove_tree(cached_repo, cache_root)
            except Exception as exc:
                cleanup_warnings.append(f"repo-cache cleanup: {type(exc).__name__}: {exc}")
        if cleanup_warnings:
            warning = "; ".join(cleanup_warnings)
            for record in records:
                record["cleanup_warning"] = warning


def experiment_key(record: dict[str, Any]) -> tuple[str, str, int]:
    return str(record.get("project_id", "")), str(record.get("sample_file", "")), int(record.get("seed", 0))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in records if row.get("baseline_eligible") is True]
    total = len(eligible)
    compiled = sum(row.get("compilation") is True for row in eligible)
    executed = sum(row.get("execution_success") is True for row in eligible)
    passed = sum(row.get("test_passed") is True for row in eligible)
    valid = sum(row.get("valid") is True for row in eligible)

    def percent(value: int, denominator: int) -> float | str:
        return round(value * 100 / denominator, 2) if denominator else ""

    statuses: dict[str, int] = {}
    for row in records:
        status = str(row.get("status", "UNKNOWN"))
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "records_total": len(records),
        "baseline_invalid_excluded_n": len(records) - total,
        "baseline_valid_evaluable_n": total,
        "compilation_success_n": compiled,
        "CSR_pct": percent(compiled, total),
        "execution_success_n": executed,
        "ESR_pct": percent(executed, total),
        "test_pass_n": passed,
        "TPR_pct": percent(passed, total),
        "valid_test_n": valid,
        "valid_rate_pct": percent(valid, total),
        "end_to_end_valid_rate_pct": percent(valid, len(records)),
        "status_counts": statuses,
        "note": (
            "CSR/ESR/TPR/valid_rate_pct use baseline_valid_evaluable_n as the denominator; "
            "end_to_end_valid_rate_pct uses records_total. evosuite_coverage is EvoSuite search "
            "coverage, not JaCoCo/PIT and must not replace paper quality metrics."
        ),
    }
