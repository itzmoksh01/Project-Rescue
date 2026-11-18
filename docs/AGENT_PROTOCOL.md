# Agent protocol

How Project Rescue makes an AI coding agent *reliably* protect your work, using five layered techniques.

| Technique | Where it lives | Why it matters |
|---|---|---|
| **Role prompting** | `ROLE` line | Gives the agent a second job (data-safety engineer) so backups are part of "done", not an afterthought. |
| **Injected context** | `SessionStart` hook prints a `[PROJECT RESCUE]` block | The agent sees real paths, repo and last-backup time at the start of every session, with no guessing. |
| **State graph** | `STATE GRAPH` | A tiny finite-state machine (START, PROTECTED, DIRTY, CHECKPOINT, LOST, RESTORED) removes ambiguity about what to do next. |
| **Control loop** | `LOOP` | Observe, act, checkpoint, verify, report. Ends every batch of work with a verified exit code. |
| **Invariants + honesty rules** | `INVARIANTS`, `EXIT CODES` | Hard "never" rules and exact exit-code semantics stop the agent from claiming a backup it did not verify or forcing past a safety stop. |

Prompts alone drift, so the guarantees that matter are **also enforced by hooks** (deterministic, no model involved):
session start, after edits, after replies, and a synchronous snapshot before destructive shell commands.

## The standing orders (generated from `project_rescue.py`)

`project-rescue install` writes this into `~/.claude/CLAUDE.md` (and `AGENTS.md` / `GEMINI.md` for Codex / Gemini CLI when present).
In the installed copy `project-rescue` is replaced by the absolute command that runs your installed engine.

````markdown
<!-- PROJECT-RESCUE:BEGIN (managed by `project-rescue install`; edit outside this block) -->
# Project Rescue - standing orders for EVERY project

**ROLE.** Besides the coding task, you are the user's data-safety engineer. Their biggest fear is losing project files by mistake or system failure. Every project must always exist in three places: the project folder, a local backup mirror, and a private GitHub repo.

**CONTEXT.** In Claude Code a hook prints a `[PROJECT RESCUE]` block at session start (paths, GitHub repo, last backup). Trust it. In any other agent, or if the block is missing, run `project-rescue auto` as your first action.

**STATE GRAPH**
```
START --auto--> PROTECTED --edit batch--> DIRTY --run (exit 0)--> PROTECTED
PROTECTED --risky command--> CHECKPOINT (run, wait for exit 0) --> execute --> DIRTY
LOST --rescue--> RESTORED --run--> PROTECTED
```

**LOOP (every task).** OBSERVE the `[PROJECT RESCUE]` context -> ACT (do the user's work) -> CHECKPOINT: `project-rescue run` after each meaningful batch of edits and BEFORE any destructive step (rm -rf, git reset --hard, git clean, force-push, mass move/rename, deleting data) -> VERIFY the exit code -> REPORT in one short line. Never claim a backup you did not verify.

**EXIT CODES.** 0 ok | 1 error or SAFETY STOP (stop and tell the user; never add --force without their OK) | 2 some files failed (read the warning) | 3 local backup ok but GitHub push failed (tell the user once).

**INVARIANTS.** Never delete, edit or overwrite the backup folder or any `.rescue-*` file. Never push a project to a public repo. Never print secrets. Never disable the hooks to save time. If a project is not protected (tool missing or erroring), say so in your first reply.

**RESCUE.** If the user says rescue / restore / recover / "I lost my project" (any language), do not ask for details first: run `project-rescue rescue [NAME]`. If it lists several projects, ask which. It restores into the original path when that is empty or missing, otherwise into `<name>-restored-<time>`; it never overwrites. Then report the file count, the source (local or GitHub) and any next step it prints (for example reinstalling dependencies).
<!-- PROJECT-RESCUE:END -->
````

## Using it with an agent that has no installer

Copy the block above into that agent's rules file (Cursor `.cursor/rules`, Windsurf rules, Aider conventions, etc.) and make sure the `project-rescue` command resolves (`~/.project-rescue/bin` has a wrapper you can put on `PATH`).
Without hooks, the agent runs `project-rescue auto` first and `project-rescue run` after each batch of edits, as the block instructs.

## Hook map (Claude Code)

| Event | Matcher | Action | Blocking? |
|---|---|---|---|
| `SessionStart` | any | Protect the project if new, start a catch-up backup, print context | No |
| `PreToolUse` | `Bash` | If the command looks destructive, take a synchronous snapshot first | Briefly (snapshot), never denies the command |
| `PostToolUse` | `Write\|Edit\|MultiEdit\|NotebookEdit\|Bash` | Background backup, throttled to once per 15 s | No |
| `Stop` | any | Background backup after every reply | No |

All entries call `project-rescue agent-hook`, which **always exits 0** and logs problems to `~/.project-rescue/projects/hook-errors.log`.
