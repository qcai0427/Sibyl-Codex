"""Checkpoint lifecycle helpers for orchestration stages."""

from __future__ import annotations

from typing import Any

from .constants import CHECKPOINT_DIRS
from .constants import PAPER_SECTIONS


_IDEA_ROLES = (
    "innovator",
    "pragmatist",
    "theoretical",
    "contrarian",
    "interdisciplinary",
    "empiricist",
)
_RESULT_ROLES = (
    "optimist",
    "skeptic",
    "strategist",
    "methodologist",
    "comparativist",
    "revisionist",
)


def checkpoint_steps_for_stage(stage: str) -> dict[str, str]:
    """Return the canonical checkpoint-tracked files for a stage."""
    if stage == "idea_debate":
        return {role: f"idea/perspectives/{role}.md" for role in _IDEA_ROLES}
    if stage == "result_debate":
        return {role: f"idea/result_debate/{role}.md" for role in _RESULT_ROLES}
    if stage == "writing_sections":
        return {sid: f"writing/sections/{sid}.md" for sid, _ in PAPER_SECTIONS}
    if stage == "writing_critique":
        return {sid: f"writing/critique/{sid}_critique.md" for sid, _ in PAPER_SECTIONS}
    return {}


def get_or_create_checkpoint(
    orchestrator: Any,
    stage: str,
    steps: dict[str, str],
) -> dict | None:
    """Get a validated checkpoint or create a fresh one for a stage."""
    cp_dir = CHECKPOINT_DIRS.get(stage)
    if cp_dir is None:
        return None

    iteration = orchestrator.ws.get_status().iteration
    valid = orchestrator.ws.validate_checkpoint(cp_dir, current_iteration=iteration)
    if valid is not None:
        return {
            "resuming": True,
            "completed_steps": valid["completed"],
            "remaining_steps": valid["remaining"],
            "all_complete": not valid["remaining"],
            "checkpoint_dir": cp_dir,
        }

    orchestrator.ws.create_checkpoint(
        stage,
        cp_dir,
        steps,
        iteration=iteration,
        stage_started_at=orchestrator.ws.get_status().stage_started_at,
    )
    return {
        "resuming": False,
        "completed_steps": [],
        "remaining_steps": list(steps.keys()),
        "all_complete": False,
        "checkpoint_dir": cp_dir,
    }


def reconcile_stage_checkpoint(orchestrator: Any, stage: str) -> dict | None:
    """Best-effort backfill of checkpoint metadata from existing artifacts.

    Codex child runs can finish a team stage without explicitly calling
    `sibyl checkpoint` for every teammate. Reconcile on `record` so stage-level
    progress metadata stays consistent with the files already written.
    """
    cp_dir = CHECKPOINT_DIRS.get(stage)
    if cp_dir is None:
        return None

    steps = checkpoint_steps_for_stage(stage)
    if not steps:
        return None

    status = orchestrator.ws.get_status()
    current_iteration = status.iteration
    current_cp = orchestrator.ws.load_checkpoint(cp_dir)
    if current_cp is None or current_cp.get("iteration") != current_iteration:
        orchestrator.ws.create_checkpoint(
            stage,
            cp_dir,
            steps,
            iteration=current_iteration,
            stage_started_at=status.stage_started_at,
        )

    if stage == "writing_sections":
        from .writing_artifacts import extract_section_figure_artifacts

        for step_id in steps:
            section_md = orchestrator.ws.read_file(f"writing/sections/{step_id}.md") or ""
            artifacts, has_figures_block = extract_section_figure_artifacts(section_md)
            if not has_figures_block:
                continue
            orchestrator.ws.complete_checkpoint_step(
                cp_dir,
                step_id,
                artifacts=artifacts,
                require_artifacts_metadata=True,
            )
    else:
        for step_id in steps:
            orchestrator.ws.complete_checkpoint_step(cp_dir, step_id)

    valid = orchestrator.ws.validate_checkpoint(cp_dir, current_iteration=current_iteration)
    if valid is None:
        return None
    return {
        "resuming": True,
        "completed_steps": valid["completed"],
        "remaining_steps": valid["remaining"],
        "all_complete": not valid["remaining"],
        "checkpoint_dir": cp_dir,
    }
