from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .adaptive_repair import RepairRuntime, RepairTemplates, compare_module_to_baseline, run_adaptive_repair
from .build_runner import BuildContext, verify_baseline, verify_module_tests, verify_target_test
from .fs_utils import atomic_write_json, atomic_write_text, ensure_dir
from .import_repair import repair_imports_for_context
from .input_selector import count_dataset, select_inputs
from .java_resolver import resolve_java_home
from .llm_client import LiteLlmClient, LlmRequest, StaticLlmClient, record_token_usage, token_usage_report
from .metrics_runner import run_maven_metrics
from .models import AgentConfig, FailureOrigin, FailureState, GenerationStrategy, RepairConfig, RepairStatus, SampleInput
from .output_paths import OutputPaths, resolve_output_paths
from .project_analyzer import analyze_experiment
from .prompt_builder import build_generation_prompt, load_template
from .repo_manager import checkout_dataset_revision, clone_repo, ensure_experiment_workspace, safe_remove_tree
from .report_writer import (
    ensure_paper_report_fields,
    load_experiment_jsonl,
    merge_rows,
    repair_summary_to_report_row,
    write_experiment_statistics,
    write_experiment_statistics_csv,
    write_class_report,
    write_experiment_json,
    write_mean_report,
    write_merged_jsonl,
    write_run_summary,
)
from .test_writer import JavaValidationError, validate_java_candidate, write_owned_generated_test


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def dataset_dir() -> Path:
    return project_root().parent / "classes2test" / "dataset"


