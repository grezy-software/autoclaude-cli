# [2.3.0](https://github.com/grezy-software/autoclaude-cli/compare/v2.2.0...v2.3.0) (2026-04-29)


### Features

* **claude_proc:** stream JSONL events to logger in real time ([f419126](https://github.com/grezy-software/autoclaude-cli/commit/f41912676f9b209328948651ba6a92a3ae7972a9))

# [2.2.0](https://github.com/grezy-software/autoclaude-cli/compare/v2.1.0...v2.2.0) (2026-04-29)


### Features

* **cli:** show status for every profile ([e847801](https://github.com/grezy-software/autoclaude-cli/commit/e8478016aa33c8f986a0b0749e926dfcd70cb044))

# [2.1.0](https://github.com/grezy-software/autoclaude-cli/compare/v2.0.0...v2.1.0) (2026-04-29)


### Features

* **logger:** tag log lines with active profile ([ceab4f6](https://github.com/grezy-software/autoclaude-cli/commit/ceab4f63a97d1f540321f67dc75d75fa6906b654))

# [2.0.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.21.5...v2.0.0) (2026-04-29)


### Bug Fixes

* **workspace:** drop stale branch before recreating tick worktree ([1c71932](https://github.com/grezy-software/autoclaude-cli/commit/1c719320677683eec2d8cefaea2c037d756ed77a))


### Features

* **cli:** tick all profiles sequentially by default ([697c485](https://github.com/grezy-software/autoclaude-cli/commit/697c4859b1f9e6d2be20435537c5f2915f83ce4c))


### BREAKING CHANGES

* **cli:** `install_service`, `install_all`, `play_scheduler`,
and the platform-specific install helpers no longer take a profile
argument. The `use` and `switch` CLI commands are gone.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## [1.21.5](https://github.com/grezy-software/autoclaude-cli/compare/v1.21.4...v1.21.5) (2026-04-29)


### Bug Fixes

* **gh:** permission mode to bypassPermission by default ([6547819](https://github.com/grezy-software/autoclaude-cli/commit/6547819c46610284e6dbca70c6b7c17c80d32a8e))

## [1.21.4](https://github.com/grezy-software/autoclaude-cli/compare/v1.21.3...v1.21.4) (2026-04-29)


### Bug Fixes

* **runner:** retry close_step on transient ApiError ([8504550](https://github.com/grezy-software/autoclaude-cli/commit/8504550c2cc27fdd3783314374dba20d3472831c))

## [1.21.3](https://github.com/grezy-software/autoclaude-cli/compare/v1.21.2...v1.21.3) (2026-04-29)


### Bug Fixes

* **cleanup:** skip branch_push and pr_open when no commits ahead of base ([2d52fc0](https://github.com/grezy-software/autoclaude-cli/commit/2d52fc0cb6d42c5057351a1a2df5b1709e63f13e))

## [1.21.2](https://github.com/grezy-software/autoclaude-cli/compare/v1.21.1...v1.21.2) (2026-04-29)


### Bug Fixes

* **steps:** no push if no code ([2f6ce77](https://github.com/grezy-software/autoclaude-cli/commit/2f6ce777081b6ac2dbcd20ba9616fbfdaddfae6c))

## [1.21.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.21.0...v1.21.1) (2026-04-29)


### Bug Fixes

* **url:** autoclaude.grezy.org ([9e8fa27](https://github.com/grezy-software/autoclaude-cli/commit/9e8fa277f47ea33be6b403db3008f8b88c464bd5))

# [1.21.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.20.1...v1.21.0) (2026-04-28)


### Features

* **gh:** add pr_create + pr_merge helpers ([c79b1c9](https://github.com/grezy-software/autoclaude-cli/commit/c79b1c9b8148f8278a95d6e866f614fccf4a460a))
* **runner:** drop FileLock, handle server 409 on tick_open ([7b84174](https://github.com/grezy-software/autoclaude-cli/commit/7b84174473f2af0962ff671d1decf0a542c83642))

## [1.20.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.20.0...v1.20.1) (2026-04-28)


### Bug Fixes

* **stats:** use display_name for custom agent step name ([bd853f2](https://github.com/grezy-software/autoclaude-cli/commit/bd853f202449bbb40f284f565f1ebfaf32e0f8c6))

# [1.20.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.19.0...v1.20.0) (2026-04-28)


### Features

* **tick-archive:** keep tick logs 7 days, harden file lookups ([c03efdd](https://github.com/grezy-software/autoclaude-cli/commit/c03efdd89f0b598a6d04dd7d2e87d44d04488f78))
* **update-check:** surface daemon-recorded version notice in CLI ([e4eba25](https://github.com/grezy-software/autoclaude-cli/commit/e4eba25ae87d02380f3350558371cbe39403ffe3))

# [1.19.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.18.1...v1.19.0) (2026-04-28)


### Features

* **logger:** show timestamps on stdout ([2e7e092](https://github.com/grezy-software/autoclaude-cli/commit/2e7e092f6859237dd0af628495068a1e3ea36f21))

## [1.18.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.18.0...v1.18.1) (2026-04-28)


### Bug Fixes

* **lint:** pass ruff on cli, runner, service_install, tests, usage_capture ([9c75187](https://github.com/grezy-software/autoclaude-cli/commit/9c75187c39804746c64cbf44807b0275045ae90f))

# [1.18.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.17.1...v1.18.0) (2026-04-28)


### Features

* **cli:** add `autoclaude logs` to tail heartbeat/scheduler service logs ([93c7f40](https://github.com/grezy-software/autoclaude-cli/commit/93c7f404acb2b9a7b5eed261342b81b514af66d5))

## [1.17.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.17.0...v1.17.1) (2026-04-28)


### Bug Fixes

* **services:** bake user PATH into launchd/systemd units ([b21c58b](https://github.com/grezy-software/autoclaude-cli/commit/b21c58b2f62d6e39b0728228bf992099852841ad))

# [1.17.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.16.0...v1.17.0) (2026-04-28)


### Features

* **runner:** skip scheduled ticks via server-side interval gate ([4a319df](https://github.com/grezy-software/autoclaude-cli/commit/4a319df6ab40bcd8d73afb22cc331a3f9c61e996))

# [1.16.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.15.0...v1.16.0) (2026-04-28)


### Features

* **services:** split daemon into heartbeat + scheduler with pause/play ([88f40f6](https://github.com/grezy-software/autoclaude-cli/commit/88f40f68072405883ec0a5e68de70162d9568d63))

# [1.15.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.14.0...v1.15.0) (2026-04-28)


### Features

* **runner:** dispatch tools as their own steps after each agent ([630eac4](https://github.com/grezy-software/autoclaude-cli/commit/630eac4e64c48ff405665c191b1f13af35c23bfe))

# [1.14.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.13.1...v1.14.0) (2026-04-28)


### Features

* **runner:** fork tick worktrees from plan.base_branch ([a1d89ee](https://github.com/grezy-software/autoclaude-cli/commit/a1d89ee0425d9adf2b0b414556725ac1816d0f16))

## [1.13.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.13.0...v1.13.1) (2026-04-28)


### Bug Fixes

* **api-client:** handle streaming request body in failure handler ([a0aa622](https://github.com/grezy-software/autoclaude-cli/commit/a0aa6223f0f6732f9fdfda35934fcb8048c4a3ab))

# [1.13.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.12.0...v1.13.0) (2026-04-28)


### Features

* **cli:** add `use` and `profiles` commands ([4ebc6a1](https://github.com/grezy-software/autoclaude-cli/commit/4ebc6a12cc34bf2597b059f0c55b270cdc0014ea))

# [1.12.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.11.0...v1.12.0) (2026-04-27)


### Features

* **daemon:** ship claude rate_limits, add task command, harden replay ([05fd567](https://github.com/grezy-software/autoclaude-cli/commit/05fd567fa86952887cebb9fcce2817a282316743))

# [1.11.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.10.0...v1.11.0) (2026-04-25)


### Features

* **daemon:** background heartbeat process with launchd/systemd installer ([beb7b1e](https://github.com/grezy-software/autoclaude-cli/commit/beb7b1ec5a79ec5717855ea547adcb7c2985d024))

# [1.10.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.9.1...v1.10.0) (2026-04-25)


### Features

* **runner:** push the tick branch to origin in cleanup ([f57e1e7](https://github.com/grezy-software/autoclaude-cli/commit/f57e1e7de7dabd8e734b8dd68d2777059ba32ee3))

## [1.9.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.9.0...v1.9.1) (2026-04-25)


### Bug Fixes

* **workspace:** clone via gh repo clone so git never prompts for HTTPS auth ([9e32672](https://github.com/grezy-software/autoclaude-cli/commit/9e326724c2796ae15e9fc19a0e9c880e1ce24beb))

# [1.9.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.8.2...v1.9.0) (2026-04-25)


### Features

* **runner:** auto-create GitHub repo when project.github_repo is empty ([0fe6e5a](https://github.com/grezy-software/autoclaude-cli/commit/0fe6e5a6041c1a1039cf02d32a2bbedbfdd50852))

## [1.8.2](https://github.com/grezy-software/autoclaude-cli/compare/v1.8.1...v1.8.2) (2026-04-24)


### Bug Fixes

* **heartbeat:** ping backend on a timer so long steps don't go stale ([15e709c](https://github.com/grezy-software/autoclaude-cli/commit/15e709c366b9308952b5c55c872ad2992aaf826c))

## [1.8.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.8.0...v1.8.1) (2026-04-24)


### Bug Fixes

* **workspace:** normalise github_repo to a canonical clone URL ([34473dd](https://github.com/grezy-software/autoclaude-cli/commit/34473ddfe6c37693cff69344224cb3f23a5d9987))

# [1.8.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.7.0...v1.8.0) (2026-04-24)


### Features

* **claude-proc:** pin Opus, detect bail marker, concise step summaries ([d168dd2](https://github.com/grezy-software/autoclaude-cli/commit/d168dd220e46f7d746c35eb8c5d2cd7969acf111))

# [1.7.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.6.0...v1.7.0) (2026-04-24)


### Features

* **workspace:** github remote for gh + file-tree snapshot upload ([4cfb251](https://github.com/grezy-software/autoclaude-cli/commit/4cfb251346ef7179082088b98a579e23b34fcf5c))

# [1.6.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.5.1...v1.6.0) (2026-04-24)


### Features

* **tick-steps:** setup/cleanup lifecycle rows + readable step summary ([b4b3f51](https://github.com/grezy-software/autoclaude-cli/commit/b4b3f51b13aaf9b56db773b6fdae86152cd9e829))

## [1.5.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.5.0...v1.5.1) (2026-04-24)


### Bug Fixes

* **cost:** send raw cost_usd without rounding ([2cf2509](https://github.com/grezy-software/autoclaude-cli/commit/2cf2509d6b56c2d810e65554764f38a6c73fdb8c))

# [1.5.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.4.0...v1.5.0) (2026-04-24)


### Features

* **cli:** add --version/-v and read __version__ from package metadata ([bfc17bb](https://github.com/grezy-software/autoclaude-cli/commit/bfc17bb7aec96a3c14850894b55d4113935972b2))

# [1.4.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.3.0...v1.4.0) (2026-04-24)


### Features

* **gh:** require GitHub CLI for git work and add preflight check ([a9e125a](https://github.com/grezy-software/autoclaude-cli/commit/a9e125a5a2827bd53c640342e02a36174954f30b))

# [1.3.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.2.0...v1.3.0) (2026-04-24)


### Features

* isolate ticks in dedicated clones + worktrees and add self-healing docs protocol ([c108bee](https://github.com/grezy-software/autoclaude-cli/commit/c108beea3e06c53ea613636f44a79489ed273532))

# [1.2.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.1.2...v1.2.0) (2026-04-22)


### Features

* **profiles:** single --url per profile, default hardcoded to prod ([0a94d97](https://github.com/grezy-software/autoclaude-cli/commit/0a94d972496363c395d78ae580d881d1bc9f610d))

## [1.1.2](https://github.com/grezy-software/autoclaude-cli/compare/v1.1.1...v1.1.2) (2026-04-22)


### Bug Fixes

* **login:** open the frontend URL for API keys and confirm first ([c4f5924](https://github.com/grezy-software/autoclaude-cli/commit/c4f592497e6c9bfce7e1057c9ac9597e7d960a21))

## [1.1.1](https://github.com/grezy-software/autoclaude-cli/compare/v1.1.0...v1.1.1) (2026-04-22)


### Bug Fixes

* **cli:** accept --profile before or after the subcommand ([591a1f5](https://github.com/grezy-software/autoclaude-cli/commit/591a1f5d074870c4fabedbf9c6f76e7d3b0939bb))

# [1.1.0](https://github.com/grezy-software/autoclaude-cli/compare/v1.0.0...v1.1.0) (2026-04-22)


### Features

* **docs:** recommend uv for installation ([c4746db](https://github.com/grezy-software/autoclaude-cli/commit/c4746db482ea1482e09189c5c566c2ebecb51a2d))

# 1.0.0 (2026-04-22)


### Bug Fixes

* **ci:** case-insensitive match for semantic-release version line ([f9a731b](https://github.com/grezy-software/autoclaude-cli/commit/f9a731bea921b1d65c9d2977f3bb2353ae68b140))
* **lint:** sort __all__ and drop deprecated ANN101/ANN102 ignores ([48e6807](https://github.com/grezy-software/autoclaude-cli/commit/48e6807f2755abdb4c9b2894a47ce8eeaee710a1))
* rename PyPI distribution to autoclaude-cli ([ef52240](https://github.com/grezy-software/autoclaude-cli/commit/ef52240b2ea7f5347fb78da13865c7fc0e304e98))
