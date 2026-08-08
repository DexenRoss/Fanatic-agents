# AGENTS.md — Fanatic Agents

## Project purpose

Fanatic Agents is a Python platform for orchestrating AI agents that manage
software engineering projects.

The system is designed around:
- explicit permissions
- bounded autonomy
- human approval for risky operations
- testable agent behavior
- safe repository interaction

## Development environment

- Python >= 3.12
- Package layout: `src/`
- Install development dependencies with:

  pip install -e ".[dev]"

## Required validation

Before completing any implementation task, run:

  python -m pytest

All tests must pass.

When CLI behavior changes, also validate relevant commands manually.

## Architecture

Keep responsibilities separated.

Current main packages:

- `core/` — domain models and configuration
- `agents/` — AI agent implementations
- `git/` — local Git operations
- `github/` — GitHub integration
- `sandbox/` — isolated execution
- `orchestrator/` — agent workflow coordination
- `cli/` — command-line interface

Do not place unrelated responsibilities in the same module.

Prefer simple abstractions over premature framework adoption.

## Coding standards

- Use type hints.
- Prefer small, explicit functions.
- Use Pydantic models for structured external/agent data where appropriate.
- Reject invalid state early.
- Handle errors explicitly.
- Avoid broad exception swallowing.
- Do not add dependencies unless they provide clear value.
- Add or update tests for new behavior.

## Security rules

Fanatic Agents must use deny-by-default behavior for dangerous capabilities.

Never enable automatically:

- merge to the primary branch
- production deployment
- secret modification
- destructive database operations

These require explicit human authorization.

Never print, log, commit, or expose secrets.

Never read `.env` contents for inclusion in prompts, logs, reports, or agent context.

## Agent safety

Agents must receive only the capabilities required for their task.

Prefer deterministic code over LLM calls when a result can be derived reliably
without AI.

Do not send an entire repository blindly to an LLM.

Repository context must be bounded and filtered.

Exclude generated files, dependency directories, binaries, caches, secrets,
and other irrelevant content from agent context.

## Git workflow

Do not work directly on `main`.

Sprint and feature work must happen on dedicated branches.

Unless the user explicitly requests otherwise:

- do not merge into `main`
- do not push changes
- do not create releases
- do not rewrite existing history
- do not amend existing commits

Before finishing a task, inspect:

  git status

Do not leave unintended generated artifacts tracked.

## Scope discipline

Implement only the requested sprint/task.

Do not implement future sprint features early unless they are strictly required
for the current task.

Do not introduce PostgreSQL, Redis, Celery, LangGraph, dashboards, deployment
infrastructure, or other major components unless the current sprint requires them.

## Documentation

Update README or relevant documentation when public behavior, installation,
configuration, or architecture changes materially.

## Completion report

At the end of an implementation task report:

1. files changed
2. architecture decisions
3. commands executed
4. test results
5. known limitations or technical debt
6. git status

Do not continue automatically into the next sprint.