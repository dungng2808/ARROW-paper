from __future__ import annotations

import os
import stat
import subprocess

import pytest

from src.build_runner import (
    BuildContext,
    module_test_command,
    select_gradle_command,
    select_maven_command,
    target_test_command,
    verify_module_tests,
)
from src.models import FailureOrigin, FailureState, VerificationResult
from src import repo_manager
from src.repo_manager import checkout_dataset_revision, copy_isolated_workspace, safe_remove_tree
from src.test_writer import JavaValidationError, validate_java_candidate, write_owned_generated_test


def test_validate_java_strips_fence_and_checks_package_and_class():
    code, digest = validate_java_candidate(
        """```java
package demo;
public class FooTest_1234abcd {
}
```""",
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
    )
    assert code.startswith("package demo;")
    assert digest


def test_validate_java_normalizes_crlf_and_lf_to_same_code_and_hash():
    lf = "package demo;\npublic class FooTest_1234abcd {\n}\n"
    crlf = lf.replace("\n", "\r\n")

    lf_code, lf_hash = validate_java_candidate(
        lf,
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
    )
    crlf_code, crlf_hash = validate_java_candidate(
        crlf,
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
    )

    assert crlf_code == lf_code
    assert crlf_hash == lf_hash
    assert "\r" not in crlf_code


def test_validate_java_rejects_truncated_compilation_unit_before_build():
    with pytest.raises(JavaValidationError, match="unclosed delimiter"):
        validate_java_candidate(
            "package demo;\npublic class FooTest_1234abcd {\n    void test() {\n",
            expected_package="demo",
            expected_class_name="FooTest_1234abcd",
        )


def test_validate_java_ignores_delimiters_inside_literals_and_comments():
    code, _digest = validate_java_candidate(
        '''package demo;
public class FooTest_1234abcd {
    String value = "not structural: })]";
    // not structural: {
    /* not structural: ( [ { */
}
''',
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
    )

    assert "not structural" in code


def test_validate_java_rejects_unterminated_block_comment():
    with pytest.raises(JavaValidationError, match="unterminated block comment"):
        validate_java_candidate(
            "package demo;\npublic class FooTest_1234abcd { /* truncated\n",
            expected_package="demo",
            expected_class_name="FooTest_1234abcd",
        )


def test_validate_java_normalizes_common_generated_class_name_variants():
    code, _digest = validate_java_candidate(
        """package demo;
public class FooGeneratedTest_1234abcd {
}
""",
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
    )

    assert "public class FooTest_1234abcd" in code


def test_validate_java_normalizes_unhashed_base_test_class_name():
    code, _digest = validate_java_candidate(
        """package demo;
public class FooTest {
}
""",
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
    )

    assert "public class FooTest_1234abcd" in code
    assert "public class FooTest {" not in code


def test_validate_java_adds_missing_junit5_imports():
    code, _digest = validate_java_candidate(
        """package demo;

import static org.junit.jupiter.api.Assertions.*;

public class FooTest_1234abcd {
    @BeforeEach
    public void setUp() {
    }

    @Test
    public void works() {
        assertEquals(1, 1);
    }
}
""",
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
        testing_framework="junit5",
    )

    assert "import org.junit.jupiter.api.BeforeEach;" in code
    assert "import org.junit.jupiter.api.Test;" in code


def test_validate_java_adds_high_confidence_common_jdk_imports():
    code, _digest = validate_java_candidate(
        """package demo;
public class FooTest_1234abcd {
    Object values = Arrays.asList(1, 2);
    Object empty = Collections.emptyList();
    Object order = Comparator.comparingInt(Object::hashCode);
    Class<?> missing = NoSuchElementException.class;
}
""",
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
    )

    assert "import java.util.Arrays;" in code
    assert "import java.util.Collections;" in code
    assert "import java.util.Comparator;" in code
    assert "import java.util.NoSuchElementException;" in code


def test_validate_java_normalizes_junit4_lifecycle_annotations():
    code, _digest = validate_java_candidate(
        """package demo;

public class FooTest_1234abcd {
    @BeforeEach
    public void setUp() {
    }

    @Test
    public void works() {
        assertEquals(1, 1);
    }
}
""",
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
        testing_framework="junit4",
    )

    assert "@Before" in code
    assert "@BeforeEach" not in code
    assert "import org.junit.Before;" in code
    assert "import org.junit.Test;" in code
    assert "import static org.junit.Assert.*;" in code


