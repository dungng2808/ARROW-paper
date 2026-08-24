from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .build_runner import BuildContext, verify_module_tests, verify_target_test
from .fs_utils import atomic_copy, atomic_write_json, atomic_write_text, ensure_dir, read_text_if_exists
from .import_repair import repair_imports_for_context
from .llm_client import LlmClient, LlmRequest, record_token_usage
from .models import (
    ExperimentContext,
    FailureOrigin,
    FailureState,
    RepairConfig,
    RepairDecision,
    RepairStatus,
    RepairSummary,
    VerificationResult,
)
from .prompt_builder import build_regeneration_prompt, build_repair_prompt
from .test_writer import JavaValidationError, code_hash, validate_java_candidate, write_owned_generated_test


TARGET_FAILURE_STATES = {
    FailureState.COMPILE_FAILED,
    FailureState.TEST_DISCOVERY_FAILED,
    FailureState.RUNTIME_FAILED,
    FailureState.ASSERTION_FAILED,
    FailureState.UNKNOWN_FAILED,
}

PROGRESS_TRANSITIONS = {
    (FailureState.COMPILE_FAILED, FailureState.TEST_DISCOVERY_FAILED),
    (FailureState.COMPILE_FAILED, FailureState.RUNTIME_FAILED),
    (FailureState.COMPILE_FAILED, FailureState.ASSERTION_FAILED),
    (FailureState.COMPILE_FAILED, FailureState.TARGET_TEST_PASSED),
    (FailureState.TEST_DISCOVERY_FAILED, FailureState.RUNTIME_FAILED),
    (FailureState.TEST_DISCOVERY_FAILED, FailureState.ASSERTION_FAILED),
    (FailureState.TEST_DISCOVERY_FAILED, FailureState.TARGET_TEST_PASSED),
    (FailureState.RUNTIME_FAILED, FailureState.ASSERTION_FAILED),
    (FailureState.RUNTIME_FAILED, FailureState.TARGET_TEST_PASSED),
    (FailureState.ASSERTION_FAILED, FailureState.TARGET_TEST_PASSED),
    (FailureState.TARGET_TEST_PASSED, FailureState.MODULE_TESTS_PASSED),
    (FailureState.MODULE_TESTS_FAILED, FailureState.MODULE_TESTS_PASSED),
}

REGRESSION_TRANSITIONS = {
    (FailureState.TEST_DISCOVERY_FAILED, FailureState.COMPILE_FAILED),
    (FailureState.RUNTIME_FAILED, FailureState.COMPILE_FAILED),
    (FailureState.RUNTIME_FAILED, FailureState.TEST_DISCOVERY_FAILED),
    (FailureState.ASSERTION_FAILED, FailureState.COMPILE_FAILED),
    (FailureState.ASSERTION_FAILED, FailureState.TEST_DISCOVERY_FAILED),
    (FailureState.ASSERTION_FAILED, FailureState.RUNTIME_FAILED),
    (FailureState.TARGET_TEST_PASSED, FailureState.COMPILE_FAILED),
    (FailureState.TARGET_TEST_PASSED, FailureState.TEST_DISCOVERY_FAILED),
    (FailureState.TARGET_TEST_PASSED, FailureState.RUNTIME_FAILED),
    (FailureState.TARGET_TEST_PASSED, FailureState.ASSERTION_FAILED),
}


