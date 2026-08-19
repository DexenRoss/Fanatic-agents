"""Network-free tests for the read-only GitHub Issue transport."""

from __future__ import annotations

import json
import subprocess

import pytest

from fanatic_agents.github.client import GitHubCli, GitHubCommandError


def test_issue_list_uses_only_structured_bounded_read_only_argv(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    response = json.dumps(
        [
            {
                "number": 1,
                "title": "Task",
                "body": "body",
                "url": "https://github.com/owner/repo/issues/1",
                "state": "OPEN",
                "labels": [],
                "assignees": [],
                "author": None,
                "createdAt": "2025-01-01T00:00:00Z",
                "updatedAt": "2025-01-01T00:00:00Z",
                "milestone": None,
            }
        ]
    )

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, response, "")

    monkeypatch.setattr("fanatic_agents.github.client.subprocess.run", fake_run)
    result = GitHubCli().list_open_issues("owner/repo", limit=50)

    assert result[0]["number"] == 1
    argv, kwargs = calls[0]
    assert argv[:3] == ["gh", "issue", "list"]
    assert "--state" in argv and argv[argv.index("--state") + 1] == "open"
    assert "--limit" in argv and argv[argv.index("--limit") + 1] == "50"
    assert "--json" in argv
    assert kwargs["shell"] is False
    forbidden = {"edit", "close", "comment", "develop", "pin", "pr", "merge"}
    assert forbidden.isdisjoint(argv)


def test_issue_transport_output_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        "fanatic_agents.github.client.subprocess.run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, "x" * 100, "y" * 100
        ),
    )

    result = GitHubCli()._run("version", output_limit=12)

    assert result.stdout == "x" * 12 and result.stderr == "y" * 12


@pytest.mark.parametrize("limit", [0, 101])
def test_issue_limit_is_rejected_before_subprocess(limit: int) -> None:
    with pytest.raises(ValueError):
        GitHubCli().list_open_issues("owner/repo", limit=limit)


@pytest.mark.parametrize(
    "stdout",
    ["not-json", "{}", "[1]"],
)
def test_invalid_issue_json_fails_with_sanitized_error(
    monkeypatch, stdout: str
) -> None:
    monkeypatch.setattr(
        "fanatic_agents.github.client.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout, "raw-secret-error"
        ),
    )

    with pytest.raises(GitHubCommandError) as error:
        GitHubCli().list_open_issues("owner/repo", limit=1)

    assert "raw-secret-error" not in str(error.value)


def test_network_error_and_timeout_are_sanitized(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["gh"], 20)

    monkeypatch.setattr("fanatic_agents.github.client.subprocess.run", timeout)
    with pytest.raises(GitHubCommandError, match="complete safely"):
        GitHubCli().list_open_issues("owner/repo", limit=1)

    monkeypatch.setattr(
        "fanatic_agents.github.client.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, "", "network included a token"
        ),
    )
    with pytest.raises(GitHubCommandError) as error:
        GitHubCli().list_open_issues("owner/repo", limit=1)
    assert "token" not in str(error.value)
