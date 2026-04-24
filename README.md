# autoclaude-cli

Local runner for [AutoClaude](https://github.com/grezy-software/grezy). Executes orchestration plans handed down from the server using **your own Claude Max/Pro subscription**. The server never pays for tokens.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.com/claude-code) CLI on `$PATH` (`claude --version` works) and a Max or Pro subscription signed in.
- [`gh`](https://cli.github.com) CLI signed in with access to the target repo.
- Git.

## Install

With [`uv`](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install autoclaude-cli
```

Or with [`pipx`](https://pipx.pypa.io):

```bash
pipx install autoclaude-cli
```

Don't have `uv` yet? Install it first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # macOS / Linux
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
```

During development, install from source:

```bash
uv tool install --force .      # or: pipx install --force ./
```

## Quickstart

```bash
# 1. Authenticate once. Defaults to https://app.grezy.com. The CLI asks
#    before opening the API-key page in your browser.
autoclaude login
autoclaude login --url localhost:3001        # point at a local frontend

# 2. Verify everything is wired up.
autoclaude diag

# 3. Fire a tick. The server picks the next Job and plan.
autoclaude tick
```

## Profiles

`autoclaude` supports multiple named profiles (stored in `~/.config/autoclaude/config.toml`). Each profile holds one URL, its API key, and an optional repo checkout.

```bash
autoclaude login                                        # default profile -> prod
autoclaude --profile staging login --url https://stage.example.com
autoclaude --profile staging tick
AUTOCLAUDE_PROFILE=staging autoclaude tick
```

`--url` accepts `localhost:3001` (http is assumed) or a full `https://…` URL. Override at runtime with `AUTOCLAUDE_URL` / `AUTOCLAUDE_API_KEY`.

## How it works

1. CLI mirrors the source repo into `$AUTOCLAUDE_HOME/repos/<slug>/` (defaults to `~/.autoclaude/repos/<slug>/`); subsequent ticks just fetch.
2. CLI fetches the current plan from `GET /api/ac/runner/context/`.
3. After opening the tick, CLI creates a dedicated git worktree at `$AUTOCLAUDE_HOME/worktrees/<slug>/<tick_id>/` on branch `autoclaude/<slug>/tick-<tick_id>`.
4. For each step in the plan:
   - Spawns `claude -p "<prompt>"` inside the worktree. The user's checkout is never modified.
5. Closes the tick with the outcome and cost report, removes the worktree, and keeps the branch so the changes remain inspectable.

Override the workspace root with the `AUTOCLAUDE_HOME` environment variable.

## License

MIT. See `LICENSE`.
