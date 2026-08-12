"""CLI tests for controlled implementation safety gates."""
from pathlib import Path
from typer.testing import CliRunner
from fanatic_agents.cli.main import app
from fanatic_agents.core.settings import ApplicationSettings
from fanatic_agents.implementation.models import ChangeOperation, ChangeSet, ImplementationResult, WorkspaceSummary
from test_implementation_flow import workflow

runner=CliRunner()


def repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path/"README.md").write_text("# Sample",encoding="utf-8"); return tmp_path


def test_implement_without_ai_calls_neither_agents_nor_docker(tmp_path: Path, monkeypatch) -> None:
    def forbidden(*_args,**_kwargs): raise AssertionError("must not run")
    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow",forbidden)
    monkeypatch.setattr("fanatic_agents.cli.main.run_controlled_implementation",forbidden)
    result=runner.invoke(app,["workflow","implement",str(repo(tmp_path)),"--image","python:3.12-slim"])
    assert result.exit_code == 0
    assert "AI implementation:" in result.stdout and "NOT REQUESTED" in result.stdout
    assert "Original repository:" in result.stdout and "PROTECTED" in result.stdout
    assert "TEMPORARY" in result.stdout and "not executed" in result.stdout


def test_implement_ai_without_key_stops_before_agents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("fanatic_agents.cli.main.get_settings",lambda: ApplicationSettings(_env_file=None))
    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow",lambda *_: (_ for _ in ()).throw(AssertionError("agent")))
    result=runner.invoke(app,["workflow","implement",str(repo(tmp_path)),"--image","python:3.12-slim","--ai"])
    assert result.exit_code == 1 and "requires OPENAI_API_KEY" in result.stdout and "not executed" in result.stdout


def test_stopped_workflow_never_runs_implementation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY","fake")
    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow",lambda _snapshot: workflow(status="changes_requested"))
    monkeypatch.setattr("fanatic_agents.cli.main.run_controlled_implementation",lambda *_: (_ for _ in ()).throw(AssertionError("docker")))
    result=runner.invoke(app,["workflow","implement",str(repo(tmp_path)),"--image","python:3.12-slim","--ai"])
    assert result.exit_code == 1 and "CHANGES_REQUESTED" in result.stdout


def implementation_result(status: str) -> ImplementationResult:
    cs=ChangeSet(task_title="Update source",summary="Updated",changes=[ChangeOperation(operation="modify",path="source.py",content="changed",reason="task")])
    return ImplementationResult(task="Update source",changeset=cs,status=status,workspace_summary=WorkspaceSummary(initial_file_count=1,initial_total_bytes=3,changes_applied=1,cleaned_up=True),tests_passed=status=="verified",commands_executed_count=1)  # type: ignore[arg-type]


def test_cli_renders_verified_and_verification_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY","fake")
    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow",lambda _snapshot: workflow())
    monkeypatch.setattr("fanatic_agents.cli.main.run_controlled_implementation",lambda *_: implementation_result("verified"))
    passed=runner.invoke(app,["workflow","implement",str(repo(tmp_path/"pass")),"--image","python:3.12-slim","--ai"])
    assert passed.exit_code == 0 and "VERIFIED" in passed.stdout and "Generated changes: 1" in passed.stdout
    monkeypatch.setattr("fanatic_agents.cli.main.run_controlled_implementation",lambda *_: implementation_result("verification_failed"))
    failed=runner.invoke(app,["workflow","implement",str(repo(tmp_path/"fail")),"--image","python:3.12-slim","--ai"])
    assert failed.exit_code == 1 and "VERIFICATION_FAILED" in failed.stdout