from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


def test_start_agent_resolves_its_root_when_project_root_is_omitted(tmp_path: Path) -> None:
    """The launcher's default root must be resolved after PowerShell binds parameters."""
    project = Path(__file__).resolve().parents[1]
    launcher = tmp_path / "tools" / "start_agent.ps1"
    launcher.parent.mkdir()
    shutil.copy2(project / "tools" / "start_agent.ps1", launcher)

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            "-Contact",
            "test@example.org",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert f"Missing .env file: {tmp_path / '.env'}" in output
    assert "Split-Path" not in output


def test_start_agent_passes_the_configured_dashscope_base_url_to_python() -> None:
    script = (Path(__file__).resolve().parents[1] / "tools" / "start_agent.ps1").read_text(encoding="utf-8")

    assert "--base-url $env:DASHSCOPE_BASE_URL" in script
    assert "DASHSCOPE_BASE_URL is not set after loading .env" in script
    assert "https://dashscope.aliyuncs.com/compatible-mode/v1" not in script


def test_resume_agent_uses_the_existing_run_directory_and_configured_base_url() -> None:
    script = (Path(__file__).resolve().parents[1] / "tools" / "resume_agent.ps1").read_text(encoding="utf-8")

    assert "[string]$RunDir" in script
    assert "--base-url $env:DASHSCOPE_BASE_URL" in script
    assert "DASHSCOPE_BASE_URL is not set after loading .env" in script
    assert "https://dashscope.aliyuncs.com/compatible-mode/v1" not in script