def classify_transition(
    previous: VerificationResult,
    new: VerificationResult,
    *,
    candidate_hash_seen: bool = False,
) -> RepairDecision:
    if candidate_hash_seen:
        return RepairDecision.REPEATED_CODE
    if new.state == FailureState.MODULE_TESTS_PASSED:
        return RepairDecision.ACCEPTED
    if previous.state and new.state and (previous.state, new.state) in PROGRESS_TRANSITIONS:
        return RepairDecision.PROGRESS
    if previous.state and new.state and (previous.state, new.state) in REGRESSION_TRANSITIONS:
        return RepairDecision.REGRESSION
    if previous.state == FailureState.MODULE_TESTS_FAILED and new.state in TARGET_FAILURE_STATES:
        return RepairDecision.REGRESSION
    if previous.state == FailureState.MODULE_TESTS_PASSED and new.state != FailureState.MODULE_TESTS_PASSED:
        return RepairDecision.REGRESSION
    if previous.state == new.state:
        if previous.normalized_error_signature and previous.normalized_error_signature == new.normalized_error_signature:
            return RepairDecision.REPEATED_ERROR
        if (
            previous.normalized_error_signature
            and previous.normalized_error_signature not in set(new.error_signatures)
            and new.normalized_error_signature
            and new.normalized_error_signature != previous.normalized_error_signature
        ):
            return RepairDecision.PARTIAL_PROGRESS
        return RepairDecision.NO_PROGRESS
    return RepairDecision.NO_PROGRESS


def compare_module_to_baseline(module_result: VerificationResult, baseline: VerificationResult) -> VerificationResult:
    if module_result.state == FailureState.MODULE_TESTS_PASSED:
        return module_result
    baseline_failures = set(baseline.failed_test_ids)
    new_failures = [test_id for test_id in module_result.failed_test_ids if test_id not in baseline_failures]
    if not new_failures and module_result.failed_test_ids:
        module_result.failure_origin = FailureOrigin.EXISTING_PROJECT
    elif new_failures:
        module_result.failure_origin = FailureOrigin.GENERATED_TEST
    else:
        module_result.failure_origin = FailureOrigin.UNKNOWN
    module_result.state = FailureState.MODULE_TESTS_FAILED
    return module_result


@dataclass
class RepairTemplates:
    repair_templates: dict[str, str]
    regeneration_template: str


@dataclass
class RepairRuntime:
    config: RepairConfig
    context: ExperimentContext
    build_context: BuildContext
    llm_client: LlmClient
    templates: RepairTemplates
    model: str
    api_base: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    num_ctx: int | None = None
    max_tokens: int | None = None
    stream: bool = False
    thinking: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    token_usage_by_prompt: dict[str, dict[str, int]] = field(default_factory=dict)
    attempted_hashes: set[str] = field(default_factory=set)
    failed_signatures: list[str] = field(default_factory=list)

    @property
    def repair_dir(self) -> Path:
        return self.context.workspace.parent / "repair"

    @property
    def checkpoint_dir(self) -> Path:
        return self.repair_dir / "checkpoints"

    @property
    def best_candidate_path(self) -> Path:
        return self.repair_dir / "best_candidate.java"

    @property
    def best_verification_path(self) -> Path:
        return self.repair_dir / "best_verification.json"

    @property
    def best_metadata_path(self) -> Path:
        return self.repair_dir / "best_candidate_metadata.json"

    def persist_best(self, code: str, verification: VerificationResult, digest: str) -> None:
        atomic_write_text(self.best_candidate_path, code)
        atomic_write_json(self.best_verification_path, verification.to_dict())
        atomic_write_json(
            self.best_metadata_path,
            {
                "hash": digest,
                "state": verification.state.value if verification.state else None,
                "timestamp": time.time(),
            },
        )

    def rollback_to_best(self) -> None:
        atomic_copy(self.best_candidate_path, self.context.generated_test_path)


def _prompt_order(config: RepairConfig, state: FailureState | None) -> list[str]:
    key = state.value if state else FailureState.UNKNOWN_FAILED.value
    order = config.prompt_order_by_state.get(key) or config.prompt_order_by_state.get(FailureState.UNKNOWN_FAILED.value)
    return list(order or ["minimal-test", "dependency-aware", "compile-error-focused"])


def _checkpoint(runtime: RepairRuntime, attempt: int) -> Path:
    path = runtime.checkpoint_dir / f"attempt_{attempt}"
    ensure_dir(path)
    return path


def _write_decision(path: Path, **data: object) -> None:
    data.setdefault("timestamp", time.time())
    atomic_write_json(path / "decision.json", data)


