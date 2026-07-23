# Distribution

How people (and their agents) get bumper into a project. bumper is an unusual shape for packaging: it
is **project-local scaffolding** (`.bumper/` lives in each repo, and the hook commands reference it by
path) plus a small **CLI**, not a global library. That shape rules some channels in and others out.

## Today: GitHub + `install.sh`

The source of truth. `git clone` the repo, run `./install.sh <target>`. Zero runtime dependencies
beyond Python 3 + Claude Code. This is enough to dogfood with real projects and gather feedback before
committing to anything heavier.

**Pro:** nothing to publish, nothing to maintain, works immediately.
**Con:** every user needs a local clone; upgrades mean `git pull` + re-run `install.sh`.

## Option A — `curl | bash` one-liner

A hosted bootstrap script that downloads the payload and installs it, e.g.:

```bash
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/bumper_bot/main/install.sh | bash -s -- .
```

This needs the installer reworked to *fetch* the `.bumper/` files (from a release tarball or raw
GitHub) instead of copying from a local clone. Low effort, big UX win, no package registry.
**Best if:** we want a frictionless human install without publishing to a registry.

## Option B — Claude Code plugin (the most idiomatic fit)

bumper *is* a bundle of Claude Code hooks. Claude Code's plugin system distributes exactly that — hooks
plus commands/skills — via a marketplace:

```
/plugin marketplace add SupposedlySam/bumper_bot
/plugin install bumper
```

The plugin would ship the `bumper` CLI and register the hooks; the per-project `.bumper/state.json`
and `config.json` stay local (created by a `/bumper init` command or first run). This is the natural
channel for the **agent-first** audience — it's how Claude Code users already discover and install
tooling.
**Best if:** the primary users are Claude Code sessions/humans in Claude Code. Requires restructuring
into the plugin layout (`.claude-plugin/`, `hooks/`, `commands/`) and hosting a marketplace manifest.

## Option C — PyPI + `pipx`

Package the CLI so it installs globally:

```bash
pipx install bumper-bot
bumper init          # scaffolds .bumper/ and wires the hooks into this project
```

The bash write-guard ships as package data; `bumper init` writes it into the project's `.bumper/bin/`.
Gives a clean global `bumper` command and standard upgrades (`pipx upgrade`).
**Best if:** we want a "real," versioned CLI with a familiar install path for the general dev audience.
Requires a `pyproject.toml`, an entry point, bundling the non-Python files, and release plumbing.

## Not a fit

- **npm / Homebrew core** — bumper isn't JS, and it isn't a standalone binary; a Homebrew *tap* could
  wrap Option C but adds a channel without adding reach.

## Recommendation

1. **Now:** ship on GitHub as-is and dogfood with real projects (pebble, gents).
2. **Next, quick win:** add the **Option A** `curl | bash` installer — cheap, removes the clone step.
3. **Then, pick the primary audience:**
   - agent-first / Claude Code users → **Option B (plugin)**;
   - general CLI users → **Option C (PyPI/pipx)**.

They aren't mutually exclusive — GitHub + a plugin, or GitHub + PyPI, are both coherent. The open
question is which *second* channel is worth the maintenance, and that depends on who we expect to use
it.
