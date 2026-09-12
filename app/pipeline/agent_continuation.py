"""Detached Codex handoff after a human approval wait completes."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def launch_full_pipeline_continuation(
    run_id: str, *, repo_root: str | Path = REPO_ROOT,
    log_root: str | Path | None = None,
) -> dict[str, object]:
    """Start a fresh agent only after approval, leaving the wait token-free."""
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("Codex CLI is unavailable; cannot resume the agent-owned pipeline stages")
    root = Path(repo_root).resolve()
    logs = Path(log_root) if log_root is not None else root / "logs" / "agent_continuations"
    logs.mkdir(parents=True, exist_ok=True)
    output_path = logs / f"{run_id}.log"
    receipt_path = logs / f"{run_id}.json"
    prompt = (
        f"$full-pipeline Resume the already-approved Decision Run {run_id} from its "
        "immutable process-approved artifact. Do not fetch, screen, rank, email another "
        "approval request, or wait for approval again. Act as the coordinator and use up to "
        "three parallel workers for the full-job-enrichment, resume-tailoring, company-Phase-A, "
        "contact-research, and draft-copy frontiers described by the skill. Give workers disjoint "
        "immutable IDs and artifact paths; keep manifest freezes and same-stage persistence/reporting "
        "single-writer in the coordinator. For every policy-valid master-resume pairing, use "
        "draft-outreach's fixed TA master fallback and do not pause for wording. Complete all "
        "remaining stages through Gmail drafts and the emailed completion summary. Never send outreach."
    )
    output = output_path.open("ab")
    try:
        process = subprocess.Popen(
            [executable, "exec", "--approve-for-me", "-C", str(root), prompt],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        output.close()
    receipt = {
        "run_id": run_id,
        "pid": process.pid,
        "launched_at": datetime.now(timezone.utc).isoformat(),
        "log_path": str(output_path),
    }
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt
