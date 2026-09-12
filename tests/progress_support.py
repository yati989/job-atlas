"""Small public-seam fixtures for tests whose subject starts at approval."""
from sqlalchemy import select

from app.decision_runs.progress import PREDECESSORS, StageName, finish_stage, start_stage
from app.models.orm import DecisionRunStage


def complete_stage_chain(session, run_id: str, stage: str | StageName) -> None:
    """Complete a stage and all canonical ancestors through the public seam."""
    stage_name = StageName(stage)
    for predecessor in PREDECESSORS.get(stage_name, ()):
        row = session.execute(select(DecisionRunStage).where(
            DecisionRunStage.run_id == run_id,
            DecisionRunStage.name == predecessor.value,
        )).scalar_one_or_none()
        if row is None:
            complete_stage_chain(session, run_id, predecessor)
    start_stage(
        session, run_id, stage_name, expected_count=0,
        reason=f"fixture completed {stage_name.value}",
    )
    finish_stage(session, run_id, stage_name)


def complete_screening(session, run_id: str) -> None:
    complete_stage_chain(session, run_id, StageName.SCREENING_RANKING_GROUPING)
