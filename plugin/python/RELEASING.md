# Releasing the Python plugin

The Canopy validator for the Canasino chain (chain 406) runs a Go auto-updater
(`cmd/auto-update/`) that polls this repo's GitHub Releases about every 30 minutes
(plus a random 1-30 minute stagger) and applies new `plugin-python-v*` releases
by itself. There is no approval step and, today, no signature or checksum check.

## The rule: every release tag must be semver-higher than all existing ones

The updater decides with `semver.Compare(candidate, installed) > 0`
(`cmd/auto-update/releaser.go`). Semver compares the major number first, and it
**discards a lower release silently**: no log line, no error. From the outside it
looks identical to "it hasn't seen it yet".

This repo has two tag schemes:

| Scheme | Example | Major |
|---|---|---|
| Old, manual | `plugin-python-v0.5.0` | 0 |
| Canopy date-based | `plugin-python-v2026.277.1` | 2026 |

Since `0 < 2026`, any `v0.x.y` tag loses to `v2026.*` forever. This cost us about a
day in October 2026: `v0.3.0` through `v0.5.0` were published and verified but the
validator (running `v2026.216.1785869497`, from 4 Aug) never applied them, and
`expire_room` kept failing with `fromAny() failed with err: proto: not found`.

**Always use the date-based scheme: `2026.<day-of-year>.<n>`** (n starts at 1 and
increments for several releases on the same day). Never publish `0.x.y` again.

```bash
# day of year, e.g. 277 for 4 Oct
date +%j
gh workflow run release-plugin-python.yml --repo infoboy27/canopy \
  --ref <branch-or-tag-with-the-code> -f tag=2026.277.1
```

The workflow now refuses a tag that does not exceed the highest existing
`plugin-python-v*` tag, so this mistake fails fast instead of publishing a dead
release. (The check compares against existing tags, which approximates the
installed version; if the validator runs something higher than any tag here, the
tag must beat that too.)

## What to check before releasing

1. **Release from the right commit.** `--ref` picks the code. Do not release
   fair-randomness-v2 plugin code before the matching Go node change is deployed:
   the plugin's `begin_block` rejects any block whose `last_block_hash` is not
   32 bytes, so an old node would halt block production on a validator we cannot
   SSH into. Poker/Domino/Roulette-only code is safe (`release/plugin-python-v0.5.0`,
   commit `23f7831b`, is what `2026.277.1` shipped).
2. **Inspect the tarball.** Download `python-plugin.tar.gz` from the release and
   confirm the message types you expect are in `contract/contract.py`.
3. **Confirm it actually deployed.** Do not assume. Submit a transaction of a message
   type that only the new version knows and check that it is included. Rejected
   txs show up under `POST /rpc/v1/query/failed-txs`. Rejection by an old plugin looks like
   `fromAny() failed with err: proto: not found`.
4. **Allow about an hour** for the updater (30 min poll + up to 30 min stagger).

## Related

- Gameserver releases are separate (manual `docker build`, no CI/CD).
- The updater does not verify signatures. Treat anything it applies as trusted
  only as far as this repo's GitHub account is.
