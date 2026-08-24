from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .log_parser import parse_verification_output
from .models import FailureOrigin, FailureState, VerificationResult


@dataclass
class BuildContext:
    repository_root: Path
    module_root: Path
    build_tool: str
    generated_test_class_name: str
    generated_test_fqcn: str
    timeout_seconds: int = 900
    prefer_wrapper: bool = True
    java_home: str | None = None
    maven_multi_module_strategy: str = "module_only"
    maven_use_also_make: bool = True
    maven_fail_if_no_specified_tests: bool = False


def _is_windows() -> bool:
    return platform.system().lower().startswith("win")


def _find_upwards(start: Path, stop: Path, names: list[str]) -> Path | None:
    current = start.resolve()
    stop = stop.resolve()
    while True:
        for name in names:
            candidate = current / name
            if candidate.is_file():
                return candidate
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _maven_declared_module_paths(pom: Path) -> list[Path]:
    try:
        root = ET.parse(pom).getroot()
    except (ET.ParseError, OSError):
        return []
    modules: list[Path] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "module" or not element.text:
            continue
        value = element.text.strip()
        if value and "${" not in value:
            modules.append((pom.parent / value).resolve())
    return modules


def _maven_module_is_in_root_reactor(ctx: BuildContext) -> bool:
    repository_root = ctx.repository_root.resolve()
    target = ctx.module_root.resolve()
    pending = [repository_root]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for module in _maven_declared_module_paths(current / "pom.xml"):
            try:
                module.relative_to(repository_root)
            except ValueError:
                continue
            if module == target:
                return True
            if (module / "pom.xml").is_file():
                pending.append(module)
    return False


def _maven_archetype_template_failure(ctx: BuildContext) -> VerificationResult | None:
    if ctx.build_tool != "maven":
        return None
    try:
        relative = ctx.module_root.resolve().relative_to(ctx.repository_root.resolve())
    except ValueError:
        return None
    relative_parts = tuple(part.lower() for part in relative.parts)
    marker = ("src", "main", "resources", "archetype-resources")
    is_template = any(
        relative_parts[index : index + len(marker)] == marker
        for index in range(len(relative_parts) - len(marker) + 1)
    )
    if not is_template:
        return None
    message = (
        "Maven archetype template sources are not a directly buildable reactor module; "
        "materialize the archetype before compiling generated tests"
    )
    return VerificationResult(
        state=FailureState.RUNTIME_FAILED,
        failure_origin=FailureOrigin.BUILD_CONFIGURATION,
        exit_code=None,
        raw_output=message + f": {ctx.module_root}",
        primary_error="maven_archetype_template_not_materialized",
        normalized_error_signature="maven_archetype_template_not_materialized",
        error_signatures=["maven_archetype_template_not_materialized"],
        build_skipped=True,
        tool_name="maven",
        command=[],
    )


def select_maven_command(ctx: BuildContext) -> list[str]:
    wrapper_names = ["mvnw.cmd"] if _is_windows() else ["mvnw"]
    wrapper = _find_upwards(ctx.module_root, ctx.repository_root, wrapper_names) if ctx.prefer_wrapper else None
    if wrapper:
        command = [str(wrapper)]
    else:
        exe = shutil.which("mvn.cmd") or shutil.which("mvn") if _is_windows() else shutil.which("mvn")
        command = [exe or ("mvn.cmd" if _is_windows() else "mvn")]
    use_root_reactor = (
        ctx.maven_multi_module_strategy == "root_with_pl_am"
        and (ctx.repository_root / "pom.xml").is_file()
        and (
            ctx.module_root.resolve() == ctx.repository_root.resolve()
            or _maven_module_is_in_root_reactor(ctx)
        )
    )
    if use_root_reactor:
        command.extend(["-f", str(ctx.repository_root / "pom.xml")])
        if ctx.module_root.resolve() != ctx.repository_root.resolve():
            module_selector = ctx.module_root.resolve().relative_to(ctx.repository_root.resolve()).as_posix()
            command.extend(["-pl", module_selector])
            if ctx.maven_use_also_make:
                command.append("-am")
    else:
        pom = ctx.module_root / "pom.xml"
        if pom.is_file():
            command.extend(["-f", str(pom)])
    return command


