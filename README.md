# autoclaude-cli

Local runner for [AutoClaude](https://github.com/grezy-software/grezy). Executes orchestration plans handed down from the server using **your own Claude Max/Pro subscription**. The server never pays for tokens.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.com/claude-code) CLI on `$PATH` (`claude --version` works) and a Max or Pro subscription signed in.
- [`gh`](https://cli.github.com) CLI signed in with access to the target repo.
- Git.

## Install

```bash
pipx install autoclaude
```

During development, install from source:

```bash
pipx install --force ./
```

## Quickstart

```bash
# 1. Authenticate once. The CLI opens the API-key page in your browser.
autoclaude login                      # picks the prod profile
autoclaude --profile local login      # point at http://localhost:8000

# 2. Verify everything is wired up.
autoclaude diag

# 3. Install the Claude Code plugins your team's jobs require.
autoclaude skills install

# 4. Fire a tick. The server picks the next Job and plan.
autoclaude tick
```

## Profiles

`autoclaude` supports multiple named profiles (stored in `~/.config/autoclaude/config.toml`). Useful for switching between the hosted product and your local backend.

```bash
autoclaude --profile prod  tick    # https://app.grezy.com
autoclaude --profile local tick    # http://localhost:8000
AUTOCLAUDE_PROFILE=local autoclaude tick
```

Each profile stores its own `api_base`, `api_key`, and `repo_checkout`.

## How it works

1. CLI fetches the current plan from `GET /api/ac/runner/context/`.
2. For each step in the plan:
   - Ensures required Claude Code plugins are installed (`claude plugin install ...`).
   - Spawns `claude -p "<prompt>"` in the repo checkout.
   - Forwards tool callbacks to the server via `/api/ac/tool/<slug>/<action>/`.
3. Closes the tick with the outcome and cost report.

## License

MIT. See `LICENSE`.
