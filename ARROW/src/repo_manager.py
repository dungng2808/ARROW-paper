from __future__ import annotations

import os
import re
import shutil
import subprocess
import stat
from pathlib import Path

from .fs_utils import ensure_dir


def _retry_remove_readonly(function, path, _exc_info) -> None:
    target = Path(path)
    target.chmod(target.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    # Python 3.9's shutil.rmtree reports an unreadable directory through
    # onerror(os.open, ...).  os.open cannot be retried as function(path), and
    # merely opening it would not make rmtree descend into the directory.
    if function is os.open:
        shutil.rmtree(target, onerror=_retry_remove_readonly)
    else:
        function(path)


def safe_remove_tree(path: Path, allowed_root: Path) -> bool:
    """Remove a tree only when it is inside the expected pipeline-owned root."""
    resolved_path = path.resolve()
    resolved_root = allowed_root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Refusing to remove path outside allowed root: {resolved_path}") from exc
    if not resolved_path.exists():
        return False
    target = _to_longpath_str(resolved_path) if os.name == "nt" else resolved_path
    try:
        shutil.rmtree(target, onerror=_retry_remove_readonly)
    except Exception:
        pass
    return True


def _workspace_copy_ignore(directory: str, names: list[str]) -> set[str]:
    """Ignore generated outputs without deleting source packages named build.

    ``shutil.ignore_patterns("build")`` matches at every directory depth. That
    corrupts repositories such as cruise-control, whose Gradle buildSrc source
    package is literally ``com/linkedin/gradle/build``.
    """
    current = Path(directory)
    ignored = {name for name in names if name in {".git", ".gradle", "__pycache__"}}
    is_build_root = any((current / marker).is_file() for marker in ("pom.xml", "build.gradle", "build.gradle.kts"))
    if is_build_root:
        ignored.update(name for name in names if name in {"target", "build"})
    return ignored


def _repository_requires_git_metadata(repository: Path) -> bool:
    command_pattern = re.compile(
        r"(?is)(?:commandLine|command)\s*\(?\s*['\"]git['\"].{0,300}\b(?:describe|rev-parse|log|tag)\b"
    )
    candidates = [repository / "build.gradle", repository / "build.gradle.kts", repository / "pom.xml"]
    candidates.extend(repository.glob("**/*.gradle"))
    candidates.extend(repository.glob("**/*.gradle.kts"))
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file() or ".git" in path.parts:
            continue
        seen.add(path)
        text = path.read_text(encoding="utf-8", errors="ignore")
        if command_pattern.search(text):
            return True
    return False


def _make_build_wrappers_executable(root: Path) -> None:
    fallback_jar = Path(__file__).resolve().parent.parent / "tools" / "gradle" / "gradle-wrapper.jar"
    for wrapper_name in ("mvnw", "gradlew", "gradlew.bat"):
        for wrapper in root.glob(f"**/{wrapper_name}"):
            if not wrapper.is_file() or ".git" in wrapper.parts:
                continue
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            if "gradlew" in wrapper_name and fallback_jar.is_file():
                wrapper_jar = wrapper.parent / "gradle" / "wrapper" / "gradle-wrapper.jar"
                if not wrapper_jar.is_file():
                    wrapper_jar.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(fallback_jar, wrapper_jar)
                    except OSError:
                        pass


def _ensure_git_history_for_build(repository: Path) -> None:
    if not _repository_requires_git_metadata(repository):
        return
    result = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    if result.stdout.strip().lower() == "true":
        subprocess.run(["git", "fetch", "--unshallow", "--tags"], cwd=repository, check=True)


def _ensure_full_git_history(repository: Path) -> None:
    if not (repository / ".git").exists():
        return
    result = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    if result.stdout.strip().lower() == "true":
        subprocess.run(["git", "fetch", "--unshallow", "--tags"], cwd=repository, check=True)


def _repo_relative_candidates(relative: str) -> list[Path]:
    path = Path(relative)
    candidates = [path]
    if len(path.parts) > 1:
        candidates.append(Path(*path.parts[1:]))
    output: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = item.as_posix()
        if key not in seen:
            output.append(item)
            seen.add(key)
    return output


def _has_any_worktree_path(repository: Path, candidates: list[Path]) -> bool:
    return any((repository / candidate).is_file() for candidate in candidates)


def _git_lines(repository: Path, command: list[str]) -> list[str]:
    result = subprocess.run(
        command,
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_object_exists(repository: Path, revision: str, relative: Path) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}:{relative.as_posix()}"],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def checkout_dataset_revision(repository: Path, focal_class_path: str, test_class_path: str) -> str:
    """Checkout a commit where dataset focal/test paths still exist.

    Classes2Test samples were mined from historical repository revisions. Some
    repositories no longer contain the recorded focal path on the default
    branch, so the pipeline needs a best-effort history lookup before analysis.
    """
    if not (repository / ".git").exists():
        return ""
    focal_candidates = _repo_relative_candidates(focal_class_path)
    test_candidates = _repo_relative_candidates(test_class_path)
    if _has_any_worktree_path(repository, focal_candidates) and _has_any_worktree_path(repository, test_candidates):
        return ""

    _ensure_full_git_history(repository)
    commits: list[str] = []
    seen: set[str] = set()
    for candidate in [*focal_candidates, *test_candidates]:
        for commit in _git_lines(repository, ["git", "log", "--all", "--format=%H", "--", candidate.as_posix()]):
            if commit not in seen:
                commits.append(commit)
                seen.add(commit)

    for commit in commits:
        focal_exists = any(_git_object_exists(repository, commit, candidate) for candidate in focal_candidates)
        test_exists = any(_git_object_exists(repository, commit, candidate) for candidate in test_candidates)
        if focal_exists and test_exists:
            # The repository under ``repos/`` is a pipeline-owned cache.  A
            # previous interrupted run (or making mvnw/gradlew executable on
            # POSIX) can leave tracked mode-only changes behind.  Force the
            # historical checkout so those cache-local changes cannot trigger
            # a multi-gigabyte clone retry.
            subprocess.run(["git", "checkout", "--force", commit], cwd=repository, check=True)
            return commit
    return ""


def _to_longpath_str(p: Path | str) -> str:
    s = str(Path(p).resolve()).replace("/", "\\")
    if os.name == "nt" and not s.startswith("\\\\?\\") and not s.startswith("\\\\"):
        return "\\\\?\\" + s
    return s


def copy_isolated_workspace(source_repo: Path, experiment_workspace: Path) -> Path:
    """Create a writable per-experiment copy without depending on Git worktrees."""
    if experiment_workspace.exists():
        safe_remove_tree(experiment_workspace, experiment_workspace.parent)
    # Preserve symlinks instead of dereferencing them. Historical repositories
    # frequently contain relative links (for example .sdkmanrc) whose target is
    # absent in a shallow checkout; dereferencing such a link makes copytree
    # fail before the build can even be attempted.
    src_str = _to_longpath_str(source_repo)
    dst_str = _to_longpath_str(experiment_workspace)
    shutil.copytree(src_str, dst_str, symlinks=True, ignore=_workspace_copy_ignore)
    if _repository_requires_git_metadata(source_repo) and (source_repo / ".git").is_dir():
        shutil.copytree(_to_longpath_str(source_repo / ".git"), _to_longpath_str(experiment_workspace / ".git"))
    _make_build_wrappers_executable(experiment_workspace)
    return experiment_workspace


def clone_repo(repo_url: str, destination: Path, checkout: str | None = None) -> Path:
    if destination.exists():
        _ensure_git_history_for_build(destination)
        return destination
    ensure_dir(destination.parent)
    try:
        subprocess.run(["git", "-c", "core.longpaths=true", "clone", "--depth", "1", repo_url, str(destination)], check=True)
    except subprocess.CalledProcessError:
        if destination.exists():
            safe_remove_tree(destination, destination.parent)
        raise
    if checkout:
        subprocess.run(["git", "checkout", "--force", checkout], cwd=destination, check=True)
    _ensure_git_history_for_build(destination)
    return destination


def ensure_experiment_workspace(
    *,
    cached_repo: Path,
    experiment_workspace: Path,
) -> Path:
    return copy_isolated_workspace(cached_repo, experiment_workspace)
