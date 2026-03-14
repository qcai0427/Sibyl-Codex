"""Helpers for running Sibyl experiment batches on the local machine."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from sibyl.experiment_recovery import (
    _load_gpu_progress,
    _save_gpu_progress,
    load_experiment_state,
    save_experiment_state,
    sync_to_gpu_progress,
)
from sibyl.gpu_scheduler import parse_free_gpus, parse_gpu_snapshot, write_poll_result

from .config_helpers import load_effective_config
from .workspace_paths import project_marker_file, resolve_active_workspace_path, resolve_workspace_root


def cli_local_gpu_poll(workspace_path: str) -> dict[str, Any]:
    """Poll local GPUs with nvidia-smi and persist the project-scoped marker."""
    workspace_root = resolve_workspace_root(workspace_path)
    config = load_effective_config(workspace_path=workspace_path)
    marker_file = project_marker_file(workspace_root, "gpu_free")

    query_cmd = [
        "nvidia-smi",
        "--query-gpu=index,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]

    attempt = 0
    while True:
        attempt += 1
        proc = subprocess.run(query_cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            payload = {
                "status": "nvidia_smi_error",
                "attempt": attempt,
                "returncode": proc.returncode,
                "stderr": proc.stderr.strip(),
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            if config.gpu_poll_max_attempts > 0 and attempt >= config.gpu_poll_max_attempts:
                return payload
            time.sleep(max(1, config.gpu_poll_interval_sec))
            continue

        output = proc.stdout.strip()
        snapshot = parse_gpu_snapshot(output)
        free_gpus = parse_free_gpus(
            output,
            threshold_mb=config.gpu_free_threshold_mb,
            max_gpus=config.max_gpus,
            aggressive_mode=config.gpu_aggressive_mode,
            aggressive_threshold_pct=config.gpu_aggressive_threshold_pct,
        )
        payload = write_poll_result(
            marker_file,
            free_gpus=free_gpus,
            poll_count=attempt,
            snapshot=snapshot,
            source="local-gpu-poll",
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if free_gpus:
            return payload
        if config.gpu_poll_max_attempts > 0 and attempt >= config.gpu_poll_max_attempts:
            return payload
        time.sleep(max(1, config.gpu_poll_interval_sec))


def _load_task_index(active_root: Path) -> dict[str, dict[str, Any]]:
    task_plan_path = active_root / "plan" / "task_plan.json"
    payload = json.loads(task_plan_path.read_text(encoding="utf-8"))
    tasks = payload.get("tasks", [])
    return {
        str(task["id"]): task
        for task in tasks
        if isinstance(task, dict) and str(task.get("id", "")).strip()
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_progress(results_dir: Path, task_id: str, *, status: str, message: str) -> None:
    _write_json(
        results_dir / f"{task_id}_PROGRESS.json",
        {
            "task_id": task_id,
            "status": status,
            "message": message,
            "updated_at": dt.datetime.now().isoformat(),
        },
    )


def _update_task_state(
    active_root: Path,
    task_id: str,
    *,
    status: str,
    gpu_ids: list[int],
    log_path: Path,
    expected_output: str,
    planned_minutes: int,
    started_at: str,
    exit_code: int | None = None,
) -> None:
    state = load_experiment_state(active_root)
    task = state.tasks.setdefault(task_id, {})
    task["status"] = status
    task["gpu_ids"] = gpu_ids
    task["started_at"] = started_at
    task["completed_at"] = dt.datetime.now().isoformat()
    task["log_path"] = str(log_path)
    task["expected_output"] = expected_output
    if exit_code is not None:
        task["exit_code"] = exit_code
    save_experiment_state(active_root, state)
    sync_to_gpu_progress(active_root, state)

    progress = _load_gpu_progress(active_root)
    elapsed_min = max(
        1,
        int(
            (
                dt.datetime.now() - dt.datetime.fromisoformat(started_at)
            ).total_seconds()
            / 60
        ),
    )
    progress.setdefault("timings", {})
    progress["timings"][task_id] = {
        "planned_min": planned_minutes,
        "actual_min": elapsed_min,
    }
    _save_gpu_progress(active_root, progress)


def cli_local_experiment_run(workspace_path: str, mode: str, batch_json: str) -> dict[str, Any]:
    """Execute one claimed experiment batch locally using task_plan local_runner commands."""
    workspace_root = resolve_workspace_root(workspace_path)
    active_root = resolve_active_workspace_path(workspace_path)
    task_index = _load_task_index(active_root)
    batch = json.loads(batch_json)
    results_dir = active_root / "exp" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    failures = 0
    for assignment in batch:
        gpu_ids = [int(gpu_id) for gpu_id in assignment.get("gpu_ids", [])]
        task_ids = [str(task_id) for task_id in assignment.get("task_ids", [])]
        cuda_visible_devices = ",".join(str(gpu_id) for gpu_id in gpu_ids)

        for task_id in task_ids:
            task = task_index.get(task_id)
            if task is None:
                raise KeyError(f"Task '{task_id}' not found in task_plan.json")
            runner = task.get("local_runner") or {}
            command_template = str(runner.get("command", "")).strip()
            if not command_template:
                raise ValueError(f"Task '{task_id}' is missing local_runner.command")

            expected_output = str(task.get("expected_output", "")).strip()
            started_at = dt.datetime.now().isoformat()
            log_path = results_dir / f"{task_id}.log"
            done_path = results_dir / f"{task_id}_DONE"
            pid_path = results_dir / f"{task_id}.pid"

            env = os.environ.copy()
            env["CUDA_VISIBLE_DEVICES"] = cuda_visible_devices
            env["SIBYL_WORKSPACE"] = str(workspace_root)
            env["SIBYL_ACTIVE_WORKSPACE"] = str(active_root)
            env["SIBYL_TASK_ID"] = task_id
            env["SIBYL_TASK_MODE"] = mode

            command = command_template.format(
                workspace=str(workspace_root),
                active_workspace=str(active_root),
                task_id=task_id,
                mode=mode,
                gpu_ids=",".join(str(gpu_id) for gpu_id in gpu_ids),
                cuda_visible_devices=cuda_visible_devices,
                expected_output=expected_output,
            )

            pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            _write_progress(results_dir, task_id, status="running", message="local task started")

            with open(log_path, "w", encoding="utf-8") as log_file:
                log_file.write(f"$ {command}\n\n")
                proc = subprocess.run(
                    ["/bin/bash", "-lc", command],
                    cwd=active_root,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    check=False,
                )

            success = proc.returncode == 0
            _update_task_state(
                active_root,
                task_id,
                status="completed" if success else "failed",
                gpu_ids=gpu_ids,
                log_path=log_path,
                expected_output=expected_output,
                planned_minutes=int(task.get("estimated_minutes", 0) or 0),
                started_at=started_at,
                exit_code=proc.returncode,
            )
            _write_progress(
                results_dir,
                task_id,
                status="completed" if success else "failed",
                message="local task finished",
            )
            _write_json(
                done_path,
                {
                    "task_id": task_id,
                    "mode": mode,
                    "exit_code": proc.returncode,
                    "success": success,
                    "completed_at": dt.datetime.now().isoformat(),
                    "gpu_ids": gpu_ids,
                    "log_path": str(log_path),
                    "expected_output": expected_output,
                },
            )

            if not success:
                failures += 1

            results.append(
                {
                    "task_id": task_id,
                    "gpu_ids": gpu_ids,
                    "exit_code": proc.returncode,
                    "success": success,
                    "log_path": str(log_path),
                    "expected_output": expected_output,
                }
            )

    payload = {
        "workspace": str(workspace_root),
        "active_workspace": str(active_root),
        "mode": mode,
        "failures": failures,
        "results": results,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if failures:
        raise SystemExit(1)
    return payload
