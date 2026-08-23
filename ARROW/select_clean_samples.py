#!/usr/bin/env python3
"""Chọn manifest one-sample-per-repository có baseline build/test sạch.

Script không gọi LLM và không sửa repository cache. Mỗi phép kiểm tra chạy trên
một workspace cô lập. Candidate được xếp hạng trước bằng seed cố định; kết quả
generation tuyệt đối không tham gia vào việc chọn sample.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import shutil
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.build_runner import BuildContext, module_test_command, run_command, verify_baseline, verify_module_tests, verify_target_test
from src.evosuite_runner import (
    find_focal_bytecode,
    focal_fqcn,
    java_environment,
    resolve_focal_path,
    resolve_project_classpath,
    sha256_file,
)
from src.input_selector import load_sample
from src.java_resolver import resolve_java_home
from src.models import FailureState, SampleInput, VerificationResult
from src.project_analyzer import analyze_experiment
from src.repo_manager import checkout_dataset_revision, clone_repo, ensure_experiment_workspace, safe_remove_tree
from src.run_pipeline import load_config


ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = ROOT.parent / "classes2test" / "dataset"
AUDIT_FIELDS = [
    "candidate_rank",
    "project_id",
    "sample_file",
    "repository_url",
    "focal_class",
    "focal_class_path",
    "source_layout",
    "class_kind",
    "is_abstract",
    "class_loc",
    "focal_method_name",
    "focal_method_loc",
    "focal_method_modifiers",
    "has_control_flow",
    "has_observable_output",
    "constructor_dependency_count",
    "external_dependency_risk",
    "external_risk_reasons",
    "testability_score",
    "semantic_status",
    "semantic_exclusion_reason",
    "build_tool",
    "module_path",
    "testing_framework",
    "detected_java_version",
    "selected_java_home",
    "java_selection_reason",
    "checkout_revision",
    "clone_attempts",
    "baseline_passes_required",
    "baseline_passes_completed",
    "baseline_states",
    "classpath_resolved",
    "classpath_exit_code",
    "focal_class_fqcn",
    "focal_bytecode_found",
    "probe_target_state",
    "probe_module_state",
    "offline_baseline_state",
    "eligibility_status",
    "exclusion_stage",
    "exclusion_reason",
    "elapsed_seconds",
    "artifact_dir",
]
MANIFEST_FIELDS = [
    "rank",
    "candidate_rank",
    "project_id",
    "sample_file",
    "repository_url",
    "focal_class",
    "focal_class_path",
    "class_kind",
    "focal_method_name",
    "testability_score",
    "build_tool",
    "java_version",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chọn sample baseline-clean trước khi chạy LLM/EvoSuite."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "pipeline.yaml")
    parser.add_argument("--run-id", default="", help="Dùng lại run-id để resume.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--target", type=int, default=200)
    parser.add_argument("--reserve", type=int, default=50)
    parser.add_argument("--candidate-count", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--baseline-repeats", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=900, help="Timeout mỗi lệnh build, giây.")
    parser.add_argument("--clone-attempts", type=int, default=3)
    parser.add_argument("--clone-backoff-seconds", type=float, default=2.0)
    parser.add_argument("--min-testability-score", type=int, default=7)
    parser.add_argument(
        "--include-non-concrete",
        action="store_true",
        help="Cho phép interface/abstract/enum/record; mặc định loại.",
    )
    parser.add_argument(
        "--allow-external-risk",
        action="store_true",
        help="Cho phép class có dấu hiệu DB/network/framework/runtime ngoài.",
    )
    parser.add_argument(
        "--allow-nonstandard-source-layout",
        action="store_true",
        help="Cho phép focal source không nằm dưới src/main/java.",
    )
    parser.add_argument("--java-home", default="", help="Không khuyến nghị cho dataset nhiều JDK.")
    parser.add_argument(
        "--project-shard",
        type=Path,
        action="append",
        default=[],
        help="File project_id; có thể lặp lại. Mặc định dùng toàn dataset.",
    )
    parser.add_argument(
        "--exclude-manifest",
        type=Path,
        action="append",
        default=[],
        help="Loại project đã dùng trong manifest cũ; có thể lặp lại.",
    )
    parser.add_argument("--skip-classpath-check", action="store_true")
    parser.add_argument("--skip-probe-test", action="store_true")
    parser.add_argument("--skip-offline-check", action="store_true")
    parser.add_argument("--keep-all-repo-cache", action="store_true")
    parser.add_argument("--delete-eligible-repo-cache", action="store_true")
    parser.add_argument("--keep-failed-workspaces", action="store_true")
    parser.add_argument(
        "--candidate-manifest",
        type=Path,
        default=None,
        help="Candidate manifest đã khóa và được dùng chung giữa các máy.",
    )
    parser.add_argument("--prepare-only", action="store_true", help="Chỉ tạo candidate manifest, không clone/build.")
    parser.add_argument(
        "--export-dataset-dir",
        type=Path,
        default=None,
        help="Export đúng các sample JSON candidate theo layout dataset/<project>/<sample>.",
    )
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0, help="Chỉ số shard zero-based.")
    parser.add_argument(
        "--merge-shard-dir",
        type=Path,
        action="append",
        default=[],
        help="Merge preflight_audit.csv từ thư mục shard; lặp lại cho từng máy.",
    )
    parser.add_argument("--no-resume", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in (
        "target",
        "candidate_count",
        "workers",
        "batch_size",
        "baseline_repeats",
        "timeout",
        "clone_attempts",
        "shard_count",
    ):
        if getattr(args, name) < 1:
            raise ValueError(f"--{name.replace('_', '-')} phải >= 1")
    if args.reserve < 0:
        raise ValueError("--reserve phải >= 0")
    if not args.candidate_manifest and args.target + args.reserve > args.candidate_count:
        raise ValueError("target + reserve không được lớn hơn candidate-count")
    if args.clone_backoff_seconds < 0:
        raise ValueError("--clone-backoff-seconds phải >= 0")
    if not 0 <= args.min_testability_score <= 10:
        raise ValueError("--min-testability-score phải trong khoảng 0..10")
    if not 0 <= args.shard_index < args.shard_count:
        raise ValueError("--shard-index phải thỏa 0 <= index < shard-count")
    if args.keep_all_repo_cache and args.delete_eligible_repo_cache:
        raise ValueError("Không thể dùng đồng thời --keep-all-repo-cache và --delete-eligible-repo-cache")
    if args.prepare_only and args.merge_shard_dir:
        raise ValueError("Không thể dùng --prepare-only cùng --merge-shard-dir")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_project_ids(paths: Iterable[Path]) -> set[str]:
    selected: set[str] = set()
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
        selected.update(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return selected


def _read_excluded_projects(paths: Iterable[Path]) -> set[str]:
    excluded: set[str] = set()
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                project_id = str(row.get("project_id", "")).strip()
                if project_id:
                    excluded.add(project_id)
    return excluded


def deterministic_sample_file(project_dir: Path, seed: int) -> Path:
    samples = sorted(project_dir.glob("*.json"), key=lambda path: path.name)
    if not samples:
        raise FileNotFoundError(f"Project không có sample JSON: {project_dir}")
    digest = hashlib.sha256(f"{seed}:{project_dir.name}".encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], byteorder="big") % len(samples)
    return samples[index]


def build_candidates(
    dataset: Path,
    *,
    seed: int,
    candidate_count: int,
    included_projects: set[str] | None = None,
    excluded_projects: set[str] | None = None,
) -> list[dict[str, str]]:
    excluded = excluded_projects or set()
    projects = [
        path
        for path in dataset.iterdir()
        if path.is_dir()
        and path.name not in excluded
        and (not included_projects or path.name in included_projects)
        and any(path.glob("*.json"))
    ]
    projects.sort(key=lambda path: path.name)
    random.Random(seed).shuffle(projects)
    rows: list[dict[str, str]] = []
    for project in projects[:candidate_count]:
        sample = deterministic_sample_file(project, seed)
        rows.append(
            {
                "candidate_rank": str(len(rows) + 1),
                "project_id": project.name,
                "sample_file": sample.name,
                "sample_sha256": sha256_file(sample),
                "sample_bytes": str(sample.stat().st_size),
            }
        )
    return rows


def export_candidate_dataset(
    candidates: list[dict[str, str]],
    *,
    source_dataset: Path,
    destination_dataset: Path,
) -> dict[str, Any]:
    destination_dataset.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    for row in candidates:
        source = source_dataset / row["project_id"] / row["sample_file"]
        if not source.is_file():
            raise FileNotFoundError(source)
        expected_hash = str(row.get("sample_sha256", ""))
        actual_hash = sha256_file(source)
        if expected_hash and actual_hash != expected_hash:
            raise ValueError(f"Sample hash thay đổi trước export: {source}")
        destination = destination_dataset / row["project_id"] / row["sample_file"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if sha256_file(destination) != actual_hash:
            raise IOError(f"Sample hash không khớp sau export: {destination}")
        total_bytes += destination.stat().st_size
    provenance = {
        "sample_count": len(candidates),
        "total_bytes": total_bytes,
        "source_dataset": str(source_dataset),
        "destination_dataset": str(destination_dataset),
    }
    (destination_dataset / "dataset_provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return provenance


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fields or list(rows[0]) if rows else (fields or [])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _state(result: VerificationResult | None) -> str:
    return result.state.value if result and result.state else ""


def _passed(result: VerificationResult | None) -> bool:
    return bool(result and result.state == FailureState.MODULE_TESTS_PASSED)


def _reason(result: VerificationResult | None, fallback: str) -> str:
    if result is None:
        return fallback
    return (result.primary_error or result.normalized_error_signature or fallback)[:2000]


EXTERNAL_RISK_PATTERNS = {
    "database": re.compile(
        r"\b(?:java\.sql|javax\.sql|jakarta\.persistence|EntityManager|JdbcTemplate|"
        r"MongoTemplate|RedisTemplate|DataSource|Connection|Repository)\b",
        re.IGNORECASE,
    ),
    "network": re.compile(
        r"\b(?:HttpClient|HttpURLConnection|URLConnection|RestTemplate|WebClient|Socket|ServerSocket|"
        r"OkHttpClient|Retrofit)\b",
        re.IGNORECASE,
    ),
    "framework_context": re.compile(
        r"\b(?:ApplicationContext|SpringApplication|SpringBootTest|ContextConfiguration|InjectMocks|"
        r"Activity|Fragment|AndroidJUnit|ServletContext)\b",
        re.IGNORECASE,
    ),
    "filesystem": re.compile(
        r"\b(?:java\.io\.File|FileInputStream|FileOutputStream|Files\s*\.|Paths\s*\.|RandomAccessFile)\b"
    ),
    "process_or_native": re.compile(
        r"\b(?:Runtime\.getRuntime|ProcessBuilder|System\.loadLibrary|\bnative\s+[A-Za-z_])\b"
    ),
    "nondeterminism": re.compile(
        r"\b(?:System\.currentTimeMillis|System\.nanoTime|Thread\.sleep|SecureRandom|new\s+Random\s*\()"
    ),
}


def _strip_java_comments_and_literals(source: str) -> str:
    """Che nội dung comment/literal, giữ newline để regex/LOC ổn định."""
    pattern = re.compile(
        r'(?P<block>/\*.*?\*/)|(?P<line>//[^\n]*)|(?P<text>""".*?""")|'
        r'(?P<string>"(?:\\.|[^"\\])*"\s*)|(?P<char>\'(?:\\.|[^\'\\])\')',
        re.DOTALL,
    )

    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        return "".join("\n" if char == "\n" else " " for char in value)

    return pattern.sub(replace, source)


def _source_layout(path_text: str) -> str:
    normalized = path_text.replace("\\", "/").lower()
    if any(token in normalized for token in ("generated-sources", "/generated/", "/target/", "/build/generated/")):
        return "generated"
    if "/src/test/" in f"/{normalized.lstrip('/')}":
        return "test"
    if "/src/main/java/" in f"/{normalized.lstrip('/')}":
        return "production"
    if "/archetype-resources/" in f"/{normalized.lstrip('/')}":
        return "template"
    return "nonstandard"


def _class_declaration(source: str, class_name: str) -> tuple[str, bool]:
    cleaned = _strip_java_comments_and_literals(source)
    declaration = re.search(
        rf"(?m)(?P<prefix>(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed)\s+)*)"
        rf"(?P<kind>@interface|class|interface|enum|record)\s+{re.escape(class_name)}\b",
        cleaned,
    )
    if not declaration:
        return "unknown", False
    kind = declaration.group("kind")
    return kind, kind in {"interface", "@interface"} or "abstract" in declaration.group("prefix").split()


def _parameter_parts(parameters: str) -> list[str]:
    text = parameters.strip().strip("() ")
    if not text:
        return []
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _parameters_are_simple(parameters: str) -> bool:
    parts = _parameter_parts(parameters)
    if not parts:
        return True
    simple_types = re.compile(
        r"^(?:final\s+)?(?:byte|short|int|long|float|double|boolean|char|String|"
        r"Byte|Short|Integer|Long|Float|Double|Boolean|Character|BigDecimal|BigInteger|"
        r"List|Set|Map|Collection|Iterable|Optional)(?:\s*<[^>]+>)?(?:\[\])?(?:\.\.\.)?\s+\w+$"
    )
    return all(simple_types.match(re.sub(r"@[A-Za-z_][\w.]*\s*", "", part)) for part in parts)


def _constructor_profile(sample: SampleInput, source: str) -> tuple[int, bool]:
    methods = sample.raw.get("focal_class", {}).get("methods", [])
    constructors = [method for method in methods if method.get("constructor")]
    accessible = [
        method
        for method in constructors
        if "private" not in str(method.get("modifiers", "")).split()
    ]
    if accessible:
        dependency_count = min(len(_parameter_parts(str(method.get("parameters", "")))) for method in accessible)
        return dependency_count, True
    if not constructors:
        return 0, True
    factory = re.search(
        rf"(?m)\bpublic\s+static\s+(?:[\w<>?,.\s]+\s+)?{re.escape(sample.focal_class_name)}\s+\w+\s*\(",
        _strip_java_comments_and_literals(source),
    )
    return min(len(_parameter_parts(str(method.get("parameters", "")))) for method in constructors), bool(factory)


def _is_trivial_method(method_name: str, body: str) -> bool:
    cleaned = re.sub(r"/\*.*?\*/|//[^\n]*", " ", body, flags=re.DOTALL).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        cleaned = cleaned[1:-1].strip()
    compact = re.sub(r"\s+", " ", cleaned).strip()
    if not compact:
        return True
    trivial_patterns = (
        r"return\s+(?:this\.)?[A-Za-z_][\w.]*\s*;",
        r"return\s+(?:true|false|null|[-+]?\d+(?:\.\d+)?[fFdDlL]?|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])')\s*;",
        r"(?:this\.)?\w+\s*=\s*\w+\s*;",
        r"return\s+(?:this\.)?\w+\.\w+\([^;]*\)\s*;",
        r"super\.\w+\([^;]*\)\s*;",
    )
    if any(re.fullmatch(pattern, compact) for pattern in trivial_patterns):
        return True
    trivial_names = {"tostring", "hashcode", "clone"}
    getter_setter = re.fullmatch(r"(?:get|set|is)[A-Z_].*", method_name or "")
    statement_count = compact.count(";")
    return statement_count <= 1 and ((method_name or "").lower() in trivial_names or getter_setter is not None)


def analyze_metadata_testability(
    sample: SampleInput,
    *,
    allow_nonstandard_source_layout: bool,
) -> dict[str, Any]:
    """Loại trường hợp chắc chắn không testable trước khi tốn chi phí clone."""
    focal_method = sample.raw.get("focal_method", {}) or {}
    method_name = str(focal_method.get("identifier", ""))
    method_body = str(focal_method.get("body", "") or "")
    method_modifiers = str(focal_method.get("modifiers", "") or "")
    method_return = str(focal_method.get("return", "") or "")
    cleaned_body = _strip_java_comments_and_literals(method_body)
    source_layout = _source_layout(sample.focal_class_path)
    control_flow = bool(
        re.search(r"\b(?:if|else|switch|case|for|while|do|try|catch|throw)\b|\?|&&|\|\|", cleaned_body)
    )
    observable = (
        bool(method_return and method_return.lower() != "void")
        or "throw " in cleaned_body
        or bool(
            re.search(
                r"\bthis\.\w+\s*(?:\+\+|--|[+\-*/%]?=)|\bthis\.\w+\.\w+\s*\(",
                cleaned_body,
            )
        )
    )
    reasons: list[str] = []
    if source_layout != "production" and not allow_nonstandard_source_layout:
        reasons.append(f"source_layout:{source_layout}")
    if not method_name or not method_body.strip():
        reasons.append("focal_method_has_no_body")
    if set(method_modifiers.split()).intersection({"private", "abstract", "native"}):
        reasons.append("focal_method_not_directly_testable")
    if _is_trivial_method(method_name, method_body):
        reasons.append("trivial_focal_method")
    if not observable:
        reasons.append("no_observable_behavior")
    return {
        "source_layout": source_layout,
        "focal_method_name": method_name,
        "focal_method_loc": sum(1 for line in method_body.splitlines() if line.strip()),
        "focal_method_modifiers": method_modifiers,
        "has_control_flow": control_flow,
        "has_observable_output": observable,
        "semantic_status": "ELIGIBLE" if not reasons else "EXCLUDED",
        "semantic_exclusion_reason": ";".join(reasons),
    }


def analyze_semantic_testability(
    sample: SampleInput,
    source: str,
    *,
    min_score: int,
    include_non_concrete: bool,
    allow_external_risk: bool,
    allow_nonstandard_source_layout: bool,
) -> dict[str, Any]:
    focal_method = sample.raw.get("focal_method", {}) or {}
    method_name = str(focal_method.get("identifier", ""))
    method_body = str(focal_method.get("body", "") or "")
    method_modifiers = str(focal_method.get("modifiers", "") or "")
    method_return = str(focal_method.get("return", "") or "")
    method_parameters = str(focal_method.get("parameters", "") or "")
    class_kind, is_abstract = _class_declaration(source, sample.focal_class_name)
    source_layout = _source_layout(sample.focal_class_path)
    cleaned_body = _strip_java_comments_and_literals(method_body)
    control_flow = bool(
        re.search(r"\b(?:if|else|switch|case|for|while|do|try|catch|throw)\b|\?|&&|\|\|", cleaned_body)
    )
    observable = (
        bool(method_return and method_return.lower() != "void")
        or "throw " in cleaned_body
        or bool(
            re.search(
                r"\bthis\.\w+\s*(?:\+\+|--|[+\-*/%]?=)|\bthis\.\w+\.\w+\s*\(",
                cleaned_body,
            )
        )
    )
    constructor_dependencies, instantiable = _constructor_profile(sample, source)
    is_static = "static" in method_modifiers.split()
    simple_parameters = _parameters_are_simple(method_parameters)
    cleaned_source = _strip_java_comments_and_literals(source)
    external_reasons = [
        name for name, pattern in EXTERNAL_RISK_PATTERNS.items() if pattern.search(cleaned_source)
    ]
    external_risk = bool(external_reasons)
    class_loc = sum(1 for line in source.splitlines() if line.strip())
    method_loc = sum(1 for line in method_body.splitlines() if line.strip())
    reasonable_size = 20 <= class_loc <= 500

    score = 0
    score += 2 if control_flow else 0
    score += 2 if observable else 0
    score += 2 if instantiable or is_static else 0
    score += 1 if simple_parameters else 0
    score += 1 if not external_risk else 0
    score += 1 if constructor_dependencies <= 3 else 0
    score += 1 if reasonable_size else 0

    hard_reasons: list[str] = []
    if source_layout != "production" and not allow_nonstandard_source_layout:
        hard_reasons.append(f"source_layout:{source_layout}")
    if (class_kind != "class" or is_abstract) and not include_non_concrete:
        hard_reasons.append(f"non_concrete:{class_kind}{':abstract' if is_abstract else ''}")
    if not method_name or not method_body.strip():
        hard_reasons.append("focal_method_has_no_body")
    modifier_tokens = set(method_modifiers.split())
    if modifier_tokens.intersection({"private", "abstract", "native"}):
        hard_reasons.append("focal_method_not_directly_testable")
    if _is_trivial_method(method_name, method_body):
        hard_reasons.append("trivial_focal_method")
    if not observable:
        hard_reasons.append("no_observable_behavior")
    if external_risk and not allow_external_risk:
        hard_reasons.append("external_dependency_risk:" + "|".join(external_reasons))
    if score < min_score:
        hard_reasons.append(f"testability_score_below_{min_score}")

    return {
        "source_layout": source_layout,
        "class_kind": class_kind,
        "is_abstract": is_abstract,
        "class_loc": class_loc,
        "focal_method_name": method_name,
        "focal_method_loc": method_loc,
        "focal_method_modifiers": method_modifiers,
        "has_control_flow": control_flow,
        "has_observable_output": observable,
        "constructor_dependency_count": constructor_dependencies,
        "external_dependency_risk": external_risk,
        "external_risk_reasons": "|".join(external_reasons),
        "testability_score": score,
        "semantic_status": "ELIGIBLE" if not hard_reasons else "EXCLUDED",
        "semantic_exclusion_reason": ";".join(hard_reasons),
    }


def _probe_source(package_name: str, class_name: str, framework: str) -> str:
    package = f"package {package_name};\n\n" if package_name else ""
    if framework == "junit5":
        imports = "import org.junit.jupiter.api.Test;\nimport static org.junit.jupiter.api.Assertions.assertTrue;"
    elif framework == "junit4":
        imports = "import org.junit.Test;\nimport static org.junit.Assert.assertTrue;"
    elif framework == "testng":
        imports = "import org.testng.annotations.Test;\nimport static org.testng.Assert.assertTrue;"
    else:
        raise ValueError(f"Unsupported or unknown testing framework: {framework}")
    return (
        f"{package}{imports}\n\n"
        f"public class {class_name} {{\n"
        "    @Test\n"
        "    public void arrowInfrastructureProbe() {\n"
        "        assertTrue(true);\n"
        "    }\n"
        "}\n"
    )


def verify_probe(context: Any, build_context: BuildContext) -> tuple[VerificationResult, VerificationResult]:
    generated_path = context.generated_test_path
    ownership_path = generated_path.with_suffix(generated_path.suffix + ".agone-ownership.json")
    if generated_path.exists():
        raise FileExistsError(f"Probe path đã tồn tại: {generated_path}")
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    generated_path.write_text(
        _probe_source(context.package_name, context.generated_test_class_name, context.testing_framework),
        encoding="utf-8",
    )
    try:
        target = verify_target_test(build_context)
        module = verify_module_tests(build_context) if target.state == FailureState.TARGET_TEST_PASSED else VerificationResult.skipped("probe target did not pass")
        return target, module
    finally:
        generated_path.unlink(missing_ok=True)
        ownership_path.unlink(missing_ok=True)


def verify_offline_baseline(ctx: BuildContext) -> VerificationResult:
    tool, command, cwd = module_test_command(ctx)
    if tool == "maven":
        command.insert(1, "-o")
    elif tool == "gradle":
        command.append("--offline")
    return run_command(ctx, command, cwd, tool, target_only=False)


def _repo_cache_root(config: dict[str, Any]) -> Path:
    configured = Path(str(config.get("repo", {}).get("repos_dir", "repos")))
    return configured if configured.is_absolute() else ROOT / configured


def prepare_repository_with_retry(
    sample: SampleInput,
    config: dict[str, Any],
    *,
    attempts: int,
    backoff_seconds: float,
) -> tuple[Path, str, int]:
    cache_root = _repo_cache_root(config)
    destination = cache_root / sample.project_id
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            repository = clone_repo(sample.repository_url, destination)
            checkout = ""
            if config.get("repo", {}).get("checkout_commit", True):
                checkout = checkout_dataset_revision(repository, sample.focal_class_path, sample.test_class_path)
            return repository, checkout, attempt
        except Exception as exc:
            last_error = exc
            if destination.exists():
                safe_remove_tree(destination, cache_root)
            if attempt < attempts and backoff_seconds:
                time.sleep(min(30.0, backoff_seconds * (2 ** (attempt - 1))))
    assert last_error is not None
    raise RuntimeError(f"Repository setup failed after {attempts} attempts: {type(last_error).__name__}: {last_error}") from last_error


def _base_audit(candidate: dict[str, str], artifact_dir: Path, baseline_repeats: int) -> dict[str, Any]:
    return {
        **candidate,
        "repository_url": "",
        "focal_class": "",
        "focal_class_path": "",
        "source_layout": "",
        "class_kind": "",
        "is_abstract": "",
        "class_loc": "",
        "focal_method_name": "",
        "focal_method_loc": "",
        "focal_method_modifiers": "",
        "has_control_flow": "",
        "has_observable_output": "",
        "constructor_dependency_count": "",
        "external_dependency_risk": "",
        "external_risk_reasons": "",
        "testability_score": "",
        "semantic_status": "",
        "semantic_exclusion_reason": "",
        "build_tool": "",
        "module_path": "",
        "testing_framework": "",
        "detected_java_version": "",
        "selected_java_home": "",
        "java_selection_reason": "",
        "checkout_revision": "",
        "clone_attempts": "",
        "baseline_passes_required": baseline_repeats,
        "baseline_passes_completed": 0,
        "baseline_states": "",
        "classpath_resolved": "",
        "classpath_exit_code": "",
        "focal_class_fqcn": "",
        "focal_bytecode_found": "",
        "probe_target_state": "",
        "probe_module_state": "",
        "offline_baseline_state": "",
        "eligibility_status": "EXCLUDED",
        "exclusion_stage": "setup",
        "exclusion_reason": "",
        "elapsed_seconds": "",
        "artifact_dir": str(artifact_dir),
    }


def qualify_candidate(
    candidate: dict[str, str],
    *,
    dataset: Path,
    config: dict[str, Any],
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = time.monotonic()
    project_id = candidate["project_id"]
    sample_file = candidate["sample_file"]
    artifact_dir = output_dir / "candidates" / f"{int(candidate['candidate_rank']):05d}_{project_id}_{Path(sample_file).stem}"
    workspace = artifact_dir / "workspace"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    row = _base_audit(candidate, artifact_dir, args.baseline_repeats)
    cached_repo: Path | None = None
    try:
        sample_path = dataset / project_id / sample_file
        expected_sample_hash = str(candidate.get("sample_sha256", ""))
        if expected_sample_hash and sha256_file(sample_path) != expected_sample_hash:
            raise ValueError(f"Sample SHA256 không khớp candidate manifest: {sample_path}")
        sample = load_sample(sample_path, dataset)
        row.update(
            repository_url=sample.repository_url,
            focal_class=sample.focal_class_name,
            focal_class_path=sample.focal_class_path,
        )
        if not sample.repository_url:
            raise ValueError("Sample không có repository URL")
        metadata_semantic = analyze_metadata_testability(
            sample,
            allow_nonstandard_source_layout=args.allow_nonstandard_source_layout,
        )
        row.update(metadata_semantic)
        if metadata_semantic["semantic_status"] != "ELIGIBLE":
            row.update(
                exclusion_stage="semantic_metadata",
                exclusion_reason=metadata_semantic["semantic_exclusion_reason"],
            )
            return row
        cached_repo, checkout, clone_attempts = prepare_repository_with_retry(
            sample,
            config,
            attempts=args.clone_attempts,
            backoff_seconds=args.clone_backoff_seconds,
        )
        row.update(checkout_revision=checkout, clone_attempts=clone_attempts)
        ensure_experiment_workspace(cached_repo=cached_repo, experiment_workspace=workspace)
        context, module_root = analyze_experiment(
            sample=sample,
            workspace=workspace,
            run_id=args.run_id,
            shard_id="clean-selection",
            agent_name="preflight",
            generation_prompt="infrastructure-probe",
        )
        focal_source_path = resolve_focal_path(workspace, sample.focal_class_path)
        focal_source = focal_source_path.read_text(encoding="utf-8", errors="replace")
        semantic = analyze_semantic_testability(
            sample,
            focal_source,
            min_score=args.min_testability_score,
            include_non_concrete=args.include_non_concrete,
            allow_external_risk=args.allow_external_risk,
            allow_nonstandard_source_layout=args.allow_nonstandard_source_layout,
        )
        row.update(semantic)
        (artifact_dir / "semantic_analysis.json").write_text(
            json.dumps(semantic, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if semantic["semantic_status"] != "ELIGIBLE":
            row.update(
                exclusion_stage="semantic",
                exclusion_reason=semantic["semantic_exclusion_reason"],
            )
            return row
        selection = resolve_java_home(workspace, module_root, config, args.java_home or None)
        build_cfg = config.get("build", {})
        maven_cfg = build_cfg.get("maven", {})
        fqcn = focal_fqcn(sample, workspace)
        build_context = BuildContext(
            repository_root=workspace,
            module_root=module_root,
            build_tool=context.build_tool,
            generated_test_class_name=context.generated_test_class_name,
            generated_test_fqcn=(f"{context.package_name}.{context.generated_test_class_name}" if context.package_name else context.generated_test_class_name),
            timeout_seconds=args.timeout,
            prefer_wrapper=bool(build_cfg.get("prefer_wrapper", True)),
            java_home=selection.java_home or None,
            maven_multi_module_strategy=maven_cfg.get("multi_module_strategy", "module_only"),
            maven_use_also_make=bool(maven_cfg.get("use_also_make", True)),
            maven_fail_if_no_specified_tests=bool(maven_cfg.get("fail_if_no_specified_tests", False)),
        )
        row.update(
            build_tool=context.build_tool,
            module_path=context.module_path,
            testing_framework=context.testing_framework,
            detected_java_version=context.java_version,
            selected_java_home=selection.java_home,
            java_selection_reason=selection.reason,
            focal_class_fqcn=fqcn,
        )

        baseline_states: list[str] = []
        for attempt in range(1, args.baseline_repeats + 1):
            baseline = verify_baseline(build_context)
            baseline_states.append(_state(baseline))
            (artifact_dir / f"baseline_{attempt}.log").write_text(baseline.raw_output, encoding="utf-8")
            row.update(baseline_passes_completed=attempt, baseline_states="|".join(baseline_states))
            if not _passed(baseline):
                row.update(exclusion_stage="baseline", exclusion_reason=_reason(baseline, "baseline did not pass"))
                return row

        if not args.skip_classpath_check:
            classpath, classpath_result = resolve_project_classpath(
                build_context,
                artifact_dir,
                java_environment(selection.java_home or None),
                args.timeout,
            )
            (artifact_dir / "classpath.log").write_text(classpath_result.output, encoding="utf-8")
            bytecode = find_focal_bytecode(classpath, fqcn)
            row.update(
                classpath_resolved=bool(classpath),
                classpath_exit_code=classpath_result.exit_code,
                focal_bytecode_found=bytecode is not None,
            )
            if not classpath:
                row.update(exclusion_stage="classpath", exclusion_reason="Không resolve được project classpath")
                return row
            if bytecode is None:
                row.update(exclusion_stage="bytecode", exclusion_reason=f"Không tìm thấy bytecode focal class {fqcn}")
                return row

        if not args.skip_probe_test:
            try:
                target, module = verify_probe(context, build_context)
            except ValueError as exc:
                row.update(exclusion_stage="probe", exclusion_reason=str(exc))
                return row
            (artifact_dir / "probe_target.log").write_text(target.raw_output, encoding="utf-8")
            (artifact_dir / "probe_module.log").write_text(module.raw_output, encoding="utf-8")
            row.update(probe_target_state=_state(target), probe_module_state=_state(module))
            if target.state != FailureState.TARGET_TEST_PASSED:
                row.update(exclusion_stage="probe_target", exclusion_reason=_reason(target, "probe target did not pass"))
                return row
            if module.state != FailureState.MODULE_TESTS_PASSED:
                row.update(exclusion_stage="probe_module", exclusion_reason=_reason(module, "module failed with probe"))
                return row

        if not args.skip_offline_check:
            offline = verify_offline_baseline(build_context)
            (artifact_dir / "offline_baseline.log").write_text(offline.raw_output, encoding="utf-8")
            row["offline_baseline_state"] = _state(offline)
            if not _passed(offline):
                row.update(exclusion_stage="offline", exclusion_reason=_reason(offline, "offline baseline did not pass"))
                return row

        row.update(eligibility_status="ELIGIBLE", exclusion_stage="", exclusion_reason="")
        return row
    except Exception as exc:
        row.update(exclusion_reason=f"{type(exc).__name__}: {exc}"[:2000])
        return row
    finally:
        row["elapsed_seconds"] = f"{time.monotonic() - started:.3f}"
        if workspace.exists() and not (args.keep_failed_workspaces and row["eligibility_status"] != "ELIGIBLE"):
            safe_remove_tree(workspace, artifact_dir)
        if cached_repo is not None and cached_repo.exists():
            should_delete = (
                args.delete_eligible_repo_cache
                if row["eligibility_status"] == "ELIGIBLE"
                else not args.keep_all_repo_cache
            )
            if should_delete:
                safe_remove_tree(cached_repo, _repo_cache_root(config))


def manifest_rows(rows: list[dict[str, Any]], *, start_rank: int = 1) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start_rank):
        output.append(
            {
                "rank": index,
                "candidate_rank": row["candidate_rank"],
                "project_id": row["project_id"],
                "sample_file": row["sample_file"],
                "repository_url": row.get("repository_url", ""),
                "focal_class": row.get("focal_class", ""),
                "focal_class_path": row.get("focal_class_path", ""),
                "class_kind": row.get("class_kind", ""),
                "focal_method_name": row.get("focal_method_name", ""),
                "testability_score": row.get("testability_score", ""),
                "build_tool": row.get("build_tool", ""),
                "java_version": row.get("detected_java_version", ""),
            }
        )
    return output


def select_final_and_reserve(
    audit_rows: list[dict[str, Any]], target: int, reserve: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    eligible = sorted(
        (row for row in audit_rows if row.get("eligibility_status") == "ELIGIBLE"),
        key=lambda row: int(row["candidate_rank"]),
    )
    final = eligible[:target]
    backup = eligible[target : target + reserve]
    return final, backup, eligible


def write_outputs(output_dir: Path, audit_rows: list[dict[str, Any]], target: int, reserve: int) -> dict[str, Any]:
    ordered = sorted(audit_rows, key=lambda row: int(row["candidate_rank"]))
    write_csv(output_dir / "preflight_audit.csv", ordered, AUDIT_FIELDS)
    final, backup, eligible = select_final_and_reserve(ordered, target, reserve)
    write_csv(output_dir / "eligible_manifest.csv", manifest_rows(eligible), MANIFEST_FIELDS)
    write_csv(output_dir / f"final_manifest_{target}.csv", manifest_rows(final), MANIFEST_FIELDS)
    write_csv(output_dir / f"reserve_manifest_{reserve}.csv", manifest_rows(backup), MANIFEST_FIELDS)
    statuses = Counter(str(row.get("eligibility_status", "UNKNOWN")) for row in ordered)
    stages = Counter(str(row.get("exclusion_stage", "")) for row in ordered if row.get("eligibility_status") != "ELIGIBLE")
    summary = {
        "processed": len(ordered),
        "eligible": len(eligible),
        "target_requested": target,
        "target_selected": len(final),
        "reserve_requested": reserve,
        "reserve_selected": len(backup),
        "enough_samples": len(final) == target and len(backup) == reserve,
        "status_counts": dict(statuses),
        "exclusion_stage_counts": dict(stages),
    }
    (output_dir / "selection_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def write_partitioned_manifests(
    output_dir: Path,
    audit_rows: list[dict[str, Any]],
    *,
    target: int,
    reserve: int,
    shard_count: int,
) -> None:
    final, backup, _eligible = select_final_and_reserve(audit_rows, target, reserve)
    final_rows = manifest_rows(final)
    backup_rows = manifest_rows(backup)
    for index in range(shard_count):
        final_part = [
            row for row in final_rows if (int(row["candidate_rank"]) - 1) % shard_count == index
        ]
        backup_part = [
            row for row in backup_rows if (int(row["candidate_rank"]) - 1) % shard_count == index
        ]
        write_csv(
            output_dir / f"final_manifest_{target}_shard_{index}_of_{shard_count}.csv",
            final_part,
            MANIFEST_FIELDS,
        )
        write_csv(
            output_dir / f"reserve_manifest_{reserve}_shard_{index}_of_{shard_count}.csv",
            backup_part,
            MANIFEST_FIELDS,
        )


def validate_candidate_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ValueError("Candidate manifest rỗng")
    required = {"candidate_rank", "project_id", "sample_file"}
    if not required.issubset(rows[0]):
        raise ValueError(f"Candidate manifest phải có cột {sorted(required)}")
    seen_ranks: set[int] = set()
    seen_projects: set[str] = set()
    for row in rows:
        try:
            rank = int(str(row.get("candidate_rank", "")))
        except ValueError as exc:
            raise ValueError(f"candidate_rank không hợp lệ: {row}") from exc
        project_id = str(row.get("project_id", "")).strip()
        sample_file = Path(str(row.get("sample_file", "")).strip()).name
        if rank < 1 or rank in seen_ranks:
            raise ValueError(f"candidate_rank thiếu hoặc lặp: {rank}")
        if not project_id or project_id in seen_projects:
            raise ValueError(f"project_id thiếu hoặc lặp: {project_id}")
        if not sample_file:
            raise ValueError(f"sample_file thiếu ở candidate rank {rank}")
        row["candidate_rank"] = str(rank)
        row["project_id"] = project_id
        row["sample_file"] = sample_file
        if row.get("sample_bytes"):
            try:
                if int(row["sample_bytes"]) < 1:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"sample_bytes không hợp lệ ở candidate rank {rank}") from exc
        seen_ranks.add(rank)
        seen_projects.add(project_id)
    expected = set(range(1, len(rows) + 1))
    if seen_ranks != expected:
        raise ValueError("candidate_rank phải liên tục từ 1 đến số candidate")
    rows.sort(key=lambda row: int(row["candidate_rank"]))


def shard_candidates(rows: list[dict[str, str]], shard_count: int, shard_index: int) -> list[dict[str, str]]:
    return [row for row in rows if (int(row["candidate_rank"]) - 1) % shard_count == shard_index]


def merge_shard_audits(
    shard_dirs: list[Path],
    *,
    candidate_manifest_sha256: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if not shard_dirs:
        raise ValueError("Cần ít nhất một --merge-shard-dir")
    merged: dict[str, dict[str, str]] = {}
    declared_count: int | None = None
    indexes: set[int] = set()
    source_runs: list[str] = []
    for directory in shard_dirs:
        resolved = directory.resolve()
        provenance_path = resolved / "provenance.json"
        audit_path = resolved / "preflight_audit.csv"
        if not provenance_path.is_file() or not audit_path.is_file():
            raise FileNotFoundError(f"Shard thiếu provenance.json hoặc preflight_audit.csv: {resolved}")
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("candidate_manifest_sha256") != candidate_manifest_sha256:
            raise ValueError(f"Candidate manifest hash không khớp ở shard {resolved}")
        count = int(provenance.get("shard_count", 1))
        index = int(provenance.get("shard_index", 0))
        if declared_count is None:
            declared_count = count
        elif declared_count != count:
            raise ValueError("Các shard khai báo shard_count khác nhau")
        if index in indexes:
            raise ValueError(f"shard_index bị lặp: {index}")
        indexes.add(index)
        source_runs.append(str(provenance.get("run_id", resolved.name)))
        for row in read_csv(audit_path):
            rank = str(row.get("candidate_rank", ""))
            if not rank:
                raise ValueError(f"Audit thiếu candidate_rank: {audit_path}")
            if (int(rank) - 1) % count != index:
                raise ValueError(f"Candidate rank {rank} nằm sai shard {index}/{count}")
            if rank in merged:
                raise ValueError(f"Candidate rank {rank} xuất hiện ở nhiều shard")
            merged[rank] = row
    assert declared_count is not None
    expected_indexes = set(range(declared_count))
    if indexes != expected_indexes:
        missing = sorted(expected_indexes - indexes)
        raise ValueError(f"Chưa đủ shard để merge; thiếu shard_index={missing}")
    ordered = sorted(merged.values(), key=lambda row: int(row["candidate_rank"]))
    return ordered, {
        "source_shard_count": declared_count,
        "source_shard_indexes": sorted(indexes),
        "source_run_ids": source_runs,
    }


def main() -> int:
    args = parse_args()
    validate_args(args)
    args.dataset = args.dataset.resolve()
    args.config = args.config.resolve()
    args.run_id = args.run_id or f"clean-samples-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = (args.output_dir.resolve() if args.output_dir else ROOT / "runs" / "sample_selection" / args.run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)

    generated_candidate_path = output_dir / "candidate_manifest.csv"
    if args.candidate_manifest:
        candidate_path = args.candidate_manifest.resolve()
        candidates = read_csv(candidate_path)
        print(f"[locked] dùng candidate manifest {candidate_path}")
    elif generated_candidate_path.is_file() and not args.no_resume:
        candidate_path = generated_candidate_path
        candidates = read_csv(candidate_path)
        print(f"[resume] dùng lại {len(candidates)} candidate từ {candidate_path}")
    else:
        included = _read_project_ids(path.resolve() for path in args.project_shard)
        excluded = _read_excluded_projects(path.resolve() for path in args.exclude_manifest)
        candidates = build_candidates(
            args.dataset,
            seed=args.seed,
            candidate_count=args.candidate_count,
            included_projects=included or None,
            excluded_projects=excluded,
        )
        if len(candidates) < args.target + args.reserve:
            raise RuntimeError(
                f"Chỉ tạo được {len(candidates)} candidate, không đủ target + reserve = {args.target + args.reserve}"
            )
        candidate_path = generated_candidate_path
        write_csv(
            candidate_path,
            candidates,
            ["candidate_rank", "project_id", "sample_file", "sample_sha256", "sample_bytes"],
        )

    validate_candidate_rows(candidates)
    if len(candidates) < args.target + args.reserve:
        raise RuntimeError(
            f"Candidate manifest có {len(candidates)} hàng, không đủ target + reserve = {args.target + args.reserve}"
        )
    candidate_hash = sha256_file(candidate_path)
    exported_dataset: dict[str, Any] | None = None
    if args.export_dataset_dir:
        exported_dataset = export_candidate_dataset(
            candidates,
            source_dataset=args.dataset,
            destination_dataset=args.export_dataset_dir.resolve(),
        )
        print(
            f"Exported {exported_dataset['sample_count']} sample JSON "
            f"({exported_dataset['total_bytes']} bytes) tới {args.export_dataset_dir.resolve()}"
        )
    if args.prepare_only:
        provenance = {
            "run_id": args.run_id,
            "prepared_at_utc": utc_now(),
            "dataset": str(args.dataset),
            "seed": args.seed,
            "candidate_count": len(candidates),
            "candidate_manifest": str(candidate_path),
            "candidate_manifest_sha256": candidate_hash,
            "exported_dataset": exported_dataset,
            "selection_rule": "seeded repository order; one deterministic sample per repository",
            "generation_results_used_for_selection": False,
        }
        (output_dir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Candidate manifest: {candidate_path}")
        print(f"SHA256: {candidate_hash}")
        return 0

    if args.merge_shard_dir:
        merged_rows, merge_meta = merge_shard_audits(
            args.merge_shard_dir,
            candidate_manifest_sha256=candidate_hash,
        )
        summary = write_outputs(output_dir, merged_rows, args.target, args.reserve)
        write_partitioned_manifests(
            output_dir,
            merged_rows,
            target=args.target,
            reserve=args.reserve,
            shard_count=int(merge_meta["source_shard_count"]),
        )
        provenance = {
            "run_id": args.run_id,
            "finished_at_utc": utc_now(),
            "mode": "merge",
            "candidate_manifest": str(candidate_path),
            "candidate_manifest_sha256": candidate_hash,
            "target": args.target,
            "reserve": args.reserve,
            **merge_meta,
            "generation_results_used_for_selection": False,
        }
        (output_dir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"Final manifest: {output_dir / f'final_manifest_{args.target}.csv'}")
        return 0 if summary["enough_samples"] else 2

    assigned_candidates = shard_candidates(candidates, args.shard_count, args.shard_index)
    if not assigned_candidates:
        raise RuntimeError(f"Shard {args.shard_index}/{args.shard_count} không có candidate")

    prior_rows = [] if args.no_resume else read_csv(output_dir / "preflight_audit.csv")
    completed = {str(row.get("candidate_rank", "")): row for row in prior_rows}
    audit_rows: list[dict[str, Any]] = list(completed.values())
    needed = args.target + args.reserve
    summary = write_outputs(output_dir, audit_rows, args.target, args.reserve)

    for offset in range(0, len(assigned_candidates), args.batch_size):
        if args.shard_count == 1 and summary["eligible"] >= needed:
            break
        batch = [
            row
            for row in assigned_candidates[offset : offset + args.batch_size]
            if row["candidate_rank"] not in completed
        ]
        if not batch:
            continue
        print(
            f"[batch] candidate {batch[0]['candidate_rank']}..{batch[-1]['candidate_rank']}; "
            f"eligible={summary['eligible']}/{needed}; workers={min(args.workers, len(batch))}",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=min(args.workers, len(batch)), thread_name_prefix="clean-sample") as executor:
            futures = {
                executor.submit(
                    qualify_candidate,
                    row,
                    dataset=args.dataset,
                    config=config,
                    output_dir=output_dir,
                    args=args,
                ): row
                for row in batch
            }
            batch_results: list[dict[str, Any]] = []
            for future in as_completed(futures):
                result = future.result()
                batch_results.append(result)
                print(
                    f"[{result['eligibility_status']}] rank={result['candidate_rank']} "
                    f"{result['project_id']}/{result['sample_file']} stage={result['exclusion_stage'] or 'complete'}",
                    flush=True,
                )
        for result in batch_results:
            completed[str(result["candidate_rank"])] = result
        audit_rows = list(completed.values())
        summary = write_outputs(output_dir, audit_rows, args.target, args.reserve)

    provenance = {
        "run_id": args.run_id,
        "finished_at_utc": utc_now(),
        "dataset": str(args.dataset),
        "config": str(args.config),
        "config_sha256": sha256_file(args.config),
        "seed": args.seed,
        "candidate_count": len(candidates),
        "target": args.target,
        "reserve": args.reserve,
        "baseline_repeats": args.baseline_repeats,
        "classpath_check": not args.skip_classpath_check,
        "probe_test": not args.skip_probe_test,
        "offline_check": not args.skip_offline_check,
        "candidate_manifest_sha256": sha256_file(candidate_path),
        "final_manifest_sha256": sha256_file(output_dir / f"final_manifest_{args.target}.csv"),
        "shard_count": args.shard_count,
        "shard_index": args.shard_index,
        "assigned_candidates": len(assigned_candidates),
        "processed_candidates": len(audit_rows),
        "min_testability_score": args.min_testability_score,
        "include_non_concrete": args.include_non_concrete,
        "allow_external_risk": args.allow_external_risk,
        "allow_nonstandard_source_layout": args.allow_nonstandard_source_layout,
        "selection_rule": "seeded repository order; one deterministic sample per repository; first eligible candidates",
        "generation_results_used_for_selection": False,
    }
    (output_dir / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Final manifest:   {output_dir / f'final_manifest_{args.target}.csv'}")
    print(f"Reserve manifest: {output_dir / f'reserve_manifest_{args.reserve}.csv'}")
    print(f"Audit:            {output_dir / 'preflight_audit.csv'}")
    if args.shard_count > 1:
        print(
            f"Shard {args.shard_index}/{args.shard_count} hoàn tất; copy cả thư mục {output_dir} về máy merge."
        )
        return 0
    return 0 if summary["enough_samples"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
