# Changelog

## 1.0.0
- Exact incremental local mirror with git-versioned history.
- Private GitHub backup; secrets and >95 MB files kept off GitHub; public repos refused.
- Project git history preserved via bundle; full restore (branches, tags, remotes, uncommitted work).
- Claude Code integration: SessionStart / PreToolUse / PostToolUse / Stop hooks, standing orders, `/rescue` and `/vault`.
- Codex CLI and Gemini CLI standing orders.
- `rescue` for local and fresh-machine recovery; `restore` for any version.
- Self-healing: lost backup folder is rebuilt from GitHub; GitHub repo attached later if `gh` login happens after first use.
- Safety stop against mass deletion, surfaced to the agent at session start; auto-protect guards (home, system folders, size limits); opt-out.