def select_gradle_command(ctx: BuildContext) -> list[str]:
    wrapper_names = ["gradlew.bat"] if _is_windows() else ["gradlew"]
    wrapper = _find_upwards(ctx.module_root, ctx.repository_root, wrapper_names) if ctx.prefer_wrapper else None
    if wrapper:
        return [str(wrapper)]
    exe = shutil.which("gradle") or "gradle"
    return [exe]


def _gradle_task_path(ctx: BuildContext) -> str:
    if ctx.module_root == ctx.repository_root:
        return "test"
    relative = ctx.module_root.relative_to(ctx.repository_root)
    return ":" + ":".join(relative.parts) + ":test"


def _gradle_project_path(ctx: BuildContext) -> str:
    if ctx.module_root.resolve() == ctx.repository_root.resolve():
        return ""
    relative = ctx.module_root.resolve().relative_to(ctx.repository_root.resolve())
    return ":" + ":".join(relative.parts)


def _gradle_settings_text(repository_root: Path) -> str:
    for name in ("settings.gradle", "settings.gradle.kts"):
        path = repository_root / name
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _gradle_module_is_in_root_settings(ctx: BuildContext) -> bool:
    project_path = _gradle_project_path(ctx)
    if not project_path:
        return True
    settings = _gradle_settings_text(ctx.repository_root)
    if not settings:
        return False
    without_leading_colon = project_path.lstrip(":")
    quoted_variants = (
        f"'{project_path}'",
        f'"{project_path}"',
        f"'{without_leading_colon}'",
        f'"{without_leading_colon}"',
    )
    return any(variant in settings for variant in quoted_variants)


def _gradle_test_invocation(ctx: BuildContext) -> tuple[list[str], Path]:
    command = select_gradle_command(ctx)
    if ctx.module_root.resolve() == ctx.repository_root.resolve() or _gradle_module_is_in_root_settings(ctx):
        command.append(_gradle_task_path(ctx))
        return command, ctx.repository_root
    command.extend(["-p", str(ctx.module_root), "test"])
    return command, ctx.module_root


def _gradle_standalone_test_invocation(ctx: BuildContext, target_only: bool) -> tuple[list[str], Path]:
    command = select_gradle_command(ctx)
    command.extend(["-p", str(ctx.module_root), "test"])
    if target_only:
        command.extend(["--tests", ctx.generated_test_fqcn])
    return command, ctx.module_root


def _gradle_project_not_found(result: VerificationResult) -> bool:
    return bool(
        re.search(
            r"project\s+['\"][^'\"]+['\"]\s+not\s+found\s+in\s+root\s+project",
            result.raw_output or "",
            flags=re.IGNORECASE,
        )
    )


def _run_with_gradle_project_fallback(
    ctx: BuildContext,
    command: list[str],
    cwd: Path,
    target_only: bool,
) -> VerificationResult:
    result = run_command(ctx, command, cwd, "gradle", target_only=target_only)
    if ctx.module_root.resolve() == ctx.repository_root.resolve() or not _gradle_project_not_found(result):
        return result

    fallback_command, fallback_cwd = _gradle_standalone_test_invocation(ctx, target_only)
    if fallback_command == command and fallback_cwd.resolve() == cwd.resolve():
        return result

    print(
        f"[{time.strftime('%H:%M:%S')}] BUILD gradle module is not registered in root settings; "
        f"retry with -p {ctx.module_root}",
        flush=True,
    )
    fallback = run_command(ctx, fallback_command, fallback_cwd, "gradle", target_only=target_only)
    fallback.raw_output = (
        result.raw_output
        + "\n\n--- ARROW Gradle standalone-module fallback ---\n\n"
        + fallback.raw_output
    )
    return fallback


