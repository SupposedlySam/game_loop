# Distribution

How people (and their agents) get game_loop into a project. game_loop is an unusual shape for packaging: it
is **project-local scaffolding** (`.game_loop/` lives in each repo, and the hook commands reference it by
path) plus a small **CLI**, not a global library. That shape rules some channels in and others out.

## Today: GitHub + `install.sh`

The source of truth. `git clone` the repo, run `./install.sh <target>`. Zero runtime dependencies
beyond Python 3 + Claude Code. This is enough to dogfood with real projects and gather feedback before
committing to anything heavier.

**Pro:** nothing to publish, nothing to maintain, works immediately.
**Con:** every user needs a local clone; upgrades mean `git pull` + re-run `install.sh`.

## Option A — `curl | bash` one-liner ✅ SHIPPED

A hosted bootstrap script that downloads the payload and installs it:

```bash
curl -fsSL https://raw.githubusercontent.com/SupposedlySam/game_loop/main/install.sh | bash -s -- .
```

`install.sh` now detects when it has no local clone (piped through `curl`) and fetches the repo
tarball from `codeload.github.com` into a temp dir before installing — no registry, no clone. Point it
elsewhere with `GAME_LOOP_REPO=owner/repo` / `GAME_LOOP_REF=branch|tag`.
**Best if:** we want a frictionless human install without publishing to a registry.

## Option B — Claude Code plugin (the most idiomatic fit)

game_loop *is* a bundle of Claude Code hooks. Claude Code's plugin system distributes exactly that — hooks
plus commands/skills — via a marketplace:

```
/plugin marketplace add SupposedlySam/game_loop
/plugin install game_loop
```

The plugin would ship the `game_loop` CLI and register the hooks; the per-project `.game_loop/state.json`
and `config.json` stay local (created by a `/game_loop init` command or first run). This is the natural
channel for the **agent-first** audience — it's how Claude Code users already discover and install
tooling.
**Best if:** the primary users are Claude Code sessions/humans in Claude Code. Requires restructuring
into the plugin layout (`.claude-plugin/`, `hooks/`, `commands/`) and hosting a marketplace manifest.

## Option C — PyPI + `pipx`

Package the CLI so it installs globally:

```bash
pipx install game-loop
game_loop init       # scaffolds .game_loop/ and wires the hooks into this project
```

The bash write-guard ships as package data; `game_loop init` writes it into the project's `.game_loop/bin/`.
Gives a clean global `game_loop` command and standard upgrades (`pipx upgrade`).
**Best if:** we want a "real," versioned CLI with a familiar install path for the general dev audience.
Requires a `pyproject.toml`, an entry point, bundling the non-Python files, and release plumbing.

## Not a fit

- **npm / Homebrew core** — game_loop isn't JS, and it isn't a standalone binary; a Homebrew *tap* could
  wrap Option C but adds a channel without adding reach.

## Recommendation

1. **Now:** ship on GitHub as-is and dogfood with real projects (pebble, gents). ✅
2. **Quick win:** the **Option A** `curl | bash` installer. ✅ shipped — `install.sh` self-fetches.
3. **Then, pick the primary audience:**
   - agent-first / Claude Code users → **Option B (plugin)**;
   - general CLI users → **Option C (PyPI/pipx)**.

They aren't mutually exclusive — GitHub + a plugin, or GitHub + PyPI, are both coherent. The open
question is which *second* channel is worth the maintenance, and that depends on who we expect to use
it.
