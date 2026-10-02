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


def test_start_agent_skips_a_valid_existing_label_without_new_run(tmp_path: Path) -> None:
    from agentized_workflow.label_registry import LabelRegistry, profile_for
    from agentized_workflow.metadata import load_tasks
    from agentized_workflow.photo_library import PhotoLibrary
    from agentized_workflow.storage import Store
    from agentized_workflow.workflow import Engine

    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "src" / "agentized_workflow", tmp_path / "src" / "agentized_workflow",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "tools").mkdir()
    shutil.copy2(source / "tools" / "start_agent.ps1", tmp_path / "tools" / "start_agent.ps1")
    config = tmp_path / "config"
    config.mkdir()
    (config / "species.txt").write_text("石磺\n", encoding="utf-8")
    (config / "species_taxonomy.json").write_text(
        '{"石磺":{"scientific_names":["Onchidium verruculatum"]}}', encoding="utf-8")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "metadata.csv").write_text("species,status\n石磺,active\n", encoding="utf-8")
    (catalog / "collection_state.json").write_text('{"石磺":{"status":"completed"}}', encoding="utf-8")
    (tmp_path / ".env").write_text(
        "# 配置服务地址\nDASHSCOPE_API_KEY=offline-test\n# 百炼模型地址\nDASHSCOPE_BASE_URL=https://example.invalid/v1\n", encoding="utf-8")
    batch = tmp_path / "batch" / "石磺"
    batch.mkdir(parents=True)
    Image.new("RGB", (64, 48), "white").save(batch / "one.jpg")
    photos = tmp_path / "photos"
    library = PhotoLibrary(catalog, photos)
    assert library.ingest_batch(batch.parent).accepted == 1
    spec = load_tasks(photos, library.write_workflow_metadata())[0]

    class Vision:
        def visibility(self, request):
            return {"route": "whole_or_mostly_visible"}

        def localize(self, request):
            return {"boxes": [{"bbox": [100, 100, 800, 800]}]}

    store = Store(tmp_path / "runs" / "earlier")
    engine = Engine(store, Vision())
    engine.add(spec)
    state = engine.run(spec.task_id)
    profile = profile_for("qwen3-vl-plus", "https://example.invalid/v1", .7, 10, False, None)
    LabelRegistry(photos).record(state, store.root, profile)
    harness = tmp_path / "run.ps1"
    harness.write_text(
        f"$realPython = '{str(Path(sys.executable)).replace("'", "''")}'\n"
        "function python {\n"
        "    if ($args -contains '--live') {\n"
        "        Set-Content -LiteralPath 'label-called.txt' -Value 'called'\n"
        "        $global:LASTEXITCODE = 0\n"
        "        return\n"
        "    }\n"
        "    & $realPython @args\n"
        "    $global:LASTEXITCODE = $LASTEXITCODE\n"
        "}\n"
        f"& '{str(tmp_path / 'tools' / 'start_agent.ps1').replace("'", "''")}' "
        f"-ProjectRoot '{str(tmp_path).replace("'", "''")}' -Contact 'test@example.org'\n",
        encoding="utf-8",
    )
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "label-called.txt").exists()
    assert not list((tmp_path / "runs").glob("agent-*"))
    assert (store.root / "annotated" / f"{spec.task_id}.jpg").is_file()


