from __future__ import annotations

from pathlib import Path
import csv
import shutil
import subprocess
import sys

from PIL import Image


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


def test_start_agent_ingests_user_photo_before_labeling(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "src" / "agentized_workflow", tmp_path / "src" / "agentized_workflow",
                    ignore=shutil.ignore_patterns("__pycache__"))
    tools = tmp_path / "tools"
    tools.mkdir()
    shutil.copy2(source / "tools" / "start_agent.ps1", tools / "start_agent.ps1")

    config = tmp_path / "config"
    config.mkdir()
    (config / "species.txt").write_text("石磺\n", encoding="utf-8")
    (config / "species_taxonomy.json").write_text(
        '{"石磺":{"scientific_names":["Onchidium verruculatum"]}}', encoding="utf-8"
    )
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "collection_state.json").write_text(
        '{"石磺":{"status":"completed"}}', encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=offline-test\nDASHSCOPE_BASE_URL=https://example.invalid/v1\n",
        encoding="utf-8",
    )
    original = tmp_path / "input" / "inbox" / "batch-one" / "石磺" / "field.jpg"
    original.parent.mkdir(parents=True)
    Image.new("RGB", (24, 24), (120, 80, 40)).save(original)

    def ps(value: Path) -> str:
        return str(value).replace("'", "''")

    harness = tmp_path / "run.ps1"
    harness.write_text(
        f"$realPython = '{ps(Path(sys.executable))}'\n"
        f"$projectRoot = '{ps(tmp_path)}'\n"
        "function python {\n"
        "    if ($args -contains '--live') {\n"
        "        $metadata = Join-Path $projectRoot 'photos/workflow_metadata.csv'\n"
        "        if (Test-Path -LiteralPath $metadata) {\n"
        "            Copy-Item -LiteralPath $metadata -Destination (Join-Path $projectRoot 'observed.csv')\n"
        "        }\n"
        "        Set-Content -LiteralPath (Join-Path $projectRoot 'label-called.txt') -Value 'called'\n"
        "        $global:LASTEXITCODE = 0\n"
        "        return\n"
        "    }\n"
        "    & $realPython @args\n"
        "    $global:LASTEXITCODE = $LASTEXITCODE\n"
        "}\n"
        f"& '{ps(tools / 'start_agent.ps1')}' -ProjectRoot $projectRoot -Contact 'test@example.org'\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "label-called.txt").is_file()
    observed = tmp_path / "observed.csv"
    assert observed.is_file(), "labeling began before inbox metadata was generated"
    with observed.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["species"] == "石磺"
    assert (tmp_path / "photos" / rows[0]["source_image"]).is_file()
    assert original.is_file()
