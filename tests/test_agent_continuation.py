import json
from pathlib import Path


def test_approval_handoff_launches_detached_full_pipeline_agent(tmp_path, monkeypatch):
    from app.pipeline.agent_continuation import launch_full_pipeline_continuation

    launched = {}

    class Process:
        pid = 4242

    def popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr("shutil.which", lambda name: "/opt/codex" if name == "codex" else None)
    monkeypatch.setattr("subprocess.Popen", popen)

    receipt = launch_full_pipeline_continuation(
        "run-42", repo_root=tmp_path, log_root=tmp_path / "logs",
    )

    assert launched["command"][:5] == [
        "/opt/codex", "exec", "--approve-for-me", "-C", str(tmp_path),
    ]
    assert "$full-pipeline" in launched["command"][5]
    assert "run-42" in launched["command"][5]
    assert "already-approved" in launched["command"][5]
    assert "three parallel workers" in launched["command"][5]
    assert "disjoint immutable IDs" in launched["command"][5]
    assert "same-stage persistence/reporting single-writer" in launched["command"][5]
    assert "fixed TA master fallback" in launched["command"][5]
    assert "do not pause for wording" in launched["command"][5]
    assert launched["kwargs"]["start_new_session"] is True
    assert launched["kwargs"]["stdin"] is not None
    assert receipt["pid"] == 4242
    durable = json.loads((tmp_path / "logs" / "run-42.json").read_text())
    assert durable["run_id"] == "run-42"
    assert durable["pid"] == 4242