def load_config(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load config/pipeline.yaml")
    with path.open("r", encoding="utf-8") as input_file:
        return yaml.safe_load(input_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARROW end-to-end pipeline.")
    parser.add_argument("--config", type=Path, default=project_root() / "config" / "pipeline.yaml")
    parser.add_argument("--count-only", action="store_true")
    parser.add_argument("--list-inputs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--project-id")
    parser.add_argument("--sample-file")
    parser.add_argument("--repo-shard", type=Path)
    parser.add_argument("--shard-id")
    parser.add_argument("--skip-metrics", action="store_true")
    parser.add_argument("--mock-llm-output", type=Path, help="Use the file content as a fake LiteLLM response.")
    parser.add_argument("--mock-llm-smoke", action="store_true", help="Generate a tiny JUnit smoke test after project analysis.")
    parser.add_argument("--java-home", help="JAVA_HOME passed only to Maven/Gradle commands.")
    parser.add_argument("--agent", action="append", help="Run only this agent name; can be repeated.")
    parser.add_argument("--generation-prompt", action="append", help="Run only this generation prompt; can be repeated.")
    parser.add_argument("--keep-repo-cache", action="store_true", help="Do not delete cloned repo cache after reports are written.")
    parser.add_argument("--keep-workspace", action="store_true", help="Do not delete per-experiment workspace after the experiment finishes.")
    parser.add_argument("--merge-reports", action="store_true", help="Merge JSONL report records and export final CSV reports.")
    parser.add_argument("--runs-dir", type=Path, default=project_root() / "runs")
    parser.add_argument("--output-dir", type=Path, help="Output directory for --merge-reports.")
    return parser.parse_args()


def _agents(config: dict[str, Any], args: argparse.Namespace) -> list[AgentConfig]:
    llm = config.get("llm", {})
    selected = set(args.agent or config.get("experiment", {}).get("selected_agents") or [])
    api_base = llm.get("api_base")
    api_key_env = llm.get("api_key_env")
    agents = []
    for item in llm.get("agents", []):
        aliases = {str(item.get("name", "")), str(item.get("model", "")), str(item.get("model", "")).split("/")[-1]}
        if selected and aliases.isdisjoint(selected):
            continue
        agents.append(
            AgentConfig(
                name=item["name"],
                model=item["model"],
                temperature=float(item.get("temperature", 0)),
                api_base=item.get("api_base") or api_base,
                api_key_env=item.get("api_key_env") or api_key_env,
                num_ctx=item.get("num_ctx"),
                max_tokens=item.get("max_tokens"),
                stream=bool(item.get("stream", False)),
                thinking=item.get("thinking"),
                reasoning_effort=item.get("reasoning_effort"),
            )
        )
    if selected and not agents:
        available = []
        for item in llm.get("agents", []):
            model = str(item.get("model", ""))
            available.append(str(item.get("name", "")))
            available.append(model)
            available.append(model.split("/")[-1])
        raise ValueError(f"No configured agent matches {sorted(selected)}. Available agent/model aliases: {sorted(set(available))}")
    return agents


def _generation_strategies(config: dict[str, Any], args: argparse.Namespace) -> list[GenerationStrategy]:
    selected = set(args.generation_prompt or config.get("experiment", {}).get("selected_generation_prompts") or [])
    strategies = []
    for item in config.get("prompts", {}).get("generation_strategies", []):
        if selected and item.get("name") not in selected:
            continue
        strategies.append(GenerationStrategy(name=item["name"], template=item["template"], examples=item.get("examples")))
    return strategies


def _repair_config(config: dict[str, Any]) -> RepairConfig:
    return RepairConfig(**config.get("adaptive_repair", {}))


def _repair_templates(config: dict[str, Any]) -> RepairTemplates:
    root = project_root()
    repair_templates = {}
    for item in config.get("prompts", {}).get("repair_strategies", []):
        repair_templates[item["name"]] = load_template(root, item["template"])
    fallback = repair_templates.get("minimal-test") or next(iter(repair_templates.values()))
    return RepairTemplates(repair_templates=repair_templates, regeneration_template=fallback)


def _mock_smoke_code(package_name: str, class_name: str, framework: str) -> str:
    if framework == "junit5":
        return f"package {package_name};\n\nimport org.junit.jupiter.api.Test;\n\npublic class {class_name} {{\n    @Test\n    public void generatedSmokeTest() {{\n    }}\n}}\n"
    return f"package {package_name};\n\nimport org.junit.Test;\n\npublic class {class_name} {{\n    @Test\n    public void generatedSmokeTest() {{\n    }}\n}}\n"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_since(started: float) -> float:
    return round(time.time() - started, 3)


def _log_event(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _verification_label(result: Any) -> str:
    state = result.state.value if getattr(result, "state", None) else "UNKNOWN"
    origin = result.failure_origin.value if getattr(result, "failure_origin", None) else "UNKNOWN"
    primary = getattr(result, "primary_error", "") or getattr(result, "normalized_error_signature", "")
    suffix = f" | {primary[:180]}" if primary else ""
    return f"{state} ({origin}){suffix}"


def _baseline_blocks_generation(result: Any) -> bool:
    return result.state in {FailureState.COMPILE_FAILED, FailureState.BUILD_TIMEOUT, FailureState.TOOL_ERROR} or result.failure_origin in {
        FailureOrigin.BUILD_CONFIGURATION,
        FailureOrigin.INFRASTRUCTURE,
    }


def _experiment_dir(paths: OutputPaths, agent: AgentConfig, strategy: GenerationStrategy) -> Path:
    return paths.sample_root / agent.name / strategy.name


def _reset_experiment_dir(exp_dir: Path, sample_root: Path) -> bool:
    """Start a rerun from a clean per-agent/per-prompt experiment directory.

    The workspace copy is already recreated for every run, but verification and
    repair artifacts live beside it (for example ``target_verification.json``
    and ``repair/checkpoints``). If those files survive a rerun, dashboard error
    views can combine the new log with stale failure artifacts from the previous
    attempt. Removing the whole experiment directory keeps reruns auditable and
    scoped to exactly one sample/agent/prompt.
    """
    return safe_remove_tree(exp_dir, sample_root)


def _base_row(run_id: str, shard_id: str, sample: SampleInput, agent: AgentConfig, strategy: GenerationStrategy, paths: OutputPaths, exp_dir: Path) -> dict[str, Any]:
    return ensure_paper_report_fields({
        "run_id": run_id,
        "shard_id": shard_id,
        "input_id": sample.input_id,
        "sample_id": sample.input_id,
        "project_id": sample.project_id,
        "sample_file": str(sample.sample_file),
        "repository_url": sample.repository_url,
        "repo_name": paths.repo_identity.name,
        "repo_owner": paths.repo_identity.owner,
        "repo_folder": paths.repo_identity.folder,
        "output_layout": paths.layout,
        "experiment_dir": str(exp_dir),
        "reports_dir": str(paths.reports_dir),
        "agent_name": agent.name,
        "model": agent.model,
        "generation_prompt_strategy": strategy.name,
        "experiment_workspace": str(exp_dir / "workspace"),
        "workspace_deleted": False,
        "repo_cache_path": "",
        "repo_cache_deleted": False,
        "started_at": "",
        "finished_at": "",
        "target_passed_at": "",
        "target_pass_elapsed_seconds": "",
        "module_passed_at": "",
        "module_pass_elapsed_seconds": "",
        "first_passed_at": "",
        "first_pass_elapsed_seconds": "",
        "repair_time_seconds": "",
        "error": "",
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "llm_total_tokens": 0,
        "llm_call_count": 0,
        "token_usage_by_prompt": {},
    })


def _load_examples(strategy: GenerationStrategy, *, testing_framework: str) -> list[dict[str, Any]]:
    if not strategy.examples:
        return []
    path = Path(strategy.examples)
    if not path.is_absolute():
        path = project_root() / path
    if not path.is_file():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        return []
    framework = testing_framework.strip().lower()
    selected: list[dict[str, Any]] = []
    for example in loaded:
        if not isinstance(example, dict):
            continue
        example_framework = str(example.get("testing_framework", "")).strip().lower()
        if example_framework in {"", "any", framework}:
            selected.append(example)
        if len(selected) == 2:
            break
    return selected


def _prepare_repo(sample: SampleInput, config: dict[str, Any]) -> Path:
    repo_cfg = config.get("repo", {})
    repos_dir = Path(repo_cfg.get("repos_dir", "repos"))
    if not repos_dir.is_absolute():
        repos_dir = project_root() / repos_dir
    cached = repos_dir / sample.project_id
    if repo_cfg.get("clone_repo", True):
        repository = clone_repo(sample.repository_url, cached)
        if repo_cfg.get("checkout_commit", False):
            checkout_dataset_revision(repository, sample.focal_class_path, sample.test_class_path)
        return repository
    if not cached.exists():
        raise FileNotFoundError(f"Local repo cache not found: {cached}")
    return cached


def _should_delete_workspace(config: dict[str, Any], args: argparse.Namespace, row: dict[str, Any]) -> bool:
    if args.keep_workspace:
        return False
    cleanup = config.get("cleanup", {})
    if not cleanup.get("delete_experiment_workspace_after_report", False):
        return False
    if cleanup.get("keep_failed_workspaces", False) and row.get("test_passed") is not True:
        return False
    return True


def _run_one_experiment(
    *,
    sample: SampleInput,
    agent: AgentConfig,
    strategy: GenerationStrategy,
    config: dict[str, Any],
    args: argparse.Namespace,
    run_id: str,
    shard_id: str,
) -> dict[str, Any]:
    started = time.time()
    output_paths = resolve_output_paths(project_root(), sample, config, run_id, shard_id)
    exp_dir = _experiment_dir(output_paths, agent, strategy)
    workspace = exp_dir / "workspace"
    row = _base_row(run_id, shard_id, sample, agent, strategy, output_paths, exp_dir)
    token_usage_by_prompt: dict[str, dict[str, int]] = {}
    row["started_at"] = _now_iso()
    cached_repo: Path | None = None
    try:
        _log_event(f"START {sample.input_id} | project={sample.project_id} | agent={agent.name} | prompt={strategy.name}")
        cleared_records = _clear_experiment_records(Path(str(row["reports_dir"])), row, config)
        if cleared_records:
            _log_event(f"CLEAN stale experiment records -> {', '.join(str(path) for path in cleared_records)}")
        if _reset_experiment_dir(exp_dir, output_paths.sample_root):
            _log_event(f"CLEAN previous experiment artifacts -> {exp_dir}")
        ensure_dir(exp_dir)
        _log_event(f"CLONE/CACHE repo {sample.repository_url}")
        cached_repo = _prepare_repo(sample, config)
        row["repo_cache_path"] = str(cached_repo)
        _log_event(f"WORKSPACE copy -> {workspace}")
        ensure_experiment_workspace(cached_repo=cached_repo, experiment_workspace=workspace)
        _log_event("ANALYZE project/module context")
        context, module_root = analyze_experiment(
            sample=sample,
            workspace=workspace,
            run_id=run_id,
            shard_id=shard_id,
            agent_name=agent.name,
            generation_prompt=strategy.name,
        )
        _log_event(f"CONTEXT build={context.build_tool} module={context.module_path} framework={context.testing_framework} java={context.java_version}")
        row.update(
            {
                "build_tool": context.build_tool,
                "module_path": context.module_path,
                "focal_class": sample.focal_class_name,
                "focal_class_path": sample.focal_class_path,
                "generated_test_path": str(context.generated_test_path),
            }
        )
        atomic_write_json(exp_dir / "context.json", {"context": context, "sample": sample.raw})
        build_cfg = config.get("build", {})
        maven_cfg = build_cfg.get("maven", {})
        java_selection = resolve_java_home(workspace, module_root, config, manual_java_home=args.java_home)
        java_home = java_selection.java_home or None
        _log_event(f"JAVA {java_selection.reason}; requested={java_selection.requested_version}; home={java_home or 'system default'}; source={java_selection.source}")
        row["java_version"] = context.java_version
        row["java_home"] = java_home or ""
        row["java_selection_reason"] = java_selection.reason
        build_context = BuildContext(
            repository_root=workspace,
            module_root=module_root,
            build_tool=context.build_tool,
            generated_test_class_name=context.generated_test_class_name,
            generated_test_fqcn=f"{context.package_name}.{context.generated_test_class_name}" if context.package_name else context.generated_test_class_name,
            timeout_seconds=int(build_cfg.get("test_timeout_seconds", 900)),
            prefer_wrapper=bool(build_cfg.get("prefer_wrapper", True)),
            java_home=java_home,
            maven_multi_module_strategy=maven_cfg.get("multi_module_strategy", "module_only"),
            maven_use_also_make=bool(maven_cfg.get("use_also_make", True)),
            maven_fail_if_no_specified_tests=bool(maven_cfg.get("fail_if_no_specified_tests", False)),
        )
        _log_event("VERIFY baseline module tests")
        baseline = verify_baseline(build_context)
        _log_event(f"BASELINE {_verification_label(baseline)}")
        atomic_write_json(exp_dir / "baseline_verification.json", baseline.to_dict())
        atomic_write_text(exp_dir / "baseline_build_output.txt", baseline.raw_output)
        if _baseline_blocks_generation(baseline):
            row.update(
                {
                    "baseline_state": baseline.state.value if baseline.state else "",
                    "baseline_error_signatures": "|".join(baseline.error_signatures),
                    "initial_failure_state": baseline.state.value if baseline.state else "",
                    "final_failure_state": baseline.state.value if baseline.state else "",
                    "initial_failure_origin": baseline.failure_origin.value,
                    "final_failure_origin": baseline.failure_origin.value,
                    "repair_status": "EXISTING_PROJECT_FAILURE",
                    "repair_stopped_reason": "existing_project_failure",
                    "target_test_passed": False,
                    "module_tests_passed": False,
                    "test_passed": False,
                    "compilation": False,
                    "compile_errors": baseline.compile_errors,
                    "test_failures": baseline.test_failures,
                    "test_errors": baseline.test_errors,
                    "test_fail_reason": baseline.primary_error,
                }
            )
            row["elapsed_seconds"] = _seconds_since(started)
            row["finished_at"] = _now_iso()
            if workspace.exists() and _should_delete_workspace(config, args, row):
                safe_remove_tree(workspace, exp_dir)
                row["workspace_deleted"] = True
            return row

        _log_event(f"GENERATE test with {agent.model}")
        template = load_template(project_root(), strategy.template)
        prompt = build_generation_prompt(
            template=template,
            context=context,
            examples=_load_examples(strategy, testing_framework=context.testing_framework),
        )
        atomic_write_json(exp_dir / "prompt_messages.json", [{"role": "user", "content": prompt}])
        if args.mock_llm_smoke:
            llm = StaticLlmClient([_mock_smoke_code(context.package_name, context.generated_test_class_name, context.testing_framework)])
        elif args.mock_llm_output:
            llm = StaticLlmClient([args.mock_llm_output.read_text(encoding="utf-8")])
        else:
            llm = LiteLlmClient()
        max_invalid_retries = max(0, int(config.get("llm", {}).get("max_invalid_output_retries", 2)))
        generation_errors: list[str] = []
        response = None
        code = ""
        generation_usage = None
        selected_attempt = 0
        for generation_attempt in range(1, max_invalid_retries + 2):
            retry_suffix = ""
            if generation_errors:
                retry_suffix = (
                    "\n\nThe previous response was rejected before build because it was incomplete or invalid: "
                    f"{generation_errors[-1]}. Generate the complete Java compilation unit again from the beginning. "
                    "Close every declaration, block, literal, comment, parenthesis, and bracket. "
                    "Prefer a smaller focused test class so the response finishes within the output limit."
                )
            request_prompt = prompt + retry_suffix
            attempt_dir = exp_dir / "generation" / f"attempt_{generation_attempt}"
            atomic_write_text(attempt_dir / "prompt.txt", request_prompt)
            response = llm.complete(
                LlmRequest(
                    model=agent.model,
                    messages=[{"role": "user", "content": request_prompt}],
                    temperature=agent.temperature,
                    api_base=agent.api_base,
                    api_key_env=agent.api_key_env,
                    num_ctx=agent.num_ctx,
                    max_tokens=agent.max_tokens,
                    stream=agent.stream,
                    thinking=agent.thinking,
                    reasoning_effort=agent.reasoning_effort,
                )
            )
            generation_usage = record_token_usage(
                token_usage_by_prompt,
                f"generation:{strategy.name}",
                response.metadata,
            )
            if generation_usage:
                _log_event(
                    f"GENERATE attempt={generation_attempt} tokens input={generation_usage['input_tokens']} "
                    f"output={generation_usage['output_tokens']} total={generation_usage['total_tokens']}"
                )
            _log_event(f"GENERATE attempt={generation_attempt} response received; validating Java candidate")
            atomic_write_text(attempt_dir / "llm_response.txt", response.content)
            atomic_write_json(
                attempt_dir / "metadata.json",
                {"model": agent.model, "metadata": response.metadata, "token_usage": generation_usage or {}},
            )
            try:
                code, _digest = validate_java_candidate(
                    response.content,
                    expected_package=context.package_name,
                    expected_class_name=context.generated_test_class_name,
                    testing_framework=context.testing_framework,
                )
            except JavaValidationError as exc:
                error = str(exc)
                generation_errors.append(error)
                atomic_write_text(attempt_dir / "validation_error.txt", error + "\n")
                _log_event(f"GENERATE attempt={generation_attempt} invalid_output reason={error}")
                if generation_attempt > max_invalid_retries:
                    raise
                continue
            selected_attempt = generation_attempt
            break

        if response is None or not code:  # Defensive guard; the loop always sets or raises.
            raise JavaValidationError("generation did not produce a Java candidate")
        atomic_write_text(exp_dir / "llm_response.txt", response.content)
        atomic_write_json(
            exp_dir / "generation_metadata.json",
            {
                "model": agent.model,
                "metadata": response.metadata,
                "token_usage": generation_usage or {},
                "selected_attempt": selected_attempt,
                "invalid_output_retries": len(generation_errors),
                "validation_errors": generation_errors,
            },
        )
        row["NumberOfMethods"] = _count_generated_test_methods(code)
        experiment_id = f"{run_id}:{shard_id}:{sample.input_id}:{agent.name}:{strategy.name}"
        write_owned_generated_test(
            experiment_id=experiment_id,
            workspace=workspace,
            generated_test_path=context.generated_test_path,
            generated_test_class_name=context.generated_test_class_name,
            code=code,
        )
        import_repair = repair_imports_for_context(code, context)
        if import_repair.changed:
            write_owned_generated_test(
                experiment_id=experiment_id,
                workspace=workspace,
                generated_test_path=context.generated_test_path,
                generated_test_class_name=context.generated_test_class_name,
                code=import_repair.code,
            )
            code = import_repair.code
            _log_event(f"IMPORT_REPAIR added={len(import_repair.added_imports)}")
        atomic_write_json(exp_dir / "import_repair.json", import_repair.to_dict())
        _log_event(f"VERIFY target generated test {context.generated_test_class_name}")
        target = verify_target_test(build_context)
        _log_event(f"TARGET {_verification_label(target)}")
        atomic_write_json(exp_dir / "target_verification.json", target.to_dict())
        atomic_write_text(exp_dir / "target_build_output.txt", target.raw_output)
        final = target
        repair_summary = None
        if target.state == FailureState.TARGET_TEST_PASSED:
            _mark_target_pass(row, started)
            _log_event("VERIFY module test suite after target pass")
            module = verify_module_tests(build_context)
            final = compare_module_to_baseline(module, baseline)
            _log_event(f"MODULE {_verification_label(final)}")
            atomic_write_json(exp_dir / "module_verification.json", final.to_dict())
            atomic_write_text(exp_dir / "module_build_output.txt", final.raw_output)
            if final.state == FailureState.MODULE_TESTS_PASSED:
                _mark_module_pass(row, started)
        elif target.failure_origin in {FailureOrigin.GENERATED_TEST, FailureOrigin.UNKNOWN} and config.get("adaptive_repair", {}).get("enabled", True):
            _log_event("REPAIR start Adaptive Repair")
            repair_runtime = RepairRuntime(
                config=_repair_config(config),
                context=context,
                build_context=build_context,
                llm_client=llm,
                templates=_repair_templates(config),
                model=agent.model,
                api_base=agent.api_base,
                api_key_env=agent.api_key_env,
                temperature=agent.temperature,
                num_ctx=agent.num_ctx,
                max_tokens=agent.max_tokens,
                stream=agent.stream,
                thinking=agent.thinking,
                reasoning_effort=agent.reasoning_effort,
                token_usage_by_prompt=token_usage_by_prompt,
            )
            repair_started = time.monotonic()
            repair_summary = run_adaptive_repair(repair_runtime, initial_verification=target, baseline_verification=baseline)
            row["repair_time_seconds"] = round(time.monotonic() - repair_started, 3)
            _log_event(f"REPAIR done status={repair_summary.repair_status.value} attempts={repair_summary.repair_attempts} final={repair_summary.final_failure_state} reason={repair_summary.repair_stopped_reason}")
            repair_data = repair_summary.to_dict()
            repair_data.update(token_usage_report(token_usage_by_prompt))
            atomic_write_json(exp_dir / "repair_summary.json", repair_data)
        if repair_summary:
            row = repair_summary_to_report_row(repair_data, row)
            if row.get("target_test_passed") is True:
                _mark_target_pass(row, started)
            if row.get("module_tests_passed") is True:
                _mark_module_pass(row, started)
        else:
            row.update(
                {
                    "baseline_state": baseline.state.value if baseline.state else "",
                    "baseline_error_signatures": "|".join(baseline.error_signatures),
                    "initial_failure_state": target.state.value if target.state else "",
                    "final_failure_state": final.state.value if final.state else "",
                    "initial_failure_origin": target.failure_origin.value,
                    "final_failure_origin": final.failure_origin.value,
                    "repair_status": (
                        RepairStatus.NOT_NEEDED.value
                        if final.state == FailureState.MODULE_TESTS_PASSED
                        else RepairStatus.EXISTING_PROJECT_FAILURE.value
                        if final.failure_origin == FailureOrigin.EXISTING_PROJECT
                        else "FAILED"
                    ),
                    "target_test_passed": target.state == FailureState.TARGET_TEST_PASSED,
                    "module_tests_passed": final.state == FailureState.MODULE_TESTS_PASSED,
                    "test_passed": final.state == FailureState.MODULE_TESTS_PASSED,
                    "compilation": final.state not in {FailureState.COMPILE_FAILED, FailureState.TEST_DISCOVERY_FAILED},
                    "compile_errors": final.compile_errors,
                    "test_failures": final.test_failures,
                    "test_errors": final.test_errors,
                    "test_fail_reason": final.primary_error,
                    "existing_baseline_failures": "|".join(baseline.failed_test_ids),
                    "new_module_failures": "|".join(test_id for test_id in final.failed_test_ids if test_id not in set(baseline.failed_test_ids)),
                }
            )
        if row.get("module_tests_passed") is True and not args.skip_metrics:
            metrics_cfg = config.get("metrics", {})
            metrics_enabled = metrics_cfg.get("coverage", False) or metrics_cfg.get("mutation", False)
            if metrics_enabled and context.build_tool == "maven":
                _log_event("METRICS run JaCoCo/PIT/tsDetect")
                focal_fqcn = f"{context.package_name}.{sample.focal_class_name}" if context.package_name else sample.focal_class_name
                metrics, metric_verifications = run_maven_metrics(
                    build_context,
                    sample.focal_class_name,
                    focal_fqcn,
                    context.testing_framework,
                    sample.project_id,
                    context.generated_test_path,
                    sample.focal_class_path,
                    bool(metrics_cfg.get("smells", False)),
                )
                atomic_write_json(exp_dir / "metrics_report.json", metrics.to_dict())
                for metric_name, verification in metric_verifications.items():
                    atomic_write_json(exp_dir / f"{metric_name}_verification.json", verification.to_dict())
                    atomic_write_text(exp_dir / f"{metric_name}_build_output.txt", verification.raw_output)
                row.update(
                    {
                        "coverage_branch": metrics.coverage_branch,
                        "coverage_line": metrics.coverage_line,
                        "coverage_method": metrics.coverage_method,
                        "mutation_score": metrics.mutation_score,
                        "mutations_total": metrics.mutations_total,
                        "mutations_killed": metrics.mutations_killed,
                        "mutations_survived": metrics.mutations_survived,
                        "test_smell_total": metrics.test_smell_total,
                        "test_smell_details": metrics.test_smell_details,
                        "coverage_error": metrics.coverage_error,
                        "mutation_error": metrics.mutation_error,
                        "smell_error": metrics.smell_error,
                        **metrics.smell_values,
                    }
                )
    except Exception as exc:
        _log_event(f"ERROR {type(exc).__name__}: {exc}")
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["repair_status"] = "FAILED"
    row.update(token_usage_report(token_usage_by_prompt))
    if exp_dir.exists():
        atomic_write_json(exp_dir / "token_usage.json", token_usage_report(token_usage_by_prompt))
    row["elapsed_seconds"] = _seconds_since(started)
    row["finished_at"] = _now_iso()
    if workspace.exists() and _should_delete_workspace(config, args, row):
        _log_event("CLEANUP workspace")
        safe_remove_tree(workspace, exp_dir)
        row["workspace_deleted"] = True
    _log_event(f"FINISH {sample.input_id} status={row.get('repair_status')} passed={row.get('test_passed')} elapsed={row['elapsed_seconds']}s")
    return row


def _mark_target_pass(row: dict[str, Any], started: float) -> None:
    if not row.get("target_passed_at"):
        row["target_passed_at"] = _now_iso()
        row["target_pass_elapsed_seconds"] = _seconds_since(started)
    if not row.get("first_passed_at"):
        row["first_passed_at"] = row["target_passed_at"]
        row["first_pass_elapsed_seconds"] = row["target_pass_elapsed_seconds"]


def _mark_module_pass(row: dict[str, Any], started: float) -> None:
    if not row.get("module_passed_at"):
        row["module_passed_at"] = _now_iso()
        row["module_pass_elapsed_seconds"] = _seconds_since(started)
    if not row.get("first_passed_at"):
        row["first_passed_at"] = row["module_passed_at"]
        row["first_pass_elapsed_seconds"] = row["module_pass_elapsed_seconds"]


def _count_generated_test_methods(code: str) -> int:
    annotated = len(re.findall(r"@\s*(?:org\.junit\.jupiter\.api\.)?Test\b", code))
    if annotated:
        return annotated
    method_like = re.findall(r"\b(?:public|protected|private)?\s*(?:static\s+)?(?:void|[\w<>\[\], ?]+)\s+\w+\s*\([^;{}]*\)\s*(?:throws\s+[\w.,\s]+)?\{", code)
    return len(method_like)


def _cleanup_repo_caches(rows: list[dict[str, Any]], config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    cleanup = config.get("cleanup", {})
    repo_cfg = config.get("repo", {})
    delete_after_report = repo_cfg.get("delete_after_report", cleanup.get("delete_repo_cache_after_run", False))
    if args.keep_repo_cache or not delete_after_report:
        return {"enabled": False, "deleted": [], "errors": []}
    repos_dir = Path(repo_cfg.get("repos_dir", "repos"))
    if not repos_dir.is_absolute():
        repos_dir = project_root() / repos_dir
    deleted: list[str] = []
    errors: list[str] = []
    for raw_path in sorted({str(row.get("repo_cache_path") or "") for row in rows if row.get("repo_cache_path")}):
        path = Path(raw_path)
        try:
            if safe_remove_tree(path, repos_dir):
                deleted.append(str(path))
                for row in rows:
                    if row.get("repo_cache_path") == raw_path:
                        row["repo_cache_deleted"] = True
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    return {"enabled": True, "deleted": deleted, "errors": errors}


def _dry_run(samples: list[SampleInput], agents: list[AgentConfig], strategies: list[GenerationStrategy], run_id: str, shard_id: str, config: dict[str, Any]) -> None:
    for sample in samples:
        paths = resolve_output_paths(project_root(), sample, config, run_id, shard_id, persist_identity=False)
        for agent in agents:
            for strategy in strategies:
                print(f"{sample.input_id}\t{agent.name}\t{strategy.name}\t{_experiment_dir(paths, agent, strategy) / 'workspace'}")


def _report_records_paths(report_dir: Path, row: dict[str, Any], config: dict[str, Any]) -> tuple[Path, Path]:
    report_cfg = config.get("report", {})
    records_dir = report_dir / report_cfg.get("json_records_dir", "records")
    result_path = records_dir / str(row["input_id"]) / str(row["agent_name"]) / str(row["generation_prompt_strategy"]) / "result.json"
    jsonl_path = records_dir / "experiments.jsonl"
    return result_path, jsonl_path


def _write_experiment_records(report_dir: Path, row: dict[str, Any], config: dict[str, Any]) -> None:
    report_cfg = config.get("report", {})
    result_path, jsonl_path = _report_records_paths(report_dir, row, config)
    if report_cfg.get("write_per_experiment_json", True):
        write_experiment_json(result_path, row)
    if report_cfg.get("write_shard_jsonl", True):
        _upsert_experiment_jsonl(jsonl_path, row)


def _clear_experiment_records(report_dir: Path, row: dict[str, Any], config: dict[str, Any]) -> list[Path]:
    """Remove the previous logical result before a rerun starts.

    The dashboard reads per-experiment ``result.json`` files directly. If a
    rerun is in progress, the old result can otherwise remain visible until the
    new attempt reaches report writing, which makes the shard error panel look
    like it is showing a fresh failure when it is actually stale.
    """
    report_cfg = config.get("report", {})
    result_path, jsonl_path = _report_records_paths(report_dir, row, config)
    removed: list[Path] = []
    if report_cfg.get("write_per_experiment_json", True) and result_path.is_file():
        result_path.unlink()
        removed.append(result_path)
    if report_cfg.get("write_shard_jsonl", True) and jsonl_path.is_file():
        existing = load_experiment_jsonl([jsonl_path])
        kept = [item for item in existing if not _same_experiment_record(item, row)]
        if len(kept) != len(existing):
            write_merged_jsonl(jsonl_path, kept)
            removed.append(jsonl_path)
    return removed


def _same_experiment_record(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("shard_id", "input_id", "agent_name", "generation_prompt_strategy")
    return all(str(left.get(key, "")) == str(right.get(key, "")) for key in keys)


def _upsert_experiment_jsonl(jsonl_path: Path, row: dict[str, Any]) -> None:
    existing = [
        item
        for item in load_experiment_jsonl([jsonl_path])
        if not _same_experiment_record(item, row)
    ]
    write_merged_jsonl(jsonl_path, [*existing, row])


def _rewrite_experiment_records(rows: list[dict[str, Any]], config: dict[str, Any]) -> None:
    grouped: dict[Path, list[dict[str, Any]]] = {}
    for row in rows:
        report_dir = Path(str(row["reports_dir"]))
        result_path, jsonl_path = _report_records_paths(report_dir, row, config)
        if config.get("report", {}).get("write_per_experiment_json", True):
            write_experiment_json(result_path, row)
        grouped.setdefault(jsonl_path, []).append(row)
    if config.get("report", {}).get("write_shard_jsonl", True):
        for jsonl_path, items in grouped.items():
            write_merged_jsonl(jsonl_path, items)


def _merge_reports(args: argparse.Namespace, config: dict[str, Any]) -> None:
    runs_dir = args.runs_dir
    if not runs_dir.is_absolute():
        runs_dir = project_root() / runs_dir
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = runs_dir / config.get("report", {}).get("merged_dir", "merged")
    elif not output_dir.is_absolute():
        output_dir = project_root() / output_dir
    paths = sorted(runs_dir.glob("**/reports/records/experiments.jsonl"))
    rows, duplicates = merge_rows(load_experiment_jsonl(paths))
    write_merged_jsonl(output_dir / "experiments_merged.jsonl", rows)
    write_class_report(output_dir / "output_agone_classes_lite.csv", rows)
    write_mean_report(output_dir / "output_agone_mean_lite.csv", rows)
    write_experiment_statistics(output_dir / "experiment_statistics.json", rows)
    write_experiment_statistics_csv(output_dir / "experiment_statistics_by_group.csv", rows)
    write_run_summary(
        output_dir / "merge_summary.json",
        {
            "source_jsonl_files": [str(path) for path in paths],
            "experiments": len(rows),
            "duplicates": duplicates,
            "passed": sum(1 for row in rows if row.get("test_passed") is True),
            "failed": sum(1 for row in rows if row.get("test_passed") is not True),
        },
    )
    print(f"Merged JSONL: {output_dir / 'experiments_merged.jsonl'}")
    print(f"Class report: {output_dir / 'output_agone_classes_lite.csv'}")
    print(f"Mean report:  {output_dir / 'output_agone_mean_lite.csv'}")
    print(f"Stats JSON:   {output_dir / 'experiment_statistics.json'}")
    print(f"Stats CSV:    {output_dir / 'experiment_statistics_by_group.csv'}")
    print(f"Summary:      {output_dir / 'merge_summary.json'}")


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if args.merge_reports:
        _merge_reports(args, config)
        return
    dataset = dataset_dir()
    if args.count_only:
        projects, samples = count_dataset(dataset)
        print(f"Project folders: {projects}")
        print(f"Dataset inputs: {samples}")
        print(f"Current input.mode: {config.get('input', {}).get('mode', 'sample')}")
        return
    samples = select_inputs(
        dataset,
        config,
        start_index=args.start_index,
        limit=args.limit,
        project_id=args.project_id,
        sample_file=args.sample_file,
        repo_shard=args.repo_shard,
    )
    if args.list_inputs:
        for sample in samples:
            print(f"{sample.project_id}\t{sample.sample_file.name}\t{sample.sample_file}")
        return
    run_id = config.get("run", {}).get("run_id")
    if not run_id or run_id == "auto":
        run_id = time.strftime("%Y%m%d-%H%M%S")
    shard_id = args.shard_id or config.get("run", {}).get("shard_id", "local")
    agents = _agents(config, args)
    strategies = _generation_strategies(config, args)
    if args.dry_run:
        _dry_run(samples, agents, strategies, run_id, shard_id, config)
        return

    rows = []
    report_dirs: set[Path] = set()
    for sample in samples:
        paths = resolve_output_paths(project_root(), sample, config, run_id, shard_id)
        report_dirs.add(paths.reports_dir)
        for agent in agents:
            for strategy in strategies:
                row = _run_one_experiment(sample=sample, agent=agent, strategy=strategy, config=config, args=args, run_id=run_id, shard_id=shard_id)
                rows.append(row)
                _write_experiment_records(Path(str(row["reports_dir"])), row, config)

    report_dir = next(iter(report_dirs)) if len(report_dirs) == 1 else project_root() / "runs" / "merged" / run_id
    report_cfg = config.get("report", {})
    if report_cfg.get("write_csv_per_run", False):
        write_class_report(report_dir / "output_agone_classes_lite.csv", rows)
        write_mean_report(report_dir / "output_agone_mean_lite.csv", rows)
    write_run_summary(
        report_dir / "run_summary.json",
        {
            "run_id": run_id,
            "shard_id": shard_id,
            "selected_inputs": len(samples),
            "experiments": len(rows),
            "passed": sum(1 for row in rows if row.get("test_passed") is True),
            "failed": sum(1 for row in rows if row.get("test_passed") is not True),
            "skip_metrics": args.skip_metrics,
        },
    )
    cleanup_result = _cleanup_repo_caches(rows, config, args)
    if cleanup_result["enabled"]:
        if report_cfg.get("write_csv_per_run", False):
            write_class_report(report_dir / "output_agone_classes_lite.csv", rows)
            write_mean_report(report_dir / "output_agone_mean_lite.csv", rows)
        _rewrite_experiment_records(rows, config)
        write_run_summary(
            report_dir / "run_summary.json",
            {
                "run_id": run_id,
                "shard_id": shard_id,
                "selected_inputs": len(samples),
                "experiments": len(rows),
                "passed": sum(1 for row in rows if row.get("test_passed") is True),
                "failed": sum(1 for row in rows if row.get("test_passed") is not True),
                "skip_metrics": args.skip_metrics,
                "cleanup": cleanup_result,
            },
        )
    if report_cfg.get("write_csv_per_run", False):
        print(f"Class report: {report_dir / 'output_agone_classes_lite.csv'}")
        print(f"Mean report:  {report_dir / 'output_agone_mean_lite.csv'}")
    print(f"JSON records: {report_dir / report_cfg.get('json_records_dir', 'records')}")
    print(f"Summary:      {report_dir / 'run_summary.json'}")


if __name__ == "__main__":
    main()
