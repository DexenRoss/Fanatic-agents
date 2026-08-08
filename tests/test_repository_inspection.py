"""Tests for deterministic, bounded repository inspection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from fanatic_agents.git.inspection import (
    RepositoryInspectionError,
    RepositoryInspector,
    SnapshotLimits,
)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
        timeout=5,
    )


def _initialize_committed_repository(repository: Path) -> None:
    _git(repository, "init", "-b", "test-branch")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Fanatic Tests")
    (repository / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")


def test_inspects_python_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires = [\"setuptools\"]\n"
        "[project]\n"
        "name = \"sample\"\n"
        "dependencies = [\"fastapi\", \"pytest\"]\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Sample", encoding="utf-8")

    snapshot = RepositoryInspector().inspect(tmp_path)

    assert snapshot.detected_technologies == ["Python", "FastAPI"]
    assert "python -m pytest" in snapshot.inferred_test_commands
    assert "python -m build" in snapshot.inferred_build_commands
    assert "pyproject.toml" in snapshot.important_files


def test_detects_javascript_typescript_next_and_react(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"next": "latest", "react": "latest"},
                "devDependencies": {"typescript": "latest"},
                "scripts": {"test": "vitest", "build": "next build"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")

    snapshot = RepositoryInspector().inspect(tmp_path)

    assert snapshot.detected_technologies == [
        "JavaScript",
        "TypeScript",
        "Next.js",
        "React",
    ]
    assert snapshot.inferred_test_commands == ["npm test"]
    assert snapshot.inferred_build_commands == ["npm run build"]


def test_detects_flutter(tmp_path: Path) -> None:
    (tmp_path / "pubspec.yaml").write_text(
        "name: sample\ndependencies:\n  flutter:\n    sdk: flutter\n",
        encoding="utf-8",
    )

    snapshot = RepositoryInspector().inspect(tmp_path)

    assert snapshot.detected_technologies == ["Dart", "Flutter"]
    assert snapshot.inferred_test_commands == ["flutter test"]


@pytest.mark.parametrize(
    ("marker", "tool", "test_command", "build_command"),
    [
        ("pom.xml", "Maven", "mvn test", "mvn package"),
        ("build.gradle.kts", "Gradle", "gradle test", "gradle build"),
    ],
)
def test_detects_java_build_tools(
    tmp_path: Path,
    marker: str,
    tool: str,
    test_command: str,
    build_command: str,
) -> None:
    (tmp_path / marker).write_text("", encoding="utf-8")

    snapshot = RepositoryInspector().inspect(tmp_path)

    assert snapshot.detected_technologies == ["Java", tool]
    assert test_command in snapshot.inferred_test_commands
    assert build_command in snapshot.inferred_build_commands


def test_directory_without_git_is_inspectable(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.invalid/sample", encoding="utf-8")

    snapshot = RepositoryInspector().inspect(tmp_path)

    assert snapshot.is_git_repository is False
    assert snapshot.current_branch is None
    assert snapshot.working_tree_clean is None
    assert snapshot.detected_technologies == ["Go"]


def test_git_repository_reports_clean_and_dirty_states(tmp_path: Path) -> None:
    _initialize_committed_repository(tmp_path)

    clean = RepositoryInspector().inspect(tmp_path)
    assert clean.is_git_repository is True
    assert clean.current_branch == "test-branch"
    assert clean.working_tree_clean is True

    (tmp_path / "README.md").write_text("changed", encoding="utf-8")
    dirty = RepositoryInspector().inspect(tmp_path)

    assert dirty.working_tree_clean is False


def test_excludes_secrets_dependencies_and_binary_files(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=never-read", encoding="utf-8")
    (tmp_path / ".env.local").write_text("SECRET=never-read", encoding="utf-8")
    (tmp_path / "service-credentials.json").write_text("secret", encoding="utf-8")
    (tmp_path / "private.key").write_text("secret", encoding="utf-8")
    (tmp_path / "image.bin").write_bytes(b"text\x00binary")
    dependency = tmp_path / "node_modules" / "package"
    dependency.mkdir(parents=True)
    (dependency / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "README.md").write_text("safe", encoding="utf-8")

    snapshot = RepositoryInspector().inspect(tmp_path)
    serialized = snapshot.model_dump_json()

    assert snapshot.relevant_paths == ["README.md"]
    assert "never-read" not in serialized
    assert "node_modules" not in serialized
    assert "image.bin" not in serialized


def test_per_file_character_limit_is_applied(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x" * 100, encoding="utf-8")
    limits = SnapshotLimits(max_characters_per_file=10, max_total_characters=100)

    snapshot = RepositoryInspector(limits).inspect(tmp_path)

    assert snapshot.files[0].content == "x" * 10
    assert snapshot.files[0].truncated is True
    assert snapshot.truncation.truncated_files == 1
    assert snapshot.truncation.content_included_paths == ["README.md"]
    assert snapshot.truncation.content_truncated_paths == ["README.md"]


def test_total_character_limit_is_applied(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("r" * 100, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("p" * 100, encoding="utf-8")
    limits = SnapshotLimits(max_characters_per_file=100, max_total_characters=15)

    snapshot = RepositoryInspector(limits).inspect(tmp_path)

    assert sum(len(file.content) for file in snapshot.files) == 15
    assert snapshot.truncation.total_characters == 15
    assert snapshot.truncation.content_files_omitted == 1
    assert len(snapshot.truncation.content_included_paths) == 1
    assert len(snapshot.truncation.content_omitted_paths) == 1


def test_relevant_path_limit_is_applied(tmp_path: Path) -> None:
    for index in range(10):
        (tmp_path / f"file-{index}.txt").write_text("safe", encoding="utf-8")

    snapshot = RepositoryInspector(SnapshotLimits(max_relevant_files=3)).inspect(tmp_path)

    assert len(snapshot.relevant_paths) == 3
    assert snapshot.truncation.relevant_files_omitted == 7


def test_does_not_follow_symlinks_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-appear", encoding="utf-8")
    (repository / "README.md").symlink_to(outside)
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    (outside_directory / "pyproject.toml").write_text("must-not-appear", encoding="utf-8")
    (repository / "linked-directory").symlink_to(outside_directory, target_is_directory=True)

    snapshot = RepositoryInspector().inspect(repository)

    assert snapshot.relevant_paths == []
    assert "must-not-appear" not in snapshot.model_dump_json()


def test_lockfile_path_is_visible_but_content_is_not_sent(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("large-lock-content", encoding="utf-8")

    snapshot = RepositoryInspector().inspect(tmp_path)

    assert "package-lock.json" in snapshot.important_files
    assert "package-lock.json" not in [file.path for file in snapshot.files]
    assert "large-lock-content" not in snapshot.model_dump_json()


def test_invalid_paths_raise_clear_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(RepositoryInspectionError, match="does not exist"):
        RepositoryInspector().inspect(missing)

    file_path = tmp_path / "file.txt"
    file_path.write_text("text", encoding="utf-8")
    with pytest.raises(RepositoryInspectionError, match="not a directory"):
        RepositoryInspector().inspect(file_path)


def test_git_detached_head_is_reported(tmp_path: Path) -> None:
    _initialize_committed_repository(tmp_path)
    _git(tmp_path, "checkout", "--detach")

    snapshot = RepositoryInspector().inspect(tmp_path)

    assert snapshot.is_git_repository is True
    assert snapshot.current_branch is None
    assert snapshot.detached_head is True

def test_includes_representative_python_source_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\n", encoding="utf-8"
    )
    source_files = {
        "src/sample/cli/main.py": "def main(): pass\n",
        "src/sample/agents/worker.py": "class Worker: pass\n",
        "src/sample/core/models.py": "class Project: pass\n",
        "src/sample/repository/inspection.py": "def inspect(): pass\n",
        "src/sample/utilities.py": "def helper(): pass\n",
    }
    for relative_path, content in source_files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    snapshot = RepositoryInspector().inspect(tmp_path)
    included = set(snapshot.truncation.content_included_paths)

    assert "src/sample/cli/main.py" in included
    assert "src/sample/agents/worker.py" in included
    assert "src/sample/core/models.py" in included
    assert "src/sample/repository/inspection.py" in included
    assert included == {file.path for file in snapshot.files}


def test_source_content_selection_has_an_independent_small_limit(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'bounded'\n", encoding="utf-8"
    )
    for index in range(12):
        path = tmp_path / "src" / "sample" / "core" / f"component_{index:02}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")
    limits = SnapshotLimits(
        max_relevant_files=50,
        max_content_files=10,
        max_source_content_files=3,
        max_characters_per_file=1_000,
        max_total_characters=10_000,
    )

    snapshot = RepositoryInspector(limits).inspect(tmp_path)
    included_sources = [
        path
        for path in snapshot.truncation.content_included_paths
        if path.endswith(".py")
    ]

    assert len(included_sources) == 3
    assert len(snapshot.files) == 4
    assert len(snapshot.files) <= limits.max_content_files
    assert any(
        path.endswith("component_11.py")
        for path in snapshot.truncation.content_omitted_paths
    )


def test_generated_and_dependency_directories_remain_excluded(tmp_path: Path) -> None:
    for directory in ("node_modules", "build", "dist", "target", ".next"):
        path = tmp_path / directory / "nested" / "main.py"
        path.parent.mkdir(parents=True)
        path.write_text("must_not_appear = True\n", encoding="utf-8")
    safe_source = tmp_path / "src" / "sample" / "main.py"
    safe_source.parent.mkdir(parents=True)
    safe_source.write_text("safe = True\n", encoding="utf-8")

    snapshot = RepositoryInspector().inspect(tmp_path)
    serialized = snapshot.model_dump_json()

    assert "src/sample/main.py" in snapshot.truncation.content_included_paths
    assert "must_not_appear" not in serialized
    for directory in ("node_modules", "build", "dist", "target", ".next"):
        prefix = f"{directory}/"
        assert all(not path.startswith(prefix) for path in snapshot.relevant_paths)
        assert all(
            not path.startswith(prefix)
            for path in snapshot.truncation.content_included_paths
        )