def _repair_log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] REPAIR {message}", flush=True)


def _verify_candidate(runtime: RepairRuntime) -> VerificationResult:
    target = verify_target_test(runtime.build_context)
    if target.state != FailureState.TARGET_TEST_PASSED:
        return target
    module = verify_module_tests(runtime.build_context)
    return module if module.state == FailureState.MODULE_TESTS_PASSED else module


def _call_llm(runtime: RepairRuntime, prompt: str, prompt_name: str) -> str:
    response = runtime.llm_client.complete(
        LlmRequest(
            model=runtime.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=runtime.temperature,
            api_base=runtime.api_base,
            api_key_env=runtime.api_key_env,
            num_ctx=runtime.num_ctx,
            max_tokens=runtime.max_tokens,
            stream=runtime.stream,
            thinking=runtime.thinking,
            reasoning_effort=runtime.reasoning_effort,
        )
    )
    usage = record_token_usage(runtime.token_usage_by_prompt, prompt_name, response.metadata)
    if usage:
        _repair_log(
            f"tokens prompt={prompt_name} input={usage['input_tokens']} "
            f"output={usage['output_tokens']} total={usage['total_tokens']}"
        )
    return response.content


def _switch_prompt(order: list[str], current_index: int) -> int:
    return min(current_index + 1, len(order))


def _unlimited_retry(config: RepairConfig) -> bool:
    return config.retry_mode.lower() == "unlimited"


def _within_attempt_budget(config: RepairConfig, repair_attempts: int, total_llm_attempts: int) -> bool:
    if _unlimited_retry(config):
        return True
    return repair_attempts < config.max_repair_attempts and total_llm_attempts < config.max_total_llm_attempts


def _within_wall_clock_budget(config: RepairConfig, started: float) -> bool:
    if not _unlimited_retry(config):
        return True
    minutes = float(config.unlimited_max_wall_clock_minutes or 0)
    if minutes <= 0:
        return True
    return (time.time() - started) < minutes * 60


def _next_prompt_index(order: list[str], current_index: int, config: RepairConfig) -> int:
    next_index = _switch_prompt(order, current_index)
    if _unlimited_retry(config) and order and next_index >= len(order):
        return 0
    return next_index


def _can_regenerate(config: RepairConfig, total_llm_attempts: int) -> bool:
    return _unlimited_retry(config) or total_llm_attempts < config.max_total_llm_attempts


