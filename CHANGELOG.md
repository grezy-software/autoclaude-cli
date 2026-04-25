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
