# Vendored skills

Third-party skills installed into `.agents/skills/`. **These are currently
gitignored.** Using a skill locally is not redistribution; committing it is. Each
one's licence has to be cleared before this repo carries the files.

This manifest is tracked, so the repo records what is in use even while the skill
files themselves are not.

| Skill | Source | Licence | Status |
|---|---|---|---|
| `test-driven-development` | [obra/superpowers](https://github.com/obra/superpowers), `skills/test-driven-development` | MIT, © 2025 Jesse Vincent | Gitignored. `LICENSE` vendored alongside it. MIT permits redistribution with the notice preserved, so this one is clear to commit whenever you want it tracked. |

## Before the project ships

Every row above resolves one of two ways:

1. **The licence permits redistribution** — keep its notice file alongside the
   skill, drop the `.gitignore` entry, commit it.
2. **It does not, or it is unclear** — remove the skill, or replace it with one
   that does.

MIT, Apache-2.0 and BSD are fine with the notice preserved. AGPL is not, for the
same reason `cook.py` is not: this repo is Apache-2.0 and copyleft only flows one
way. See the Licensing section of `AGENTS.md`.

## Adding one

`npx skills add` writes to the agent's own directory, which for Claude Code is
`.claude/skills/` — and that is gitignored here, so an install would work locally
and silently never commit. Relocate it:

```sh
npx skills add <owner/repo>                      # lands in .claude/skills/<name>
mv .claude/skills/<name> .agents/skills/<name>   # the real location
ln -sfn "../../.agents/skills/<name>" ".claude/skills/<name>"
```

Then **read the `SKILL.md` before using it** — a skill is instructions that
become project policy the moment it loads — add a row above, and gitignore it
until its licence is cleared.