def test_validate_java_converts_junit5_imports_to_junit4_when_context_is_junit4():
    code, _digest = validate_java_candidate(
        """package demo;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

public class FooTest_1234abcd {
    @BeforeEach
    public void setUp() {
    }

    @Test
    public void works() {
        assertEquals(1, 1);
    }
}
""",
        expected_package="demo",
        expected_class_name="FooTest_1234abcd",
        testing_framework="junit4",
    )

    assert "junit.jupiter" not in code
    assert "@Before" in code
    assert "import org.junit.Before;" in code
    assert "import org.junit.Test;" in code
    assert "import static org.junit.Assert.*;" in code


def test_validate_java_rejects_unrelated_class_name():
    with pytest.raises(JavaValidationError):
        validate_java_candidate(
            "package demo;\npublic class BarGeneratedTest_1234abcd {}\n",
            expected_package="demo",
            expected_class_name="FooTest_1234abcd",
        )


def test_invalid_diff_is_rejected():
    with pytest.raises(JavaValidationError):
        validate_java_candidate(
            "--- a/pom.xml\n+++ b/pom.xml",
            expected_package="demo",
            expected_class_name="FooTest",
        )


def test_human_written_test_is_not_overwritten(tmp_path):
    test_path = tmp_path / "src" / "test" / "java" / "demo" / "FooTest.java"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("package demo; public class FooTest {}", encoding="utf-8")
    with pytest.raises(JavaValidationError):
        write_owned_generated_test(
            experiment_id="exp",
            workspace=tmp_path,
            generated_test_path=test_path,
            generated_test_class_name="FooTest",
            code="package demo; public class FooTest {}",
        )


def test_path_traversal_outside_workspace_is_rejected(tmp_path):
    with pytest.raises(JavaValidationError):
        write_owned_generated_test(
            experiment_id="exp",
            workspace=tmp_path / "workspace",
            generated_test_path=tmp_path / "outside.java",
            generated_test_class_name="FooTest",
            code="package demo; public class FooTest {}",
        )


def test_maven_wrapper_selection_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("src.build_runner._is_windows", lambda: True)
    repo = tmp_path / "repo"
    module = repo / "module"
    module.mkdir(parents=True)
    (repo / "mvnw.cmd").write_text("@echo off", encoding="utf-8")
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    ctx = BuildContext(repo, module, "maven", "FooTest", "demo.FooTest")
    command = select_maven_command(ctx)
    assert command[0].endswith("mvnw.cmd")
    assert "-f" in command


