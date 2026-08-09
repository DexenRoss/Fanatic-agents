"""Read-only, deterministic repository inspection."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from fanatic_agents.core.path_safety import (
    EXCLUDED_DIRECTORY_NAMES,
    SECRET_NAME_TOKENS,
    SECRET_SUFFIXES,
)
from fanatic_agents.core.project import StrictModel

MAX_RELEVANT_FILES = 200
MAX_CONTENT_FILES = 20
MAX_SOURCE_CONTENT_FILES = 8
MAX_CHARACTERS_PER_FILE = 12_000
MAX_TOTAL_CHARACTERS = 50_000
GIT_COMMAND_TIMEOUT_SECONDS = 5.0
MAX_CONFIG_FILE_BYTES = 512_000

EXCLUDED_DIRECTORIES = EXCLUDED_DIRECTORY_NAMES

IMPORTANT_FILE_NAMES = frozenset({
    "agents.md", "readme.md", "pyproject.toml", "requirements.txt", "setup.py",
    "setup.cfg", "package.json", "tsconfig.json", "pubspec.yaml", "pom.xml",
    "build.gradle", "build.gradle.kts", "cargo.toml", "go.mod",
})
LOCKFILE_NAMES = frozenset({
    "cargo.lock", "package-lock.json", "pnpm-lock.yaml", "poetry.lock",
    "pubspec.lock", "uv.lock", "yarn.lock",
})
ENTRYPOINT_FILE_NAMES = frozenset({
    "app.py", "main.py", "manage.py", "index.js", "index.jsx",
    "index.ts", "index.tsx", "main.dart", "main.go", "main.rs",
})
SOURCE_FILE_SUFFIXES = frozenset({
    ".cs", ".dart", ".go", ".java", ".js", ".jsx", ".kt", ".kts",
    ".php", ".py", ".rb", ".rs", ".swift", ".ts", ".tsx",
})
SOURCE_ROOT_NAMES = frozenset({"app", "cmd", "lib", "packages", "src"})
ARCHITECTURE_ROLE_NAMES = frozenset({
    "agent", "agents", "api", "application", "cli", "command", "commands",
    "core", "domain", "entity", "entities", "git", "inspection", "inspector",
    "model", "models", "orchestration", "orchestrator", "repositories",
    "repository", "server", "service", "services",
})
TEST_DIRECTORY_NAMES = frozenset({"spec", "specs", "test", "tests"})


class RepositoryInspectionError(ValueError):
    """Raised when a repository path cannot be inspected safely."""


class SnapshotFile(StrictModel):
    """Bounded text from one repository file."""

    path: str
    content: str
    truncated: bool = False


class SnapshotTruncation(StrictModel):
    """Limits applied while constructing a repository snapshot."""

    max_relevant_files: int
    max_content_files: int
    max_source_content_files: int
    max_characters_per_file: int
    max_total_characters: int
    files_considered: int
    relevant_files_included: int
    relevant_files_omitted: int
    content_files_included: int
    content_files_omitted: int
    truncated_files: int
    total_characters: int
    content_included_paths: list[str] = Field(default_factory=list)
    content_omitted_paths: list[str] = Field(default_factory=list)
    content_truncated_paths: list[str] = Field(default_factory=list)


class RepositorySnapshot(StrictModel):
    """Safe, bounded repository context suitable for an LLM."""

    repository_name: str
    is_git_repository: bool
    current_branch: str | None = None
    detached_head: bool = False
    working_tree_clean: bool | None = None
    detected_technologies: list[str] = Field(default_factory=list)
    important_files: list[str] = Field(default_factory=list)
    relevant_paths: list[str] = Field(default_factory=list)
    inferred_test_commands: list[str] = Field(default_factory=list)
    inferred_build_commands: list[str] = Field(default_factory=list)
    files: list[SnapshotFile] = Field(default_factory=list)
    truncation: SnapshotTruncation

    def has_agent_context(self) -> bool:
        """Return whether the snapshot contains useful context for assessment."""
        return bool(self.detected_technologies or self.relevant_paths or self.files)


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    """Configurable bounds for repository context collection."""

    max_relevant_files: int = MAX_RELEVANT_FILES
    max_content_files: int = MAX_CONTENT_FILES
    max_source_content_files: int = MAX_SOURCE_CONTENT_FILES
    max_characters_per_file: int = MAX_CHARACTERS_PER_FILE
    max_total_characters: int = MAX_TOTAL_CHARACTERS

    def __post_init__(self) -> None:
        for name, value in (
            ("max_relevant_files", self.max_relevant_files),
            ("max_content_files", self.max_content_files),
            ("max_source_content_files", self.max_source_content_files),
            ("max_characters_per_file", self.max_characters_per_file),
            ("max_total_characters", self.max_total_characters),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")


@dataclass(frozen=True, slots=True)
class _GitState:
    is_repository: bool
    branch: str | None = None
    detached_head: bool = False
    working_tree_clean: bool | None = None


class RepositoryInspector:
    """Build deterministic snapshots without executing repository code."""

    def __init__(
        self,
        limits: SnapshotLimits | None = None,
        *,
        git_timeout_seconds: float = GIT_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._limits = limits or SnapshotLimits()
        if git_timeout_seconds <= 0:
            raise ValueError("git_timeout_seconds must be greater than zero")
        self._git_timeout_seconds = git_timeout_seconds

    def inspect(self, repository: Path) -> RepositorySnapshot:
        """Inspect ``repository`` in read-only mode and return a bounded snapshot."""
        path = Path(repository).expanduser()
        if not path.exists():
            raise RepositoryInspectionError(f"Repository path does not exist: {path}")
        if not path.is_dir():
            raise RepositoryInspectionError(f"Repository path is not a directory: {path}")

        root = path.resolve()
        git_state = self._inspect_git(root)
        candidates = sorted(self._collect_candidate_paths(root), key=_path_priority)
        relevant = candidates[: self._limits.max_relevant_files]
        relevant_paths = [item.as_posix() for item in relevant]
        important_files = [
            item.as_posix() for item in relevant
            if item.name.lower() in IMPORTANT_FILE_NAMES or item.name.lower() in LOCKFILE_NAMES
        ]
        files, content_omitted_paths, truncated_paths, total_characters = (
            self._collect_file_contents(root, relevant)
        )
        technologies, package_data, pyproject_data, pubspec_data = _detect_technologies(root)
        test_commands, build_commands = _infer_commands(
            root,
            package_data=package_data,
            pyproject_data=pyproject_data,
            pubspec_data=pubspec_data,
            technologies=technologies,
        )
        return RepositorySnapshot(
            repository_name=root.name,
            is_git_repository=git_state.is_repository,
            current_branch=git_state.branch,
            detached_head=git_state.detached_head,
            working_tree_clean=git_state.working_tree_clean,
            detected_technologies=technologies,
            important_files=important_files,
            relevant_paths=relevant_paths,
            inferred_test_commands=test_commands,
            inferred_build_commands=build_commands,
            files=files,
            truncation=SnapshotTruncation(
                max_relevant_files=self._limits.max_relevant_files,
                max_content_files=self._limits.max_content_files,
                max_source_content_files=self._limits.max_source_content_files,
                max_characters_per_file=self._limits.max_characters_per_file,
                max_total_characters=self._limits.max_total_characters,
                files_considered=len(candidates),
                relevant_files_included=len(relevant),
                relevant_files_omitted=max(0, len(candidates) - len(relevant)),
                content_files_included=len(files),
                content_files_omitted=len(content_omitted_paths),
                truncated_files=len(truncated_paths),
                total_characters=total_characters,
                content_included_paths=[file.path for file in files],
                content_omitted_paths=content_omitted_paths,
                content_truncated_paths=truncated_paths,
            ),
        )

    def _inspect_git(self, root: Path) -> _GitState:
        inside = self._run_git(root, "rev-parse", "--is-inside-work-tree")
        if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
            return _GitState(is_repository=False)
        branch_result = self._run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = None
        detached = False
        if branch_result is not None and branch_result.returncode == 0:
            branch = branch_result.stdout.strip() or None
        else:
            head_result = self._run_git(root, "rev-parse", "--verify", "HEAD")
            detached = head_result is not None and head_result.returncode == 0
        status = self._run_git(root, "status", "--porcelain", "--untracked-files=normal")
        clean = status is not None and status.returncode == 0 and not status.stdout.strip()
        return _GitState(
            is_repository=True,
            branch=branch,
            detached_head=detached,
            working_tree_clean=clean if status is not None and status.returncode == 0 else None,
        )

    def _run_git(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", *arguments], cwd=root, capture_output=True, check=False,
                shell=False, text=True, timeout=self._git_timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    def _collect_candidate_paths(self, root: Path) -> list[Path]:
        candidates: list[Path] = []
        for current_root, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            current = Path(current_root)
            directory_names[:] = sorted(
                name for name in directory_names
                if name.lower() not in EXCLUDED_DIRECTORIES and not (current / name).is_symlink()
            )
            for file_name in sorted(file_names):
                file_path = current / file_name
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                relative = file_path.relative_to(root)
                if _is_secret_path(relative) or _is_probably_binary(file_path):
                    continue
                candidates.append(relative)

        return candidates

    def _collect_file_contents(
        self, root: Path, relevant: list[Path]
    ) -> tuple[list[SnapshotFile], list[str], list[str], int]:
        selected = _select_content_candidates(relevant, self._limits)
        files: list[SnapshotFile] = []
        total_characters = 0
        for relative in selected:
            remaining = self._limits.max_total_characters - total_characters
            if remaining <= 0:
                break
            result = _read_bounded_text(
                root,
                relative,
                max_characters=min(self._limits.max_characters_per_file, remaining),
            )
            if result is None:
                continue
            content, truncated = result
            files.append(
                SnapshotFile(path=relative.as_posix(), content=content, truncated=truncated)
            )
            total_characters += len(content)

        included_paths = {file.path for file in files}
        omitted_paths = [
            path.as_posix()
            for path in relevant
            if path.as_posix() not in included_paths
        ]
        truncated_paths = [file.path for file in files if file.truncated]
        return files, omitted_paths, truncated_paths, total_characters


def _path_priority(path: Path) -> tuple[int, int, str]:
    name = path.name.lower()
    if name in IMPORTANT_FILE_NAMES:
        priority = 0
    elif name in LOCKFILE_NAMES:
        priority = 1
    elif _is_source_file(path):
        priority = 2 + _source_priority(path)[0]
    else:
        priority = 10
    return priority, len(path.parts), path.as_posix().lower()


def _select_content_candidates(
    relevant: list[Path], limits: SnapshotLimits
) -> list[Path]:
    configuration_files = [
        path
        for path in relevant
        if path.name.lower() in IMPORTANT_FILE_NAMES
        and path.name.lower() not in LOCKFILE_NAMES
    ]
    source_files = sorted(
        (path for path in relevant if _is_source_file(path)),
        key=_source_priority,
    )[: limits.max_source_content_files]
    selected = list(dict.fromkeys([*configuration_files, *source_files]))
    return selected[: limits.max_content_files]


def _is_source_file(path: Path) -> bool:
    return path.suffix.lower() in SOURCE_FILE_SUFFIXES


def _source_priority(path: Path) -> tuple[int, int, str]:
    name = path.name.lower()
    part_names = {part.lower() for part in path.parts[:-1]}
    role_tokens: set[str] = set()
    for part in (*path.parts[:-1], path.stem):
        normalized = part.lower().replace("-", "_")
        role_tokens.update(token for token in normalized.split("_") if token)

    if part_names & TEST_DIRECTORY_NAMES:
        priority = 5
    elif name in ENTRYPOINT_FILE_NAMES:
        priority = 0
    elif name != "__init__.py" and role_tokens & ARCHITECTURE_ROLE_NAMES:
        priority = 1
    elif name != "__init__.py" and part_names & SOURCE_ROOT_NAMES:
        priority = 2
    elif name == "__init__.py":
        priority = 4
    else:
        priority = 3
    return priority, len(path.parts), path.as_posix().lower()


def _is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or name.startswith(".env.") or path.suffix.lower() in SECRET_SUFFIXES:
        return True
    normalized = name.replace("-", "_").replace(".", "_")
    tokens = {token for token in normalized.split("_") if token}
    return bool(tokens & SECRET_NAME_TOKENS) or (
        {"private", "key"}.issubset(tokens)
        or {"api", "key"}.issubset(tokens)
    )


def _is_probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            sample = file.read(8_192)
    except OSError:
        return True
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _read_bounded_text(
    root: Path, relative: Path, *, max_characters: int
) -> tuple[str, bool] | None:
    path = root / relative
    if path.is_symlink() or _is_secret_path(relative):
        return None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    maximum_bytes = max_characters * 4 + 4
    try:
        with resolved.open("rb") as file:
            raw = file.read(maximum_bytes + 1)
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    truncated = len(text) > max_characters or len(raw) > maximum_bytes
    return text[:max_characters], truncated


def _read_small_text(root: Path, name: str) -> str | None:
    path = root / name
    if path.is_symlink() or not path.is_file() or _is_secret_path(Path(name)):
        return None
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_CONFIG_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_CONFIG_FILE_BYTES or b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _load_json_object(root: Path, name: str) -> dict[str, Any]:
    text = _read_small_text(root, name)
    if text is None:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _load_toml_object(root: Path, name: str) -> dict[str, Any]:
    text = _read_small_text(root, name)
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _load_yaml_object(root: Path, name: str) -> dict[str, Any]:
    text = _read_small_text(root, name)
    if text is None:
        return {}
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _detect_technologies(
    root: Path,
) -> tuple[list[str], dict[str, Any], dict[str, Any], dict[str, Any]]:
    package_data = _load_json_object(root, "package.json")
    pyproject_data = _load_toml_object(root, "pyproject.toml")
    pubspec_data = _load_yaml_object(root, "pubspec.yaml")
    technologies: list[str] = []
    if any((root / marker).is_file() for marker in (
        "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"
    )):
        technologies.append("Python")
    python_dependencies = _python_dependency_names(root, pyproject_data)
    for dependency, technology in (
        ("fastapi", "FastAPI"),
        ("django", "Django"),
        ("flask", "Flask"),
    ):
        if dependency in python_dependencies:
            technologies.append(technology)
    if package_data or (root / "package.json").is_file():
        technologies.append("JavaScript")
    node_dependencies = _node_dependency_names(package_data)
    if (root / "tsconfig.json").is_file() or "typescript" in node_dependencies:
        technologies.append("TypeScript")
    if "next" in node_dependencies:
        technologies.append("Next.js")
    if "react" in node_dependencies:
        technologies.append("React")
    if (root / "pubspec.yaml").is_file():
        technologies.append("Dart")
        dependencies = pubspec_data.get("dependencies", {})
        environment = pubspec_data.get("environment", {})
        if (
            isinstance(dependencies, dict) and "flutter" in dependencies
            or isinstance(environment, dict) and "flutter" in environment
        ):
            technologies.append("Flutter")
    if (root / "pom.xml").is_file():
        technologies.extend(["Java", "Maven"])
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        if "Java" not in technologies:
            technologies.append("Java")
        technologies.append("Gradle")
    if (root / "go.mod").is_file():
        technologies.append("Go")
    if (root / "Cargo.toml").is_file():
        technologies.append("Rust")
    return technologies, package_data, pyproject_data, pubspec_data


def _python_dependency_names(root: Path, pyproject_data: dict[str, Any]) -> set[str]:
    dependencies: set[str] = set()
    project = pyproject_data.get("project", {})
    if isinstance(project, dict):
        groups: list[object] = [project.get("dependencies", [])]
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            groups.extend(optional.values())
        for values in groups:
            if isinstance(values, list):
                dependencies.update(_normalize_python_dependency(item) for item in values)
    requirements = _read_small_text(root, "requirements.txt")
    if requirements:
        dependencies.update(
            _normalize_python_dependency(line) for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return {dependency for dependency in dependencies if dependency}


def _normalize_python_dependency(value: object) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip().lower()
    for separator in ("[", "<", ">", "=", "!", "~", ";", " "):
        name = name.split(separator, 1)[0]
    return name.replace("_", "-")


def _node_dependency_names(package_data: dict[str, Any]) -> set[str]:
    dependencies: set[str] = set()
    for group_name in ("dependencies", "devDependencies", "peerDependencies"):
        group = package_data.get(group_name, {})
        if isinstance(group, dict):
            dependencies.update(str(name).lower() for name in group)
    return dependencies


def _infer_commands(
    root: Path,
    *,
    package_data: dict[str, Any],
    pyproject_data: dict[str, Any],
    pubspec_data: dict[str, Any],
    technologies: list[str],
) -> tuple[list[str], list[str]]:
    tests: list[str] = []
    builds: list[str] = []
    if "Python" in technologies:
        tool_data = pyproject_data.get("tool", {})
        if (
            "pytest" in _python_dependency_names(root, pyproject_data)
            or isinstance(tool_data, dict) and "pytest" in tool_data
            or (root / "tests").is_dir()
        ):
            tests.append("python -m pytest")
        if pyproject_data.get("build-system"):
            builds.append("python -m build")
    scripts = package_data.get("scripts", {})
    if isinstance(scripts, dict):
        package_manager = _node_package_manager(root)
        if "test" in scripts:
            tests.append(
                "yarn test"
                if package_manager == "yarn"
                else f"{package_manager} test"
            )
        if "build" in scripts:
            builds.append(
                "yarn build"
                if package_manager == "yarn"
                else f"{package_manager} run build"
            )
    if "Flutter" in technologies:
        tests.append("flutter test")
    elif "Dart" in technologies:
        dev_dependencies = pubspec_data.get("dev_dependencies", {})
        if isinstance(dev_dependencies, dict) and "test" in dev_dependencies:
            tests.append("dart test")
    if "Maven" in technologies:
        tests.append("mvn test")
        builds.append("mvn package")
    if "Gradle" in technologies:
        gradle = "./gradlew" if (root / "gradlew").is_file() else "gradle"
        tests.append(f"{gradle} test")
        builds.append(f"{gradle} build")
    if "Go" in technologies:
        tests.append("go test ./...")
        builds.append("go build ./...")
    if "Rust" in technologies:
        tests.append("cargo test")
        builds.append("cargo build")
    return list(dict.fromkeys(tests)), list(dict.fromkeys(builds))


def _node_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    return "npm"