def test_import_labels_script_uses_configured_endpoint_without_model_calls(tmp_path: Path) -> None:
    from agentized_workflow.label_registry import LabelRegistry, profile_for
    from agentized_workflow.metadata import load_tasks
    from agentized_workflow.photo_library import PhotoLibrary
    from agentized_workflow.storage import Store
    from agentized_workflow.workflow import Engine

    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "src" / "agentized_workflow", tmp_path / "src" / "agentized_workflow",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "tools").mkdir()
    shutil.copy2(source / "tools" / "import_labels.ps1", tmp_path / "tools" / "import_labels.ps1")
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=offline-test\nDASHSCOPE_BASE_URL=https://example.invalid/v1\n", encoding="utf-8")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "metadata.csv").write_text("species,status\n石磺,active\n", encoding="utf-8")
    batch = tmp_path / "batch" / "石磺"
    batch.mkdir(parents=True)
    Image.new("RGB", (64, 48), "white").save(batch / "one.jpg")
    photos = tmp_path / "photos"
    library = PhotoLibrary(catalog, photos)
    assert library.ingest_batch(batch.parent).accepted == 1
    spec = load_tasks(photos, library.write_workflow_metadata())[0]

    class Vision:
        def visibility(self, request):
            return {"route": "whole_or_mostly_visible"}

        def localize(self, request):
            return {"boxes": [{"bbox": [100, 100, 800, 800]}]}

    store = Store(tmp_path / "runs" / "agent-historical")
    engine = Engine(store, Vision())
    engine.add(spec)
    assert engine.run(spec.task_id).phase == "done"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(tmp_path / "tools" / "import_labels.ps1"), "-ProjectRoot", str(tmp_path),
         "-RunDir", str(store.root)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    profile = profile_for("qwen3-vl-plus", "https://example.invalid/v1", .7, 10, False, None)
    assert LabelRegistry(photos).lookup(spec, profile).action == "reuse"
    assert (store.root / "annotated" / f"{spec.task_id}.jpg").is_file()
    assert list((tmp_path / "runs" / "backups").glob("photo_library-*.sqlite3"))


def test_start_agent_rejects_a_second_concurrent_launcher(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    source = Path(__file__).resolve().parents[1]
    shutil.copy2(source / "tools" / "start_agent.ps1", tmp_path / "tools" / "start_agent.ps1")
    (tmp_path / "runs").mkdir()
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=offline-test\nDASHSCOPE_BASE_URL=https://example.invalid/v1\n", encoding="utf-8")
    script = tmp_path / "hold-lock.ps1"
    script.write_text(
        f"$lock = [System.IO.File]::Open('{str(tmp_path / 'runs' / 'label-launch.lock').replace("'", "''")}', "
        "[System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)\n"
        "try {\n"
        f"  & '{str(tmp_path / 'tools' / 'start_agent.ps1').replace("'", "''")}' "
        f"-ProjectRoot '{str(tmp_path).replace("'", "''")}' -Contact 'test@example.org'\n"
        "} finally { $lock.Dispose() }\n",
        encoding="utf-8",
    )
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode != 0
    assert "another labeling launcher" in (result.stdout + result.stderr)


def test_resume_agent_respects_the_launcher_lock(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "runs" / "agent-old").mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    shutil.copy2(source / "tools" / "resume_agent.ps1", tmp_path / "tools" / "resume_agent.ps1")
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=offline-test\nDASHSCOPE_BASE_URL=https://example.invalid/v1\n", encoding="utf-8")
    script = tmp_path / "hold-lock.ps1"
    script.write_text(
        f"$lock = [System.IO.File]::Open('{str(tmp_path / 'runs' / 'label-launch.lock').replace("'", "''")}', "
        "[System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)\n"
        "try {\n"
        f"  & '{str(tmp_path / 'tools' / 'resume_agent.ps1').replace("'", "''")}' "
        f"-ProjectRoot '{str(tmp_path).replace("'", "''")}' "
        f"-RunDir '{str(tmp_path / 'runs' / 'agent-old').replace("'", "''")}'\n"
        "} finally { $lock.Dispose() }\n",
        encoding="utf-8",
    )
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode != 0
    assert "another labeling launcher" in (result.stdout + result.stderr)


def test_import_script_rejects_a_sibling_of_runs_directory(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    outside = tmp_path / "runs-other" / "agent-old"
    outside.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    shutil.copy2(source / "tools" / "import_labels.ps1", tmp_path / "tools" / "import_labels.ps1")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(tmp_path / "tools" / "import_labels.ps1"), "-ProjectRoot", str(tmp_path),
         "-RunDir", str(outside)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode != 0
    assert "RunDir must be under" in (result.stdout + result.stderr)
def test_launchers_read_env_with_explicit_utf8():
    root = Path(__file__).resolve().parents[1]
    for name in ("start_agent.ps1", "resume_agent.ps1", "import_labels.ps1"):
        script = (root / "tools" / name).read_text(encoding="utf-8")
        assert "Get-Content -LiteralPath $envFile -Encoding UTF8" in script
