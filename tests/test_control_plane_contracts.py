"""Repo-level control plane contract tests for the Codex-native runtime."""

from __future__ import annotations

from pathlib import Path

from sibyl.orchestrate import render_control_plane_prompt


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_root_agents_file_exists():
    agents_path = REPO_ROOT / "AGENTS.md"
    assert agents_path.is_file()
    text = agents_path.read_text(encoding="utf-8")
    assert "Codex CLI" in text
    assert "sibyl start" in text
    assert "codex exec" in text


def test_workspace_runtime_assets_are_codex_native():
    runtime_assets = (REPO_ROOT / "sibyl" / "runtime_assets.py").read_text(encoding="utf-8")
    assert "AGENTS.md" in runtime_assets
    assert ".codex" in runtime_assets
    assert ".claude" not in runtime_assets


def test_loop_prompt_uses_sibyl_cli_and_codex_exec():
    loop_prompt = render_control_plane_prompt("loop", workspace_path="WORKSPACE_PATH")
    assert "sibyl next" in loop_prompt
    assert "sibyl record" in loop_prompt
    assert "sibyl resume" in loop_prompt
    assert "codex exec" in loop_prompt
    assert "run_in_background" not in loop_prompt


def test_loop_prompt_keeps_progress_tracking_and_wait_contracts():
    loop_prompt = render_control_plane_prompt("loop", workspace_path="WORKSPACE_PATH")
    assert "Progress Tracking" in loop_prompt
    assert "TaskUpdate" in loop_prompt
    assert "remaining <=30min -> 2min" in loop_prompt
    assert "30-120min -> 5min" in loop_prompt
    assert ">120min -> 10min" in loop_prompt
    assert "wake_check_interval_sec" in loop_prompt


def test_agents_file_carries_gpu_and_experiment_wait_contracts():
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "keep polling" in text
    assert "sibyl stop" in text
    assert "Codex sessions" in text


def test_codex_prompt_files_exist():
    prompt_dir = REPO_ROOT / "sibyl" / "prompts"
    required = [
        "codex_reviewer.md",
        "codex_writer.md",
        "orchestration_loop.md",
        "server_experimenter.md",
    ]
    for rel_path in required:
        assert (prompt_dir / rel_path).is_file(), rel_path
