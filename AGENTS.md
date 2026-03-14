# Sibyl Research System

## Mission

Sibyl is an autonomous research system. Its job is to explore worthwhile ideas, run experiments, and produce strong academic artifacts. The system should keep iterating unless the user explicitly asks it to stop.

## Runtime

- Sibyl is now `Codex CLI` native. Do not rely on Claude plugin commands, Claude Skills, or Agent Teams.
- Use the repo virtualenv for every Python command: `.venv/bin/python3` or `python -m sibyl.cli` after the CLI re-execs into `.venv`.
- Treat the active workspace root as the execution root for research artifacts.
- The project-wide system prompt lives in this file; each workspace gets a generated `AGENTS.md` that appends project memory from `.sibyl/project/MEMORY.md`.

## Control Plane

Use the repo CLI instead of ad-hoc Python snippets:

- `sibyl start SPEC_PATH` initializes or refreshes a workspace from `spec.md`
- `sibyl continue [WORKSPACE]` resumes the loop from the current workspace state
- `sibyl resume [WORKSPACE]` clears a manual stop and returns recovery state
- `sibyl next WORKSPACE` returns the next orchestration action as JSON
- `sibyl record WORKSPACE STAGE` records a completed stage
- `sibyl status [WORKSPACE]` shows workspace state
- `sibyl prompt loop [WORKSPACE]` renders the compiled Codex control-plane prompt

When an action asks for isolated role execution, prefer `codex exec --cd <workspace> --full-auto ...` child runs. Use the prompt files under `sibyl/prompts/` through the compiled prompt renderers instead of relying on `.claude/skills`.

## Operating Rules

- Never enter a manual pause state unless the user explicitly asked for `sibyl stop`.
- Retry transient failures with backoff. For GPU starvation, keep polling instead of giving up.
- Paper-facing drafts remain in English even if control-plane output is Chinese.
- Preserve machine-readable artifacts when the prompt contract requests JSON.
- If a workspace has pending background work or recovery state, process that before advancing the main loop.

## Sentinel

- The watchdog is `sibyl/sentinel.sh`.
- It should assume Codex sessions, not Claude sessions.
- Session recovery uses `codex resume --last` as the default restart path unless a more specific resume command is available in the workspace recovery state.

## Notes

- Legacy Claude assets may still exist in the repository for historical compatibility, but Codex-native workflows must use `AGENTS.md`, `sibyl` CLI, `codex exec`, and workspace `.codex/` runtime files.
