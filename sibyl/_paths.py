"""Canonical repository paths — single source of truth."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEX_RUNTIME_DIR = REPO_ROOT / ".codex"
SYSTEM_STATE_DIR = REPO_ROOT / ".sibyl"
SYSTEM_EVOLUTION_DIR = SYSTEM_STATE_DIR / "evolution"


def _runtime_path_from_env(var_name: str) -> Path | None:
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def get_system_state_dir() -> Path:
    """Resolve the runtime system state directory.

    This allows tests and isolated runs to redirect Sibyl's mutable global state
    without importing a different module graph.
    """
    return _runtime_path_from_env("SIBYL_STATE_DIR") or SYSTEM_STATE_DIR


def get_system_evolution_dir() -> Path:
    """Resolve the runtime self-evolution directory."""
    explicit = _runtime_path_from_env("SIBYL_EVOLUTION_DIR")
    if explicit is not None:
        return explicit
    state_dir = get_system_state_dir()
    if state_dir != SYSTEM_STATE_DIR:
        return state_dir / "evolution"
    return SYSTEM_EVOLUTION_DIR
