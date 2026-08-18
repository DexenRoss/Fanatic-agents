"""Bounded GitHub CLI boundary for delivery preflight and pull requests."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

GH_TIMEOUT_SECONDS = 20.0
MAX_COMMAND_OUTPUT = 20_000
GITHUB_HTTPS = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
GITHUB_SSH = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/)(?P<owner>[^/]+)/"
    r"(?P<repo>[^/]+?)(?:\.git)?$",
    re.IGNORECASE,
)
PR_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/"
    r"(?P<number>[1-9][0-9]*)/?$",
    re.IGNORECASE,
)
PR_VIEW_FIELDS = (
    "number,url,state,isDraft,baseRefName,headRefName,headRefOid,mergeable,"
    "reviewDecision,statusCheckRollup,reviews,mergedAt,closedAt"
)


class GitHubCommandError(RuntimeError):
    """The GitHub CLI could not complete without exposing its raw output."""


@dataclass(frozen=True, slots=True)
class GitHubPreflight:
    """Availability and authentication state for the GitHub CLI."""

    status: Literal["ok", "not_found", "not_authenticated"]
    executable: str | None = None


@dataclass(frozen=True, slots=True)
class PullRequestReference:
    """Structured identity of one pull request."""

    number: int
    url: str


class GitHubCli:
    """Run gh with explicit argv, a timeout, and no OpenAI credential."""

    def __init__(self, *, timeout_seconds: float = GH_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._timeout_seconds = timeout_seconds

    def preflight(self) -> GitHubPreflight:
        executable = shutil.which("gh")
        if executable is None:
            return GitHubPreflight("not_found")
        result = self._run("auth", "status", "--hostname", "github.com")
        if result.returncode != 0:
            return GitHubPreflight("not_authenticated", executable)
        return GitHubPreflight("ok", executable)

    def find_pull_request(
        self, repository: str, *, base: str, head: str
    ) -> PullRequestReference | None:
        result = self._run(
            "pr", "list", "--repo", repository, "--base", base, "--head", head,
            "--state", "all", "--limit", "1", "--json",
            "number,url,baseRefName,headRefName",
        )
        if result.returncode != 0:
            raise GitHubCommandError("Existing pull requests could not be inspected safely.")
        try:
            items = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise GitHubCommandError("GitHub CLI returned an invalid pull request result.") from exc
        if not isinstance(items, list) or not items:
            return None
        item = items[0]
        if (
            not isinstance(item, dict)
            or item.get("baseRefName") != base
            or item.get("headRefName") != head
        ):
            raise GitHubCommandError("GitHub CLI returned an unexpected pull request.")
        return _parse_reference(item.get("url"), item.get("number"))

    def view_pull_request(self, repository: str, number: int) -> dict[str, object]:
        """Return one structured PR response using a read-only GitHub CLI query."""
        if number <= 0:
            raise ValueError("pull request number must be greater than zero")
        result = self._run(
            "pr", "view", str(number), "--repo", repository, "--json", PR_VIEW_FIELDS
        )
        if result.returncode != 0:
            raise GitHubCommandError("The pull request could not be observed safely.")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise GitHubCommandError(
                "GitHub CLI returned an invalid pull request observation."
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubCommandError(
                "GitHub CLI returned an unexpected pull request observation."
            )
        return payload

    def create_pull_request(
        self,
        repository: str,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        result = self._run(
            "pr", "create", "--repo", repository, "--base", base, "--head", head,
            "--title", title, "--body", body,
        )
        if result.returncode != 0:
            raise GitHubCommandError("The pull request could not be created safely.")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for line in reversed(lines):
            match = PR_URL.fullmatch(line)
            if match:
                return PullRequestReference(int(match.group("number")), line.rstrip("/"))
        raise GitHubCommandError("GitHub CLI did not return a valid pull request URL.")

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("OPENAI_API_KEY", None)
        try:
            result = subprocess.run(
                ["gh", *arguments], capture_output=True, check=False, shell=False,
                text=True, timeout=self._timeout_seconds, env=environment,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise GitHubCommandError("GitHub CLI could not complete safely.") from exc
        return subprocess.CompletedProcess(
            result.args, result.returncode, result.stdout[:MAX_COMMAND_OUTPUT],
            result.stderr[:MAX_COMMAND_OUTPUT],
        )


def parse_github_repository(remote_url: str) -> str | None:
    """Return OWNER/REPO for supported GitHub HTTPS and SSH remotes."""
    match = GITHUB_HTTPS.fullmatch(remote_url.strip()) or GITHUB_SSH.fullmatch(
        remote_url.strip()
    )
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def parse_pull_request_url(url: str) -> tuple[str, int] | None:
    """Return the repository and number encoded by a canonical GitHub PR URL."""
    match = PR_URL.fullmatch(url.strip())
    if match is None:
        return None
    repository = f"{match.group('owner')}/{match.group('repo')}"
    return repository, int(match.group("number"))


def check_github_cli() -> GitHubPreflight:
    """Public read-only preflight used by doctor and delivery."""
    try:
        return GitHubCli().preflight()
    except GitHubCommandError:
        return GitHubPreflight("not_authenticated", shutil.which("gh"))


def _parse_reference(url: object, number: object) -> PullRequestReference:
    if not isinstance(url, str) or not isinstance(number, int) or isinstance(number, bool):
        raise GitHubCommandError("GitHub CLI returned an invalid pull request identity.")
    match = PR_URL.fullmatch(url.strip())
    if match is None or int(match.group("number")) != number:
        raise GitHubCommandError("GitHub CLI returned an invalid pull request identity.")
    return PullRequestReference(number, url.rstrip("/"))