def test_maven_root_with_pl_am_for_submodule(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "mysql" / "codec"
    module.mkdir(parents=True)
    (repo / "pom.xml").write_text(
        "<project><modules><module>mysql/codec</module></modules></project>",
        encoding="utf-8",
    )
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    ctx = BuildContext(
        repo,
        module,
        "maven",
        "FooTest",
        "demo.FooTest",
        maven_multi_module_strategy="root_with_pl_am",
        maven_use_also_make=True,
    )
    command = select_maven_command(ctx)
    assert command[command.index("-f") + 1] == str(repo / "pom.xml")
    assert command[command.index("-pl") + 1] == "mysql/codec"
    assert "-am" in command


def test_maven_unregistered_nested_project_uses_its_own_pom(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "examples" / "standalone"
    module.mkdir(parents=True)
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    ctx = BuildContext(
        repo,
        module,
        "maven",
        "FooTest",
        "demo.FooTest",
        maven_multi_module_strategy="root_with_pl_am",
    )

    command = select_maven_command(ctx)

    assert command[command.index("-f") + 1] == str(module / "pom.xml")
    assert "-pl" not in command
    assert "-am" not in command


def test_maven_archetype_template_stops_before_running_maven(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "hello-world" / "src" / "main" / "resources" / "archetype-resources"
    module.mkdir(parents=True)
    (repo / "pom.xml").write_text(
        "<project><modules><module>hello-world</module></modules></project>",
        encoding="utf-8",
    )
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    ctx = BuildContext(
        repo,
        module,
        "maven",
        "FooTest",
        "demo.FooTest",
        maven_multi_module_strategy="root_with_pl_am",
    )

    result = verify_module_tests(ctx)

    assert result.state == FailureState.RUNTIME_FAILED
    assert result.failure_origin == FailureOrigin.BUILD_CONFIGURATION
    assert result.build_skipped is True
    assert result.normalized_error_signature == "maven_archetype_template_not_materialized"


def test_maven_target_test_ignores_no_matching_tests_in_also_make_modules(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "service"
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
        maven_fail_if_no_specified_tests=False,
    )
    _tool, command, _cwd = target_test_command(ctx)
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in command
    assert "-Dtest=GeneratedTest" in command


def test_gradle_wrapper_selection_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("src.build_runner._is_windows", lambda: True)
    repo = tmp_path / "repo"
    module = repo / "module"
    module.mkdir(parents=True)
    (repo / "gradlew.bat").write_text("@echo off", encoding="utf-8")
    ctx = BuildContext(repo, module, "gradle", "FooTest", "demo.FooTest")
    command = select_gradle_command(ctx)
    assert command[0].endswith("gradlew.bat")


def test_gradle_nested_module_uses_project_dir_when_not_in_root_settings(monkeypatch, tmp_path):
    monkeypatch.setattr("src.build_runner._is_windows", lambda: False)
    repo = tmp_path / "workspace"
    module = repo / "strategies"
    module.mkdir(parents=True)
    (repo / "gradlew").write_text("#!/bin/sh", encoding="utf-8")
    (repo / "settings.gradle").write_text("rootProject.name = 'workspace'", encoding="utf-8")
    (module / "build.gradle").write_text("plugins { id 'java' }", encoding="utf-8")
    ctx = BuildContext(repo, module, "gradle", "FooTest", "demo.FooTest")

    _tool, command, cwd = module_test_command(ctx)

    assert command == [str(repo / "gradlew"), "-p", str(module), "test"]
    assert cwd == module


def test_gradle_missing_root_project_retries_as_standalone_module(monkeypatch, tmp_path):
    monkeypatch.setattr("src.build_runner._is_windows", lambda: False)
    repo = tmp_path / "workspace"
    module = repo / "strategies"
    module.mkdir(parents=True)
    (repo / "gradlew").write_text("#!/bin/sh", encoding="utf-8")
    (repo / "settings.gradle").write_text("include 'strategies'", encoding="utf-8")
    (module / "build.gradle").write_text("plugins { id 'java' }", encoding="utf-8")
    ctx = BuildContext(repo, module, "gradle", "FooTest", "demo.FooTest")
    calls = []

    def fake_run_command(_ctx, command, cwd, tool_name, target_only):
        calls.append((command, cwd, tool_name, target_only))
        if len(calls) == 1:
            return VerificationResult(
                state=FailureState.COMPILE_FAILED,
                failure_origin=FailureOrigin.BUILD_CONFIGURATION,
                exit_code=1,
                raw_output="Project 'strategies' not found in root project 'workspace'.",
            )
        return VerificationResult(
            state=FailureState.MODULE_TESTS_PASSED,
            failure_origin=FailureOrigin.UNKNOWN,
            exit_code=0,
            raw_output="BUILD SUCCESSFUL",
        )

    monkeypatch.setattr("src.build_runner.run_command", fake_run_command)

    result = verify_module_tests(ctx)

    assert calls[0][0] == [str(repo / "gradlew"), ":strategies:test"]
    assert calls[0][1] == repo
    assert calls[1][0] == [str(repo / "gradlew"), "-p", str(module), "test"]
    assert calls[1][1] == module
    assert result.state == FailureState.MODULE_TESTS_PASSED
    assert "standalone-module fallback" in result.raw_output


def test_safe_remove_tree_removes_only_inside_allowed_root(tmp_path):
    allowed_root = tmp_path / "runs"
    target = allowed_root / "exp" / "workspace"
    target.mkdir(parents=True)
    (target / "file.txt").write_text("temporary checkout", encoding="utf-8")

    assert safe_remove_tree(target, allowed_root) is True
    assert not target.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError):
        safe_remove_tree(outside, allowed_root)
    assert outside.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions are required")
def test_safe_remove_tree_recovers_from_unreadable_directory(tmp_path):
    allowed_root = tmp_path / "runs"
    target = allowed_root / "exp" / "workspace"
    locked = target / "locked"
    locked.mkdir(parents=True)
    (locked / "file.txt").write_text("temporary checkout", encoding="utf-8")
    locked.chmod(0)

    try:
        assert safe_remove_tree(target, allowed_root) is True
        assert not target.exists()
    finally:
        if locked.exists():
            locked.chmod(0o700)


def test_workspace_copy_preserves_source_package_named_build_but_skips_gradle_outputs(tmp_path):
    source = tmp_path / "cached-repo"
    destination = tmp_path / "experiment" / "workspace"
    source.mkdir()
    (source / "build.gradle").write_text("plugins { id 'java' }", encoding="utf-8")
    gradle_source = source / "buildSrc" / "src" / "main" / "groovy" / "com" / "linkedin" / "gradle" / "build"
    gradle_source.mkdir(parents=True)
    (source / "buildSrc" / "build.gradle").write_text("plugins { id 'groovy' }", encoding="utf-8")
    (gradle_source / "DistributeTask.groovy").write_text(
        "package com.linkedin.gradle.build\nclass DistributeTask {}\n",
        encoding="utf-8",
    )
    root_output = source / "build"
    module_output = source / "buildSrc" / "build"
    root_output.mkdir()
    module_output.mkdir()
    (root_output / "generated.bin").write_bytes(b"generated")
    (module_output / "generated.bin").write_bytes(b"generated")

    copy_isolated_workspace(source, destination)

    assert (destination / gradle_source.relative_to(source) / "DistributeTask.groovy").is_file()
    assert not (destination / "build").exists()
    assert not (destination / "buildSrc" / "build").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation requires additional privileges")
def test_workspace_copy_preserves_broken_symlink(tmp_path):
    source = tmp_path / "cached-repo"
    destination = tmp_path / "experiment" / "workspace"
    source.mkdir()
    (source / "pom.xml").write_text("<project/>", encoding="utf-8")
    (source / ".sdkmanrc").symlink_to("missing-sdkmanrc")

    copy_isolated_workspace(source, destination)

    assert (destination / ".sdkmanrc").is_symlink()
    assert os.readlink(destination / ".sdkmanrc") == "missing-sdkmanrc"


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX executable bits")
def test_workspace_copy_marks_build_wrappers_executable(tmp_path):
    source = tmp_path / "cached-repo"
    destination = tmp_path / "experiment" / "workspace"
    source.mkdir()
    for name in ("mvnw", "gradlew"):
        wrapper = source / name
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    copy_isolated_workspace(source, destination)

    for name in ("mvnw", "gradlew"):
        mode = (destination / name).stat().st_mode
        assert mode & stat.S_IXUSR
        assert mode & stat.S_IXGRP
        assert mode & stat.S_IXOTH


def test_workspace_copy_includes_git_metadata_when_gradle_build_uses_git(tmp_path):
    source = tmp_path / "cached-repo"
    destination = tmp_path / "experiment" / "workspace"
    git_dir = source / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="utf-8")
    (source / "build.gradle").write_text(
        "exec { commandLine 'git', 'describe', '--tags' }\n",
        encoding="utf-8",
    )

    copy_isolated_workspace(source, destination)

    assert (destination / ".git" / "config").is_file()


def test_git_dependent_shallow_repo_fetches_history_and_tags(monkeypatch, tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "build.gradle").write_text(
        "exec { commandLine 'git', 'describe', '--tags' }\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return repo_manager.subprocess.CompletedProcess(command, 0, stdout="true\n", stderr="")

    monkeypatch.setattr(repo_manager.subprocess, "run", fake_run)

    repo_manager._ensure_git_history_for_build(repository)

    assert ["git", "rev-parse", "--is-shallow-repository"] in calls
    assert ["git", "fetch", "--unshallow", "--tags"] in calls


def test_checkout_dataset_revision_restores_historical_focal_path(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.email", "arrow@example.test")
    git("config", "user.name", "ARROW Test")
    focal = repository / "client" / "src" / "main" / "java" / "demo" / "Focal.java"
    test = repository / "client" / "src" / "test" / "java" / "demo" / "FocalTest.java"
    focal.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    focal.write_text("package demo; public class Focal {}\n", encoding="utf-8")
    test.write_text("package demo; public class FocalTest {}\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "dataset revision")
    dataset_revision = git("rev-parse", "HEAD")
    focal.unlink()
    git("add", "-A")
    git("commit", "-m", "remove focal")
    assert not focal.exists()

    checkout = checkout_dataset_revision(
        repository,
        "client/src/main/java/demo/Focal.java",
        "client/src/test/java/demo/FocalTest.java",
    )

    assert checkout == dataset_revision
    assert focal.is_file()
    assert test.is_file()


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX executable bits")
def test_checkout_dataset_revision_discards_cache_mode_changes(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.email", "arrow@example.test")
    git("config", "user.name", "ARROW Test")
    focal = repository / "src" / "main" / "java" / "demo" / "Focal.java"
    test = repository / "src" / "test" / "java" / "demo" / "FocalTest.java"
    wrapper = repository / "mvnw"
    focal.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    focal.write_text("package demo; public class Focal {}\n", encoding="utf-8")
    test.write_text("package demo; public class FocalTest {}\n", encoding="utf-8")
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o644)
    git("add", ".")
    git("commit", "-m", "dataset revision")
    dataset_revision = git("rev-parse", "HEAD")
    focal.unlink()
    git("add", "-A")
    git("commit", "-m", "remove focal")

    # Simulate a prior run making the tracked wrapper executable in the cache.
    wrapper.chmod(0o755)
    assert git("status", "--porcelain")

    checkout = checkout_dataset_revision(
        repository,
        "src/main/java/demo/Focal.java",
        "src/test/java/demo/FocalTest.java",
    )

    assert checkout == dataset_revision
    assert focal.is_file()
    assert not git("status", "--porcelain")
