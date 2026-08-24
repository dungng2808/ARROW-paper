from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class FailureState(str, Enum):
    COMPILE_FAILED = "COMPILE_FAILED"
    TEST_DISCOVERY_FAILED = "TEST_DISCOVERY_FAILED"
    RUNTIME_FAILED = "RUNTIME_FAILED"
    ASSERTION_FAILED = "ASSERTION_FAILED"
    TARGET_TEST_PASSED = "TARGET_TEST_PASSED"
    MODULE_TESTS_FAILED = "MODULE_TESTS_FAILED"
    MODULE_TESTS_PASSED = "MODULE_TESTS_PASSED"
    BUILD_TIMEOUT = "BUILD_TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    UNKNOWN_FAILED = "UNKNOWN_FAILED"


class FailureOrigin(str, Enum):
    GENERATED_TEST = "GENERATED_TEST"
    EXISTING_PROJECT = "EXISTING_PROJECT"
    PRODUCTION_CODE = "PRODUCTION_CODE"
    BUILD_CONFIGURATION = "BUILD_CONFIGURATION"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    UNKNOWN = "UNKNOWN"


class RepairDecision(str, Enum):
    PROGRESS = "PROGRESS"
    PARTIAL_PROGRESS = "PARTIAL_PROGRESS"
    REGRESSION = "REGRESSION"
    REPEATED_ERROR = "REPEATED_ERROR"
    REPEATED_CODE = "REPEATED_CODE"
    NO_PROGRESS = "NO_PROGRESS"
    ACCEPTED = "ACCEPTED"
    INVALID_LLM_OUTPUT = "INVALID_LLM_OUTPUT"
    STOP = "STOP"


class RepairStatus(str, Enum):
    NOT_NEEDED = "NOT_NEEDED"
    REPAIRED = "REPAIRED"
    REGENERATED = "REGENERATED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    EXISTING_PROJECT_FAILURE = "EXISTING_PROJECT_FAILURE"
    ATTRIBUTION_FAILED = "ATTRIBUTION_FAILED"


@dataclass
class SampleInput:
    project_id: str
    sample_file: Path
    repository_url: str
    focal_class_name: str
    focal_class_path: str
    test_class_name: str
    test_class_path: str
    raw: dict[str, Any]

    @property
    def input_id(self) -> str:
        return self.sample_file.stem


@dataclass
class AgentConfig:
    name: str
    model: str
    temperature: float = 0.0
    api_base: str | None = None
    api_key_env: str | None = None
    num_ctx: int | None = None
    max_tokens: int | None = None
    stream: bool = False
    thinking: dict[str, Any] | None = None
    reasoning_effort: str | None = None


@dataclass
class GenerationStrategy:
    name: str
    template: str
    examples: str | None = None


@dataclass
class VerificationResult:
    state: FailureState | None
    failure_origin: FailureOrigin = FailureOrigin.UNKNOWN
    exit_code: int | None = None
    raw_output: str = ""
    primary_error: str = ""
    normalized_error_signature: str = ""
    error_signatures: list[str] = field(default_factory=list)
    failed_test_ids: list[str] = field(default_factory=list)
    compile_errors: int = 0
    test_failures: int = 0
    test_errors: int = 0
    timed_out: bool = False
    build_skipped: bool = False
    tool_name: str = ""
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value if self.state else None
        data["failure_origin"] = self.failure_origin.value
        return data

    @classmethod
    def skipped(cls, reason: str) -> "VerificationResult":
        return cls(
            state=None,
            build_skipped=True,
            primary_error=reason,
            normalized_error_signature=reason,
            error_signatures=[reason],
        )


@dataclass
class RepairConfig:
    enabled: bool = True
    retry_mode: str = "bounded"
    decision_mode: str = "state_machine"
    max_attempts_per_prompt: int = 2
    max_repair_attempts: int = 6
    max_regenerate_attempts: int = 1
    max_total_llm_attempts: int = 7
    unlimited_max_wall_clock_minutes: float = 120.0
    no_progress_patience: int = 2
    repeated_error_patience: int = 1
    max_build_timeout_retries: int = 1
    max_tool_error_retries: int = 1
    rollback_on_regression: bool = True
    rollback_on_repeated_error: bool = True
    rollback_on_repeated_code: bool = True
    switch_prompt_on: list[str] = field(default_factory=list)
    prompt_order_by_state: dict[str, list[str]] = field(default_factory=dict)
    allow_regenerate_after_prompt_fallback_fail: bool = True


@dataclass
class ExperimentContext:
    run_id: str
    shard_id: str
    input_id: str
    agent_name: str
    generation_prompt: str
    workspace: Path
    generated_test_path: Path
    generated_test_class_name: str
    package_name: str
    focal_class_source: str = ""
    java_version: str = "unknown"
    testing_framework: str = "unknown"
    build_tool: str = "unknown"
    module_path: str = "."
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    existing_tests: list[str] = field(default_factory=list)
    public_api: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class RepairSummary:
    repair_status: RepairStatus
    initial_failure_state: str = ""
    final_failure_state: str = ""
    best_candidate_state: str = ""
    initial_failure_origin: str = ""
    final_failure_origin: str = ""
    repair_attempts: int = 0
    regeneration_attempts: int = 0
    total_llm_attempts: int = 0
    build_attempts: int = 0
    initial_repair_prompt_strategy: str = ""
    final_repair_prompt_strategy: str = ""
    prompt_switch_count: int = 0
    rollback_count: int = 0
    repeated_error_signature: bool = False
    repeated_code_detected: bool = False
    no_progress_count: int = 0
    repair_stopped_reason: str = ""
    regenerated_after_repair_fail: bool = False
    best_candidate_hash: str = ""
    checkpoint_directory: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["repair_status"] = self.repair_status.value
        return data