def _maven_selected_project_not_found(result: VerificationResult) -> bool:
    return bool(
        re.search(
            r"could not find the selected project in the reactor|selected project .* not found in the reactor",
            result.raw_output or "",
            flags=re.IGNORECASE,
        )
    )


def _maven_standalone_test_invocation(ctx: BuildContext, target_only: bool) -> tuple[list[str], Path]:
    """Build directly from the module POM when root ``-pl`` is rejected.

    Some repositories have a root POM that looks like an aggregator to the
    static analyser but cannot resolve the module selector Maven expects.
    Retrying only on Maven's explicit reactor-selector error is safe and keeps
    the normal root-with-``-am`` path for repositories that need sibling builds.
    """
    wrapper_names = ["mvnw.cmd"] if _is_windows() else ["mvnw"]
    wrapper = _find_upwards(ctx.module_root, ctx.repository_root, wrapper_names) if ctx.prefer_wrapper else None
    if wrapper:
        command = [str(wrapper)]
    else:
        exe = shutil.which("mvn.cmd") or shutil.which("mvn") if _is_windows() else shutil.which("mvn")
        command = [exe or ("mvn.cmd" if _is_windows() else "mvn")]
    pom = ctx.module_root / "pom.xml"
    if pom.is_file():
        command.extend(["-f", str(pom)])
    command.append("-DfailIfNoTests=false")
    if target_only:
        if not ctx.maven_fail_if_no_specified_tests:
            command.append("-Dsurefire.failIfNoSpecifiedTests=false")
        command.append(f"-Dtest={ctx.generated_test_class_name}")
    command.append("test")
    return command, ctx.module_root


def _run_with_maven_reactor_fallback(
    ctx: BuildContext,
    command: list[str],
    cwd: Path,
    target_only: bool,
) -> VerificationResult:
    result = run_command(ctx, command, cwd, "maven", target_only=target_only)
    if not _maven_selected_project_not_found(result):
        return result
    fallback_command, fallback_cwd = _maven_standalone_test_invocation(ctx, target_only)
    if fallback_command == command and fallback_cwd.resolve() == cwd.resolve():
        return result
    print(
        f"[{time.strftime('%H:%M:%S')}] BUILD maven root reactor rejected module selector; "
        f"retry with module POM {ctx.module_root / 'pom.xml'}",
        flush=True,
    )
    fallback = run_command(ctx, fallback_command, fallback_cwd, "maven", target_only=target_only)
    fallback.raw_output = (
        result.raw_output
        + "\n\n--- ARROW Maven standalone-module fallback ---\n\n"
        + fallback.raw_output
    )
    return fallback


def target_test_command(ctx: BuildContext) -> tuple[str, list[str], Path]:
    if ctx.build_tool == "maven":
        command = select_maven_command(ctx)
        command.append("-DfailIfNoTests=false")
        if not ctx.maven_fail_if_no_specified_tests:
            command.append("-Dsurefire.failIfNoSpecifiedTests=false")
        command.extend([f"-Dtest={ctx.generated_test_class_name}", "test"])
        return "maven", command, ctx.module_root
    if ctx.build_tool == "gradle":
        command, cwd = _gradle_test_invocation(ctx)
        command.extend(["--tests", ctx.generated_test_fqcn])
        return "gradle", command, cwd
    return "unknown", [], ctx.module_root


def module_test_command(ctx: BuildContext) -> tuple[str, list[str], Path]:
    if ctx.build_tool == "maven":
        command = select_maven_command(ctx)
        command.extend(["-DfailIfNoTests=false", "test"])
        return "maven", command, ctx.module_root
    if ctx.build_tool == "gradle":
        command, cwd = _gradle_test_invocation(ctx)
        return "gradle", command, cwd
    return "unknown", [], ctx.module_root


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if _is_windows():
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, text=True)
    else:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