def run_adaptive_repair(
    runtime: RepairRuntime,
    *,
    initial_verification: VerificationResult,
    baseline_verification: VerificationResult | None = None,
) -> RepairSummary:
    ensure_dir(runtime.repair_dir)
    current_code = read_text_if_exists(runtime.context.generated_test_path)
    if not current_code:
        return RepairSummary(repair_status=RepairStatus.INVALID_OUTPUT, repair_stopped_reason="invalid_llm_output")
    current_hash = code_hash(current_code)
    runtime.attempted_hashes.add(current_hash)
    runtime.persist_best(current_code, initial_verification, current_hash)

    if initial_verification.state == FailureState.TARGET_TEST_PASSED:
        module = verify_module_tests(runtime.build_context)
        final = compare_module_to_baseline(module, baseline_verification) if baseline_verification else module
        if final.state == FailureState.MODULE_TESTS_PASSED:
            runtime.persist_best(current_code, final, current_hash)
            return RepairSummary(
                repair_status=RepairStatus.NOT_NEEDED,
                initial_failure_state=initial_verification.state.value,
                final_failure_state=final.state.value,
                best_candidate_state=final.state.value,
                repair_stopped_reason="module_tests_passed",
                best_candidate_hash=current_hash,
                checkpoint_directory=str(runtime.checkpoint_dir),
            )
        if final.failure_origin != FailureOrigin.GENERATED_TEST:
            return RepairSummary(
                repair_status=RepairStatus.EXISTING_PROJECT_FAILURE,
                initial_failure_state=initial_verification.state.value,
                final_failure_state=final.state.value if final.state else "",
                initial_failure_origin=initial_verification.failure_origin.value,
                final_failure_origin=final.failure_origin.value,
                repair_stopped_reason="existing_project_failure",
                best_candidate_hash=current_hash,
                checkpoint_directory=str(runtime.checkpoint_dir),
            )
        initial_verification = final

    if initial_verification.failure_origin not in {FailureOrigin.GENERATED_TEST, FailureOrigin.UNKNOWN}:
        return RepairSummary(
            repair_status=RepairStatus.EXISTING_PROJECT_FAILURE,
            initial_failure_state=initial_verification.state.value if initial_verification.state else "",
            final_failure_state=initial_verification.state.value if initial_verification.state else "",
            initial_failure_origin=initial_verification.failure_origin.value,
            final_failure_origin=initial_verification.failure_origin.value,
            repair_stopped_reason="existing_project_failure",
            best_candidate_hash=current_hash,
            checkpoint_directory=str(runtime.checkpoint_dir),
        )

    previous = initial_verification
    best_state = initial_verification.state.value if initial_verification.state else ""
    prompt_order = _prompt_order(runtime.config, previous.state)
    prompt_index = 0
    loop_started = time.time()
    attempts_for_prompt = 0
    repair_attempts = 0
    total_llm_attempts = 0
    build_attempts = 0
    prompt_switches = 0
    rollbacks = 0
    no_progress = 0
    repeated_error = False
    repeated_code = False
    first_prompt = prompt_order[0] if prompt_order else ""
    final_prompt = first_prompt
    stop_reason = "prompts_exhausted"
    status = RepairStatus.FAILED

    while prompt_index < len(prompt_order) and _within_attempt_budget(runtime.config, repair_attempts, total_llm_attempts) and _within_wall_clock_budget(runtime.config, loop_started):
        prompt_name = prompt_order[prompt_index]
        final_prompt = prompt_name
        if attempts_for_prompt >= runtime.config.max_attempts_per_prompt:
            prompt_index = _next_prompt_index(prompt_order, prompt_index, runtime.config)
            attempts_for_prompt = 0
            prompt_switches += 1
            continue

        attempt_number = repair_attempts + 1
        path = _checkpoint(runtime, attempt_number)
        before_code = read_text_if_exists(runtime.context.generated_test_path)
        atomic_write_text(path / "generated_test_before.java", before_code)
        atomic_write_json(path / "verification_before.json", previous.to_dict())
        atomic_write_text(path / "build_output_before.txt", previous.raw_output)

        template = runtime.templates.repair_templates[prompt_name]
        _repair_log(f"attempt={attempt_number} prompt={prompt_name} previous={previous.state.value if previous.state else 'UNKNOWN'}")
        prompt = build_repair_prompt(
            template=template,
            context=runtime.context,
            best_generated_test=read_text_if_exists(runtime.best_candidate_path),
            verification=previous,
            failed_signature_history=runtime.failed_signatures,
        )
        atomic_write_text(path / "repair_prompt.txt", prompt)
        llm_content = _call_llm(runtime, prompt, f"repair:{prompt_name}")
        total_llm_attempts += 1
        repair_attempts += 1
        attempts_for_prompt += 1
        atomic_write_text(path / "llm_response.txt", llm_content)

        try:
            candidate_code, candidate_hash = validate_java_candidate(
                llm_content,
                expected_package=runtime.context.package_name,
                expected_class_name=runtime.context.generated_test_class_name,
                testing_framework=runtime.context.testing_framework,
                attempted_hashes=None,
            )
        except JavaValidationError as exc:
            _repair_log(f"attempt={attempt_number} invalid_output reason={exc}")
            atomic_write_text(path / "generated_test_after.java", "")
            skipped = VerificationResult.skipped("invalid_llm_output")
            atomic_write_json(path / "verification_after.json", skipped.to_dict())
            atomic_write_text(path / "build_output_after.txt", "")
            _write_decision(
                path,
                attempt_number=attempt_number,
                previous_state=previous.state.value if previous.state else None,
                new_state=None,
                previous_signature=previous.normalized_error_signature,
                new_signature="",
                candidate_hash_before=code_hash(before_code),
                candidate_hash_after="",
                best_candidate_hash=code_hash(read_text_if_exists(runtime.best_candidate_path)),
                decision=RepairDecision.INVALID_LLM_OUTPUT.value,
                rollback_performed=True,
                prompt_switched=True,
                build_skipped=True,
                failure_origin=FailureOrigin.UNKNOWN.value,
                reason=str(exc),
            )
            runtime.rollback_to_best()
            rollbacks += 1
            prompt_index = _next_prompt_index(prompt_order, prompt_index, runtime.config)
            prompt_switches += 1
            attempts_for_prompt = 0
            stop_reason = "invalid_llm_output"
            status = RepairStatus.INVALID_OUTPUT
            continue

        atomic_write_text(path / "generated_test_after.java", candidate_code)
        if candidate_hash in runtime.attempted_hashes:
            repeated_code = True
            _repair_log(f"attempt={attempt_number} repeated_code skip_build rollback switch_prompt")
            skipped = VerificationResult.skipped("repeated_code")
            atomic_write_json(path / "verification_after.json", skipped.to_dict())
            atomic_write_text(path / "build_output_after.txt", "")
            runtime.rollback_to_best()
            rollbacks += 1
            prompt_index = _next_prompt_index(prompt_order, prompt_index, runtime.config)
            prompt_switches += 1
            attempts_for_prompt = 0
            _write_decision(
                path,
                attempt_number=attempt_number,
                previous_state=previous.state.value if previous.state else None,
                new_state=None,
                previous_signature=previous.normalized_error_signature,
                new_signature="repeated_code",
                candidate_hash_before=code_hash(before_code),
                candidate_hash_after=candidate_hash,
                best_candidate_hash=code_hash(read_text_if_exists(runtime.best_candidate_path)),
                decision=RepairDecision.REPEATED_CODE.value,
                rollback_performed=True,
                prompt_switched=True,
                build_skipped=True,
                failure_origin=FailureOrigin.UNKNOWN.value,
                reason="repeated_code",
            )
            stop_reason = "repeated_code"
            continue

        write_owned_generated_test(
            experiment_id=f"{runtime.context.run_id}:{runtime.context.shard_id}:{runtime.context.input_id}:{runtime.context.agent_name}:{runtime.context.generation_prompt}",
            workspace=runtime.context.workspace,
            generated_test_path=runtime.context.generated_test_path,
            generated_test_class_name=runtime.context.generated_test_class_name,
            code=candidate_code,
        )
        import_repair = repair_imports_for_context(candidate_code, runtime.context)
        if import_repair.changed:
            write_owned_generated_test(
                experiment_id=f"{runtime.context.run_id}:{runtime.context.shard_id}:{runtime.context.input_id}:{runtime.context.agent_name}:{runtime.context.generation_prompt}",
                workspace=runtime.context.workspace,
                generated_test_path=runtime.context.generated_test_path,
                generated_test_class_name=runtime.context.generated_test_class_name,
                code=import_repair.code,
            )
            candidate_code = import_repair.code
            candidate_hash = code_hash(candidate_code)
            atomic_write_text(path / "generated_test_after.java", candidate_code)
            _repair_log(f"attempt={attempt_number} import_repair added={len(import_repair.added_imports)}")
        atomic_write_json(path / "import_repair.json", import_repair.to_dict())
        runtime.attempted_hashes.add(candidate_hash)
        new = _verify_candidate(runtime)
        build_attempts += 1
        atomic_write_json(path / "verification_after.json", new.to_dict())
        atomic_write_text(path / "build_output_after.txt", new.raw_output)
        decision = classify_transition(previous, new)
        _repair_log(f"attempt={attempt_number} new={new.state.value if new.state else 'UNKNOWN'} decision={decision.value} origin={new.failure_origin.value}")
        rollback = False
        switch = False
        reason = decision.value.lower()

        if new.state == FailureState.BUILD_TIMEOUT:
            runtime.rollback_to_best()
            rollback = True
            rollbacks += 1
            status = RepairStatus.TIMEOUT
            stop_reason = "build_timeout"
            _repair_log(f"attempt={attempt_number} stop build_timeout rollback")
            _write_decision(path, attempt_number=attempt_number, previous_state=previous.state.value if previous.state else None, new_state=new.state.value, previous_signature=previous.normalized_error_signature, new_signature=new.normalized_error_signature, candidate_hash_before=code_hash(before_code), candidate_hash_after=candidate_hash, best_candidate_hash=code_hash(read_text_if_exists(runtime.best_candidate_path)), decision=RepairDecision.STOP.value, rollback_performed=rollback, prompt_switched=False, build_skipped=False, failure_origin=new.failure_origin.value, reason=stop_reason)
            break
        if new.state == FailureState.TOOL_ERROR:
            runtime.rollback_to_best()
            rollback = True
            rollbacks += 1
            status = RepairStatus.TOOL_ERROR
            stop_reason = "tool_error"
            _repair_log(f"attempt={attempt_number} stop tool_error rollback")
            _write_decision(path, attempt_number=attempt_number, previous_state=previous.state.value if previous.state else None, new_state=new.state.value, previous_signature=previous.normalized_error_signature, new_signature=new.normalized_error_signature, candidate_hash_before=code_hash(before_code), candidate_hash_after=candidate_hash, best_candidate_hash=code_hash(read_text_if_exists(runtime.best_candidate_path)), decision=RepairDecision.STOP.value, rollback_performed=rollback, prompt_switched=False, build_skipped=False, failure_origin=new.failure_origin.value, reason=stop_reason)
            break

        if decision in {RepairDecision.PROGRESS, RepairDecision.PARTIAL_PROGRESS, RepairDecision.ACCEPTED}:
            runtime.persist_best(candidate_code, new, candidate_hash)
            previous = new
            best_state = new.state.value if new.state else best_state
            no_progress = 0
            if new.normalized_error_signature:
                runtime.failed_signatures.append(new.normalized_error_signature)
            if new.state == FailureState.TARGET_TEST_PASSED:
                module = verify_module_tests(runtime.build_context)
                build_attempts += 1
                final_module = compare_module_to_baseline(module, baseline_verification) if baseline_verification else module
                if final_module.state == FailureState.MODULE_TESTS_PASSED:
                    runtime.persist_best(candidate_code, final_module, candidate_hash)
                    status = RepairStatus.REPAIRED
                    stop_reason = "module_tests_passed"
                    _repair_log(f"attempt={attempt_number} success module_tests_passed")
                    previous = final_module
                    best_state = final_module.state.value
                    _write_decision(path, attempt_number=attempt_number, previous_state=initial_verification.state.value if initial_verification.state else None, new_state=final_module.state.value, previous_signature=initial_verification.normalized_error_signature, new_signature=final_module.normalized_error_signature, candidate_hash_before=code_hash(before_code), candidate_hash_after=candidate_hash, best_candidate_hash=candidate_hash, decision=RepairDecision.ACCEPTED.value, rollback_performed=False, prompt_switched=False, build_skipped=False, failure_origin=final_module.failure_origin.value, reason=stop_reason)
                    break
                if final_module.failure_origin != FailureOrigin.GENERATED_TEST:
                    status = RepairStatus.EXISTING_PROJECT_FAILURE
                    stop_reason = "existing_project_failure"
                    _repair_log(f"attempt={attempt_number} stop existing_project_failure")
                    previous = final_module
                    break
                previous = final_module
            elif new.state == FailureState.MODULE_TESTS_PASSED:
                status = RepairStatus.REPAIRED
                stop_reason = "module_tests_passed"
                _repair_log(f"attempt={attempt_number} success module_tests_passed")
                break
        elif decision in {RepairDecision.REGRESSION, RepairDecision.REPEATED_ERROR}:
            if decision == RepairDecision.REPEATED_ERROR:
                repeated_error = True
            runtime.rollback_to_best()
            rollback = True
            switch = True
            rollbacks += 1
            prompt_index = _next_prompt_index(prompt_order, prompt_index, runtime.config)
            prompt_switches += 1
            attempts_for_prompt = 0
            stop_reason = "repeated_error" if decision == RepairDecision.REPEATED_ERROR else "regression"
            _repair_log(f"attempt={attempt_number} {stop_reason} rollback switch_prompt")
        else:
            no_progress += 1
            if no_progress >= runtime.config.no_progress_patience:
                runtime.rollback_to_best()
                rollback = True
                switch = True
                rollbacks += 1
                prompt_index = _next_prompt_index(prompt_order, prompt_index, runtime.config)
                prompt_switches += 1
                attempts_for_prompt = 0
                stop_reason = "no_progress"
                _repair_log(f"attempt={attempt_number} no_progress rollback switch_prompt")

        _write_decision(
            path,
            attempt_number=attempt_number,
            previous_state=previous.state.value if previous.state else None,
            new_state=new.state.value if new.state else None,
            previous_signature=previous.normalized_error_signature,
            new_signature=new.normalized_error_signature,
            candidate_hash_before=code_hash(before_code),
            candidate_hash_after=candidate_hash,
            best_candidate_hash=code_hash(read_text_if_exists(runtime.best_candidate_path)),
            decision=decision.value,
            rollback_performed=rollback,
            prompt_switched=switch,
            build_skipped=False,
            failure_origin=new.failure_origin.value,
            reason=reason,
        )

    regenerated = False
    if status == RepairStatus.FAILED and _unlimited_retry(runtime.config) and not _within_wall_clock_budget(runtime.config, loop_started):
        stop_reason = "unlimited_time_budget_exhausted"
    if (
        status == RepairStatus.FAILED
        and runtime.config.allow_regenerate_after_prompt_fallback_fail
        and _can_regenerate(runtime.config, total_llm_attempts)
        and runtime.config.max_regenerate_attempts > 0
    ):
        regenerated = True
        _repair_log("regeneration fallback start")
        total_llm_attempts += 1
        status = RepairStatus.REGENERATED
        stop_reason = "regeneration_exhausted"
        prompt = build_regeneration_prompt(
            template=runtime.templates.regeneration_template,
            context=runtime.context,
            failed_signature_history=runtime.failed_signatures,
        )
        content = _call_llm(runtime, prompt, "regeneration")
        regen_dir = runtime.repair_dir / "regeneration"
        ensure_dir(regen_dir)
        atomic_write_text(regen_dir / "regeneration_prompt.txt", prompt)
        atomic_write_text(regen_dir / "llm_response.txt", content)

    best_hash = code_hash(read_text_if_exists(runtime.best_candidate_path))
    return RepairSummary(
        repair_status=status,
        initial_failure_state=initial_verification.state.value if initial_verification.state else "",
        final_failure_state=previous.state.value if previous.state else "",
        best_candidate_state=best_state,
        initial_failure_origin=initial_verification.failure_origin.value,
        final_failure_origin=previous.failure_origin.value,
        repair_attempts=repair_attempts,
        regeneration_attempts=1 if regenerated else 0,
        total_llm_attempts=total_llm_attempts,
        build_attempts=build_attempts,
        initial_repair_prompt_strategy=first_prompt,
        final_repair_prompt_strategy=final_prompt,
        prompt_switch_count=prompt_switches,
        rollback_count=rollbacks,
        repeated_error_signature=repeated_error,
        repeated_code_detected=repeated_code,
        no_progress_count=no_progress,
        repair_stopped_reason=stop_reason if status != RepairStatus.FAILED else "prompts_exhausted",
        regenerated_after_repair_fail=regenerated,
        best_candidate_hash=best_hash,
        checkpoint_directory=str(runtime.checkpoint_dir),
    )
