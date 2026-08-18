"""Network-free tests for the read-only GitHub observation query."""

from __future__ import annotations

import json
import subprocess

import pytest

from fanatic_agents.github.client import (
    PR_VIEW_FIELDS,
    GitHubCli,
    GitHubCommandError,
    parse_pull_request_url,
)


def test_parse_pull_request_url_binds_repository_and_number() -> None:
    assert parse_pull_request_url("https://github.com/Owner/Repo/pull/42") == (
        "Owner/Repo",
        42,
    )
    assert parse_pull_request_url("https://gitlab.com/owner/repo/pull/42") is None
    assert parse_pull_request_url("https://github.com/owner/repo/issues/42") is None


def test_view_pull_request_uses_one_structured_read_only_command(monkeypatch) -> None:
    response = {
        "number": 42,
        "url": "https://github.com/owner/repo/pull/42",
        "state": "OPEN",
    }
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    monkeypatch.setattr("fanatic_agents.github.client.subprocess.run", fake_run)
    assert GitHubCli().view_pull_request("owner/repo", 42) == response
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        "gh",
        "pr",
        "view",
        "42",
        "--repo",
        "owner/repo",
        "--json",
        PR_VIEW_FIELDS,
    ]
    assert kwargs["shell"] is False and kwargs["timeout"] == 20.0
    forbidden = {
        "edit", "comment", "review", "merge", "rerun", "workflow", "run",
        "add", "commit", "push", "pull", "rebase", "reset",
    }
    assert forbidden.isdisjoint(argv)


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, ""), (0, "not-json"), (0, "[]")],
)
def test_view_failures_are_sanitized_and_fail_closed(
    monkeypatch, returncode: int, stdout: str
) -> None:
    monkeypatch.setattr(
        "fanatic_agents.github.client.subprocess.run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, returncode, stdout, "synthetic-secret-error"
        ),
    )
    with pytest.raises(GitHubCommandError) as error:
        GitHubCli().view_pull_request("owner/repo", 42)
    assert "synthetic-secret-error" not in str(error.value)
