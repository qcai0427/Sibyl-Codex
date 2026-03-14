"""Core CLI helpers extracted from the legacy orchestrator module."""

from __future__ import annotations

import fcntl
import datetime as dt
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sibyl._paths import get_system_state_dir
from sibyl.event_logger import EventLogger
from sibyl.workspace import Workspace, workspace_status_from_data

from .dashboard_data import collect_dashboard_data
from .config_helpers import load_effective_config
from .writing_artifacts import extract_section_figure_artifacts
from .workspace_paths import (
    resolve_active_workspace_path,
    resolve_workspace_root,
    workspace_scope_id,
)


_LOOP_ACTION_TYPES = {"experiment_wait", "gpu_poll"}
_RECOVERY_STATE_REL_PATH = ".sibyl/recovery_state.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _count_jsonl_entries(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def _sync_progress(sync_status_path: Path) -> tuple[dict[str, Any], int]:
    status = _read_json(sync_status_path)
    acknowledged = 0
    for key in ("last_synced_line", "last_attempted_line"):
        value = status.get(key, 0)
        try:
            acknowledged = max(acknowledged, int(value or 0))
        except (TypeError, ValueError):
            continue
    return status, acknowledged


def _count_pending_sync_backlog(
    pending_sync_path: Path,
    sync_status_path: Path,
) -> tuple[int, int, dict[str, Any]]:
    total_count = _count_jsonl_entries(pending_sync_path)
    status, acknowledged = _sync_progress(sync_status_path)
    backlog = max(total_count - acknowledged, 0)
    return backlog, total_count, status


def _recovery_state_path(workspace_path: str | Path) -> Path:
    workspace_root = resolve_workspace_root(workspace_path)
    return workspace_root / _RECOVERY_STATE_REL_PATH


def _load_recovery_state(workspace_path: str | Path) -> dict[str, Any]:
    return _read_json(_recovery_state_path(workspace_path))


