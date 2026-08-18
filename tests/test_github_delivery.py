"""Network-free tests for the bounded GitHub CLI boundary."""

from __future__ import annotations

import json
import subprocess

import pytest

from fanatic_agents.github.client import (
    GitHubCli,
    GitHubCommandError,
    check_github_cli,
    parse_github_repository,
)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/owner/repository.git", "owner/repository"),
        ("https://github.com/owner/repository", "owner/repository"),
        ("git@github.com:owner/repository.git", "owner/repository"),
        ("ssh://git@github.com/owner/repository.git", "owner/repository"),
        ("https://gitlab.com/owner/repository.git", None),
        ("file:///tmp/repository.git", None),
    ],
)
def test_parse_supported_github_remotes(remote: str, expected: str | None) -> None:
    assert parse_github_repository(remote) == expected


def test_preflight_distinguishes_missing_auth_and_success(monkeypatch) -> None:
    monkeypatch.setattr("fanatic_agents.github.client.shutil.which", lambda _: None)
    assert GitHubCli().preflight().status == "not_found"

    monkeypatch.setattr("fanatic_agents.github.client.shutil.which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        "fanatic_agents.github.client.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "not logged in"),
    )
    assert GitHubCli().preflight().status == "not_authenticated"
    assert check_github_cli().status == "not_authenticated"

    monkeypatch.setattr(
        "fanatic_agents.github.client.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "ok", ""),
    )
    assert GitHubCli().preflight().status == "ok"


def test_gh_subprocess_is_shell_free_bounded_and_excludes_openai_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-never-forward")
    monkeypatch.setattr("fanatic_agents.github.client.shutil.which", lambda _: "/usr/bin/gh")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "x" * 25_000, "")

    monkeypatch.setattr("fanatic_agents.github.client.subprocess.run", fake_run)
    assert GitHubCli().preflight().status == "ok"
    argv, kwargs = calls[0]
    assert argv == ["gh", "auth", "status", "--hostname", "github.com"]
    assert kwargs["shell"] is False and kwargs["timeout"] == 20.0
    environment = kwargs["env"]
    assert isinstance(environment, dict) and "OPENAI_API_KEY" not in environment


def test_pr_lookup_and_creation_use_exact_base_head_and_capture_url(monkeypatch) -> None:
    calls: list[list[str]] = []
    responses = [
        json.dumps(
            [
                {
                    "number": 7,
                    "url": "https://github.com/owner/repo/pull/7",
                    "baseRefName": "feature/base",
                    "headRefName": "fanatic/task",
                }
            ]
        ),
        "https://github.com/owner/repo/pull/8\n",
    ]

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, responses.pop(0), "")

    monkeypatch.setattr("fanatic_agents.github.client.subprocess.run", fake_run)
    client = GitHubCli()
    existing = client.find_pull_request(
        "owner/repo", base="feature/base", head="fanatic/task"
    )
    created = client.create_pull_request(
        "owner/repo",
        base="feature/base",
        head="fanatic/task",
        title="fanatic: task",
        body="safe body",
    )
    assert existing and existing.number == 7
    assert created.number == 8
    assert "feature/base" in calls[0] and "fanatic/task" in calls[0]
    assert "feature/base" in calls[1] and "fanatic/task" in calls[1]
    assert not any("merge" in argument for call in calls for argument in call)


def test_invalid_or_failed_pr_results_are_sanitized(monkeypatch) -> None:
    monkeypatch.setattr(
        "fanatic_agents.github.client.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "not a url", ""),
    )
    with pytest.raises(GitHubCommandError, match="valid pull request URL"):
        GitHubCli().create_pull_request(
            "owner/repo", base="base", head="fanatic/task", title="title", body="body"
        )

    monkeypatch.setattr(
        "fanatic_agents.github.client.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "secret raw error"),
    )
    with pytest.raises(GitHubCommandError) as error:
        GitHubCli().find_pull_request("owner/repo", base="base", head="fanatic/task")
    assert "secret raw error" not in str(error.value)