def _clear_test_reports(module_root: Path) -> None:
    for path in (
        module_root / "target" / "surefire-reports",
        module_root / "target" / "failsafe-reports",
        module_root / "build" / "test-results" / "test",
    ):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def run_command(ctx: BuildContext, command: list[str], cwd: Path, tool_name: str, target_only: bool) -> VerificationResult:
    if not command:
        return VerificationResult(
            state=FailureState.TOOL_ERROR,
            failure_origin=FailureOrigin.INFRASTRUCTURE,
            raw_output="Unsupported or undetected build tool",
            primary_error="unsupported build tool",
            normalized_error_signature="tool_error:unsupported_build_tool",
            error_signatures=["tool_error:unsupported_build_tool"],
            tool_name=tool_name,
            command=command,
        )
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if _is_windows() else 0
    preexec_fn = None if _is_windows() else os.setsid
    env = os.environ.copy()
    if ctx.java_home:
        env["JAVA_HOME"] = ctx.java_home
        env["PATH"] = str(Path(ctx.java_home) / "bin") + os.pathsep + env.get("PATH", "")
    print(f"[{time.strftime('%H:%M:%S')}] BUILD {tool_name} cwd={cwd} java_home={ctx.java_home or 'default'}", flush=True)
    print(f"[{time.strftime('%H:%M:%S')}] BUILD command={' '.join(str(part) for part in command)}", flush=True)
    try:
        _clear_test_reports(ctx.module_root)
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            creationflags=creationflags,
            preexec_fn=preexec_fn,
        )
        try:
            stdout, stderr = proc.communicate(timeout=ctx.timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            stdout, stderr = proc.communicate()
            timed_out = True
        raw_output = (stdout or "") + ("\n" if stdout and stderr else "") + (stderr or "")
        result = parse_verification_output(
            raw_output=raw_output,
            exit_code=proc.returncode,
            timed_out=timed_out,
            tool_name=tool_name,
            command=command,
            module_root=ctx.module_root,
            generated_test_class=ctx.generated_test_class_name,
            target_only=target_only,
        )
        print(
            f"[{time.strftime('%H:%M:%S')}] BUILD result state={result.state.value if result.state else 'UNKNOWN'} origin={result.failure_origin.value} exit={proc.returncode} error={(result.primary_error or result.normalized_error_signature)[:180]}",
            flush=True,
        )
        return result
    except OSError as exc:
        return VerificationResult(
            state=FailureState.TOOL_ERROR,
            failure_origin=FailureOrigin.INFRASTRUCTURE,
            exit_code=None,
            raw_output=str(exc),
            primary_error=str(exc),
            normalized_error_signature="tool_error:" + type(exc).__name__.lower(),
            error_signatures=["tool_error:" + type(exc).__name__.lower()],
            tool_name=tool_name,
            command=command,
        )


def verify_target_test(ctx: BuildContext) -> VerificationResult:
    preflight_failure = _maven_archetype_template_failure(ctx)
    if preflight_failure is not None:
        return preflight_failure
    tool, command, cwd = target_test_command(ctx)
    if tool == "gradle":
        return _run_with_gradle_project_fallback(ctx, command, cwd, target_only=True)
    if tool == "maven":
        return _run_with_maven_reactor_fallback(ctx, command, cwd, target_only=True)
    return run_command(ctx, command, cwd, tool, target_only=True)


def verify_module_tests(ctx: BuildContext) -> VerificationResult:
    preflight_failure = _maven_archetype_template_failure(ctx)
    if preflight_failure is not None:
        return preflight_failure
    tool, command, cwd = module_test_command(ctx)
    if tool == "gradle":
        return _run_with_gradle_project_fallback(ctx, command, cwd, target_only=False)
    if tool == "maven":
        return _run_with_maven_reactor_fallback(ctx, command, cwd, target_only=False)
    return run_command(ctx, command, cwd, tool, target_only=False)


def verify_baseline(ctx: BuildContext) -> VerificationResult:
    return verify_module_tests(ctx)