def _persist_recovery_state(
    workspace_path: str | Path,
    payload: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    workspace_root = resolve_workspace_root(workspace_path)
    state_payload = {
        **payload,
        "source": source,
        "saved_at": time.time(),
        "path": str(_recovery_state_path(workspace_root)),
    }
    _write_json_atomic(_recovery_state_path(workspace_root), state_payload)
    return state_payload


def _sentinel_registry_path() -> Path:
    state_dir = get_system_state_dir() / "sentinel"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "session_registry.json"


@contextmanager
def _sentinel_registry_lock():
    lock_path = _sentinel_registry_path().with_suffix(".lock")
    lock_fd = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _load_sentinel_registry_unlocked() -> dict[str, dict]:
    data = _read_json(_sentinel_registry_path())
    workspaces = data.get("workspaces", data)
    if not isinstance(workspaces, dict):
        return {}
    return {
        str(workspace_root): entry
        for workspace_root, entry in workspaces.items()
        if isinstance(entry, dict)
    }


def _save_sentinel_registry_unlocked(workspaces: dict[str, dict]) -> None:
    _write_json_atomic(
        _sentinel_registry_path(),
        {"workspaces": workspaces, "updated_at": time.time()},
    )


def _load_workspace_sentinel_state(workspace_root: Path) -> dict:
    workspace_root = resolve_workspace_root(workspace_root)
    active_root = resolve_active_workspace_path(workspace_root)
    session_data = _read_json(workspace_root / "sentinel_session.json")
    heartbeat = _read_json(workspace_root / "sentinel_heartbeat.json")

    has_running = False
    exp_state_path = active_root / "exp" / "experiment_state.json"
    exp_data = _read_json(exp_state_path)
    for task in exp_data.get("tasks", {}).values():
        if isinstance(task, dict) and task.get("status") == "running":
            has_running = True
            break

    if not has_running:
        gpu_progress = _read_json(active_root / "exp" / "gpu_progress.json")
        has_running = bool(gpu_progress.get("running"))

    raw_status = _read_json(workspace_root / "status.json")
    status = workspace_status_from_data(raw_status)
    should_keep_running = (
        not status.stop_requested and (has_running or status.stage not in {"", "init", "done"})
    )
    ralph_prompt_path = str((workspace_root / ".codex" / "loop-prompt.txt").resolve())
    recovery_state = _load_recovery_state(workspace_root)
    return {
        "workspace_path": str(workspace_root),
        "active_workspace_path": str(active_root),
        "workspace_scope": workspace_scope_id(workspace_root),
        "project_name": workspace_root.name,
        "session_id": session_data.get("session_id", ""),
        "tmux_pane": session_data.get("tmux_pane", ""),
        "heartbeat": heartbeat,
        "has_running_experiments": has_running,
        "stage": status.stage,
        "paused": status.paused,
        "stop_requested": status.stop_requested,
        "auto_resume_pending": status.paused and not status.stop_requested,
        "should_keep_running": should_keep_running,
        "saved_at": session_data.get("saved_at", 0),
        "ralph_prompt_path": session_data.get("ralph_prompt_path", ralph_prompt_path),
        "ownership_conflict": bool(session_data.get("ownership_conflict", False)),
        "conflicts": list(session_data.get("conflicts", [])),
        "recovery": recovery_state,
    }


def _cleanup_sentinel_registry_unlocked(registry: dict[str, dict]) -> dict[str, dict]:
    cleaned: dict[str, dict] = {}
    for workspace_key, entry in registry.items():
        try:
            workspace_root = resolve_workspace_root(Path(workspace_key))
        except OSError:
            continue
        if not workspace_root.exists():
            continue
        state = _load_workspace_sentinel_state(workspace_root)
        if not state["should_keep_running"]:
            continue
        if not state["session_id"] and not state["tmux_pane"]:
            continue
        cleaned[str(workspace_root)] = {
            "workspace_root": str(workspace_root),
            "project_name": state["project_name"],
            "workspace_scope": state["workspace_scope"],
            "session_id": state["session_id"],
            "tmux_pane": state["tmux_pane"],
            "saved_at": state["saved_at"],
            "ralph_prompt_path": state["ralph_prompt_path"],
        }
    return cleaned


def _sentinel_conflicts(
    workspace_root: Path,
    registry: dict[str, dict],
    *,
    session_id: str,
    tmux_pane: str,
) -> list[dict]:
    workspace_key = str(resolve_workspace_root(workspace_root))
    conflicts: list[dict] = []
    for other_workspace, entry in registry.items():
        if other_workspace == workspace_key:
            continue
        reasons: list[str] = []
        if session_id and entry.get("session_id") == session_id:
            reasons.append("session_id")
        if tmux_pane and entry.get("tmux_pane") == tmux_pane:
            reasons.append("tmux_pane")
        if reasons:
            conflicts.append({
                "workspace_path": other_workspace,
                "project_name": entry.get("project_name", Path(other_workspace).name),
                "reasons": reasons,
            })
    return conflicts


def write_sentinel_heartbeat(workspace_path: str, stage: str, action: str) -> None:
    """Write heartbeat file for Sentinel watchdog (best-effort)."""
    hb_path = resolve_workspace_root(workspace_path) / "sentinel_heartbeat.json"
    _write_json_atomic(hb_path, {
        "ts": time.time(),
        "stage": stage,
        "action": action,
    })


def write_breadcrumb(
    workspace_path: str,
    action_dict: dict | None = None,
    *,
    stage: str = "",
    completed: bool = False,
) -> None:
    """Write breadcrumb file for context recovery after compaction/restart."""
    _ = completed
    workspace_root = resolve_workspace_root(workspace_path)
    bc_path = workspace_root / "breadcrumb.json"
    if action_dict:
        action_type = action_dict.get("action_type", "")
        payload = {
            "ts": time.time(),
            "stage": action_dict.get("stage", stage),
            "action_type": action_type,
            "iteration": action_dict.get("iteration", 0),
            "workspace_path": str(workspace_root),
            "in_loop": action_type in _LOOP_ACTION_TYPES,
            "loop_type": action_type if action_type in _LOOP_ACTION_TYPES else "",
            "description": action_dict.get("description", "")[:200],
        }
    else:
        payload = {
            "ts": time.time(),
            "stage": stage,
            "action_type": "completed",
            "workspace_path": str(workspace_root),
            "in_loop": False,
            "loop_type": "",
            "description": f"Stage '{stage}' completed, advancing to next",
        }
    _write_json_atomic(bc_path, payload)


def _build_resume_recovery_payload(
    orchestrator: Any,
    workspace_path: str,
    *,
    resume_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace_root = resolve_workspace_root(workspace_path)
    breadcrumb = _read_json(workspace_root / "breadcrumb.json")
    if resume_action is None:
        resume_action = orchestrator.get_next_action()
    experiment_monitor = resume_action.get("experiment_monitor", {})
    if not isinstance(experiment_monitor, dict):
        experiment_monitor = {}
    background_agent = experiment_monitor.get("background_agent", {})
    if not isinstance(background_agent, dict):
        background_agent = {}

    pending_sync_path = workspace_root / "lark_sync" / "pending_sync.jsonl"
    sync_status_path = workspace_root / "lark_sync" / "sync_status.json"
    pending_sync_count, pending_sync_total_count, sync_status = _count_pending_sync_backlog(
        pending_sync_path,
        sync_status_path,
    )

    pending_hooks: list[dict[str, Any]] = []
    if pending_sync_count > 0:
        pending_hooks.append({
            "name": "lark_sync",
            "pending_count": pending_sync_count,
            "pending_total_count": pending_sync_total_count,
            "path": str(pending_sync_path),
            "resume_hint": (
                "run `sibyl sync <workspace>` before continuing the loop"
            ),
        })

    pending_background_agents: list[dict[str, Any]] = []
    if background_agent.get("name"):
        pending_background_agents.append({
            "name": background_agent.get("name", ""),
            "args": background_agent.get("args", ""),
            "action_type": resume_action.get("action_type", ""),
            "stage": resume_action.get("stage", ""),
            "resume_hint": (
                "restart this background agent with run_in_background=true before "
                "resuming the main loop"
            ),
        })

    return {
        "resume_action_type": resume_action.get("action_type", ""),
        "resume_action": resume_action,
        "background_agent_required": bool(pending_background_agents),
        "pending_sync_count": pending_sync_count,
        "pending_sync_total_count": pending_sync_total_count,
        "lark_sync_status": sync_status,
        "pending_hooks": pending_hooks,
        "pending_background_agents": pending_background_agents,
        "breadcrumb": breadcrumb,
        "recovery_state_path": str(_recovery_state_path(workspace_root)),
    }


def cli_init(
    topic: str,
    project_name: str | None = None,
    config_path: str | None = None,
    *,
    orchestrator_cls: type[Any],
    event_logger_cls: type[Any],
) -> dict[str, Any]:
    """CLI: Initialize a project."""
    from .project_cli import _build_post_init_guide
    from .config_helpers import load_effective_config

    result = orchestrator_cls.init_project(topic, project_name, config_path)
    config = load_effective_config(result["workspace_path"], config_path=config_path)
    result["guide"] = _build_post_init_guide(
        result["workspace_path"],
        result["project_name"],
        topic,
        config,
        has_spec=False,
    )
    print(json.dumps(result, indent=2))
    try:
        event_logger_cls(Path(result["workspace_path"])).project_init(
            topic=topic,
            project_name=result.get("project_name", ""),
        )
    except Exception:
        pass
    return result


def cli_next(
    workspace_path: str,
    *,
    orchestrator_cls: type[Any],
    event_logger_cls: type[Any],
) -> dict[str, Any]:
    """CLI: Get next action."""
    orchestrator = orchestrator_cls(workspace_path)
    action = orchestrator.get_next_action()
    recovery_state = _persist_recovery_state(
        workspace_path,
        _build_resume_recovery_payload(
            orchestrator,
            workspace_path,
            resume_action=action,
        ),
        source="cli_next",
    )
    print(json.dumps(action, indent=2))
    try:
        write_sentinel_heartbeat(workspace_path, action.get("stage", ""), "cli_next")
        write_breadcrumb(workspace_path, action_dict=action)
        action_type = action.get("action_type", "")
        if action_type not in ("done", "stopped", "gpu_poll", "experiment_wait"):
            event_logger_cls(Path(workspace_path)).stage_start(
                stage=action.get("stage", ""),
                iteration=action.get("iteration", 0),
                action_type=action_type,
                description=action.get("description", "")[:200],
            )
        _ = recovery_state
    except Exception:
        pass
    return action


def cli_record(
    workspace_path: str,
    stage: str,
    result: str = "",
    score: float | None = None,
    *,
    orchestrator_cls: type[Any],
    event_logger_cls: type[Any],
) -> dict[str, Any]:
    """CLI: Record stage result."""
    orchestrator = orchestrator_cls(workspace_path)
    prev_status = orchestrator.ws.get_status()
    stage_started_at = prev_status.stage_started_at

    orchestrator.record_result(stage, result, score)
    new_status = orchestrator.ws.get_status()
    output = {"status": "ok", "new_stage": new_status.stage}
    no_sync_trigger = {"init", "quality_gate", "done", "lark_sync"}
    if orchestrator.config.lark_enabled and stage not in no_sync_trigger:
        output["sync_requested"] = True
    recovery_state = _persist_recovery_state(
        workspace_path,
        _build_resume_recovery_payload(orchestrator, workspace_path),
        source="cli_record",
    )
    print(json.dumps(output))
    try:
        write_sentinel_heartbeat(workspace_path, stage, "cli_record")
        write_breadcrumb(workspace_path, stage=stage, completed=True)
        duration = (time.time() - stage_started_at) if stage_started_at else None
        event_logger_cls(Path(workspace_path)).stage_end(
            stage=stage,
            iteration=prev_status.iteration,
            duration_sec=duration,
            score=score,
            next_stage=new_status.stage,
        )
        _ = recovery_state
    except Exception:
        pass
    return output


def cli_pause(
    workspace_path: str,
    reason: str = "rate_limit",
    *,
    orchestrator_cls: type[Any],
    event_logger_cls: type[Any],
) -> dict[str, str]:
    """CLI: Write a legacy pause marker or manual stop marker."""
    orchestrator = orchestrator_cls(workspace_path)
    orchestrator.ws.pause(reason)
    status = orchestrator.ws.get_status()
    status_value = "stopped" if reason == "user_stop" else "paused"
    payload = {"status": status_value, "stage": status.stage}
    print(json.dumps(payload))
    try:
        event_logger_cls(Path(workspace_path)).pause(
            reason=reason,
            stage=status.stage,
            iteration=status.iteration,
        )
    except Exception:
        pass
    return payload


def cli_resume(
    workspace_path: str,
    *,
    orchestrator_cls: type[Any],
    event_logger_cls: type[Any],
) -> dict[str, Any]:
    """CLI: Clear stop/pause markers and resume a project."""
    orchestrator = orchestrator_cls(workspace_path)
    orchestrator.ws.resume()
    stop_file = resolve_workspace_root(workspace_path) / "sentinel_stop.json"
    stop_file.unlink(missing_ok=True)
    status = orchestrator.ws.get_status()
    recovery = _build_resume_recovery_payload(orchestrator, workspace_path)
    payload = {
        "status": "resumed",
        "stage": status.stage,
        "iteration": status.iteration,
        **recovery,
    }
    persisted_recovery = _persist_recovery_state(
        workspace_path,
        recovery,
        source="cli_resume",
    )
    payload["recovery"] = persisted_recovery
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    try:
        write_sentinel_heartbeat(workspace_path, status.stage, "cli_resume")
        if isinstance(payload.get("resume_action"), dict):
            write_breadcrumb(workspace_path, action_dict=payload["resume_action"])
        event_logger_cls(Path(workspace_path)).resume(
            stage=status.stage,
            iteration=status.iteration,
            action_type=payload.get("resume_action_type", ""),
            pending_sync_count=payload.get("pending_sync_count", 0),
            background_agent_required=payload.get("background_agent_required", False),
        )
    except Exception:
        pass
    return payload


def cli_status(
    workspace_path: str,
) -> dict[str, Any]:
    """CLI: Get project status."""
    workspace_root = resolve_workspace_root(workspace_path)
    from .migration_cli import ensure_workspace_iteration_dirs

    preferred_iteration_dirs = (
        load_effective_config(workspace_path=workspace_root).iteration_dirs
        if (workspace_root / "config.yaml").exists()
        else False
    )
    ensure_workspace_iteration_dirs(
        workspace_root,
        preferred_enabled=preferred_iteration_dirs,
    )
    ws = Workspace.open_existing(workspace_root.parent, workspace_root.name)
    status = ws.get_project_metadata()
    status["topic"] = ws.read_file("topic.txt") or ""
    pending_sync_path = ws.root / "lark_sync" / "pending_sync.jsonl"
    sync_status_path = ws.root / "lark_sync" / "sync_status.json"
    pending_sync_count, pending_sync_total_count, sync_status = _count_pending_sync_backlog(
        pending_sync_path,
        sync_status_path,
    )
    status["pending_sync_count"] = pending_sync_count
    status["pending_sync_total_count"] = pending_sync_total_count
    if sync_status_path.exists():
        status["lark_sync_status"] = sync_status or {"error": "corrupted sync_status.json"}
    status["recovery"] = _load_recovery_state(workspace_root)
    print(json.dumps(status, indent=2))
    return status


def cli_sync(
    workspace_path: str,
) -> dict[str, Any]:
    """CLI: Acknowledge pending Lark sync triggers in Codex-native mode.

    The repo-local Python runner cannot perform MCP-backed Feishu/Lark uploads by
    itself. Instead, it records that the backlog was seen and writes an explicit
    deferred status so resume/recovery logic stops treating the same entries as a
    never-started background worker.
    """
    workspace_root = resolve_workspace_root(workspace_path)
    sync_dir = workspace_root / "lark_sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    pending_sync_path = sync_dir / "pending_sync.jsonl"
    sync_status_path = sync_dir / "sync_status.json"
    lock_path = sync_dir / "sync.lock"

    pending_sync_count, pending_sync_total_count, current_status = _count_pending_sync_backlog(
        pending_sync_path,
        sync_status_path,
    )
    last_synced_line = int(current_status.get("last_synced_line", 0) or 0)

    if pending_sync_count <= 0:
        payload = {
            "status": "ok",
            "state": "noop",
            "workspace_path": str(workspace_root),
            "pending_sync_count": 0,
            "pending_sync_total_count": pending_sync_total_count,
            "last_synced_line": last_synced_line,
            "last_attempted_line": int(current_status.get("last_attempted_line", 0) or 0),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return payload

    entries: list[dict[str, Any]] = []
    try:
        lines = pending_sync_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    start_line = max(
        int(current_status.get("last_synced_line", 0) or 0),
        int(current_status.get("last_attempted_line", 0) or 0),
    )
    for line in lines[start_line:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)

    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    lock_path.write_text(
        json.dumps(
            {
                "started_at": started_at,
                "workspace_path": str(workspace_root),
                "mode": "codex-local-deferred",
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        stages = [
            str(entry.get("trigger_stage", "")).strip()
            for entry in entries
            if str(entry.get("trigger_stage", "")).strip()
        ]
        error_message = (
            "Deferred pending Feishu/Lark sync requests in Codex-native local mode. "
            "The repo-local `sibyl sync` runner acknowledges backlog bookkeeping, but "
            "real cloud upload still requires an MCP-backed Codex agent path."
        )
        history = current_status.get("history", [])
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "at": started_at,
                "success": False,
                "state": "deferred",
                "stages_seen": stages,
                "pending_count": pending_sync_count,
                "reason": error_message,
            }
        )
        updated_status = {
            **current_status,
            "state": "deferred",
            "last_attempted_at": started_at,
            "last_attempted_line": pending_sync_total_count,
            "last_sync_success": False,
            "last_synced_line": last_synced_line,
            "last_trigger_stage": stages[-1] if stages else "",
            "last_error": error_message,
            "history": history[-20:],
        }
        _write_json_atomic(sync_status_path, updated_status)
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass

    payload = {
        "status": "ok",
        "state": "deferred",
        "workspace_path": str(workspace_root),
        "pending_sync_count": 0,
        "pending_sync_total_count": pending_sync_total_count,
        "last_synced_line": last_synced_line,
        "last_attempted_line": pending_sync_total_count,
        "stages_seen": stages,
        "message": (
            "Pending sync entries were acknowledged locally. "
            "Cloud sync remains deferred until an MCP-backed worker is available."
        ),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload


def cli_checkpoint(
    workspace_path: str,
    stage: str,
    step_id: str,
    *,
    checkpoint_dirs: dict[str, str],
) -> dict[str, Any]:
    """CLI: Mark a checkpoint sub-step as completed."""
    checkpoint_dir = checkpoint_dirs.get(stage)
    if checkpoint_dir is None:
        payload = {
            "status": "error",
            "message": f"No checkpoint support for stage '{stage}'",
        }
        print(json.dumps(payload))
        return payload

    ws_path = Path(workspace_path)
    ws = Workspace(ws_path.parent, ws_path.name)
    artifacts: list[str] | None = None
    has_figures_block = True
    if stage == "writing_sections":
        section_md = ws.read_file(f"writing/sections/{step_id}.md") or ""
        artifacts, has_figures_block = extract_section_figure_artifacts(section_md)
        if not has_figures_block:
            artifacts = None

    result = ws.complete_checkpoint_step(
        checkpoint_dir,
        step_id,
        artifacts=artifacts,
        require_artifacts_metadata=(stage == "writing_sections"),
    )

    payload = {
        "status": "ok",
        "stage": stage,
        "step": step_id,
        "completed": result["completed"],
    }
    if stage == "writing_sections" and not has_figures_block:
        payload["message"] = "section 缺少 <!-- FIGURES --> block，checkpoint 未标记完成"
    elif not result["completed"]:
        payload["message"] = "checkpoint 未标记完成，请补齐缺失产物后重试"
    if result["missing_files"]:
        payload["missing_files"] = result["missing_files"]
    print(json.dumps(payload))
    try:
        status = ws.get_status()
        EventLogger(ws.root).checkpoint_step(
            stage=stage,
            step_id=step_id,
            iteration=status.iteration,
        )
    except Exception:
        pass
    return payload


def cli_sentinel_session(
    workspace_path: str,
    session_id: str,
    tmux_pane: str = "",
) -> dict[str, Any]:
    """CLI: Save Claude Code session ownership for Sentinel and Ralph loop isolation."""
    workspace_root = resolve_workspace_root(workspace_path)
    payload = {
        "workspace_path": str(workspace_root),
        "workspace_scope": workspace_scope_id(workspace_root),
        "project_name": workspace_root.name,
        "session_id": session_id,
        "tmux_pane": tmux_pane,
        "saved_at": time.time(),
        "ralph_prompt_path": str((workspace_root / ".codex" / "loop-prompt.txt").resolve()),
    }

    with _sentinel_registry_lock():
        registry = _cleanup_sentinel_registry_unlocked(_load_sentinel_registry_unlocked())
        conflicts = _sentinel_conflicts(
            workspace_root,
            registry,
            session_id=session_id,
            tmux_pane=tmux_pane,
        )
        payload["ownership_conflict"] = bool(conflicts)
        payload["conflicts"] = conflicts

        workspace_key = str(workspace_root)
        if conflicts or (not session_id and not tmux_pane):
            registry.pop(workspace_key, None)
        else:
            registry[workspace_key] = {
                "workspace_root": workspace_key,
                "project_name": workspace_root.name,
                "workspace_scope": payload["workspace_scope"],
                "session_id": session_id,
                "tmux_pane": tmux_pane,
                "saved_at": payload["saved_at"],
                "ralph_prompt_path": payload["ralph_prompt_path"],
            }
        _save_sentinel_registry_unlocked(registry)

    _write_json_atomic(workspace_root / "sentinel_session.json", payload)
    output = {
        "status": "conflict" if payload["ownership_conflict"] else "ok",
        "workspace_path": str(workspace_root),
        "project_name": workspace_root.name,
        "session_id": session_id,
        "tmux_pane": tmux_pane,
        "ownership_conflict": payload["ownership_conflict"],
        "conflicts": payload["conflicts"],
        "ralph_prompt_path": payload["ralph_prompt_path"],
    }
    print(json.dumps(output, indent=2))
    return output


def cli_sentinel_config(
    workspace_path: str,
) -> dict[str, Any]:
    """CLI: Get Sentinel configuration for watchdog script."""
    workspace_root = resolve_workspace_root(workspace_path)
    state = _load_workspace_sentinel_state(workspace_root)

    with _sentinel_registry_lock():
        registry = _cleanup_sentinel_registry_unlocked(_load_sentinel_registry_unlocked())
        _save_sentinel_registry_unlocked(registry)
        conflicts = _sentinel_conflicts(
            workspace_root,
            registry,
            session_id=state["session_id"],
            tmux_pane=state["tmux_pane"],
        )

    state["conflicts"] = conflicts or state["conflicts"]
    state["ownership_conflict"] = bool(state["ownership_conflict"] or conflicts)
    state["watchdog_allowed"] = not state["ownership_conflict"]
    print(json.dumps(state, indent=2))
    return state


def cli_list_projects(
    workspaces_dir: str | None = None,
    *,
    workspace_cls: type[Any],
) -> list[dict[str, Any]]:
    """CLI: List all projects."""
    if workspaces_dir is None:
        from .config_helpers import load_effective_config

        ws_dir = load_effective_config().workspaces_dir
    else:
        ws_dir = Path(workspaces_dir)
    if not ws_dir.exists():
        print(json.dumps([]))
        return []

    projects = []
    for child in sorted(ws_dir.iterdir()):
        if child.is_dir() and (child / "status.json").exists():
            try:
                ws = workspace_cls.open_existing(ws_dir, child.name)
                meta = ws.get_project_metadata()
                meta["topic"] = ws.read_file("topic.txt") or ""
                projects.append(meta)
            except Exception:
                continue
    print(json.dumps(projects, indent=2))
    return projects


def cli_dashboard_data(
    workspace_path: str,
    events_tail: int = 50,
) -> dict[str, Any]:
    """CLI: Aggregate all monitoring data for frontend dashboard."""
    payload = collect_dashboard_data(workspace_path, events_tail=events_tail)
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return payload
