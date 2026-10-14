# Battlecode replay archive

Downloads public replays from [2026a.battlecode.co.uk](https://2026a.battlecode.co.uk/game/ladder), with a SQLite catalog and historical team Elo. Python 3.11+, standard library only.

## GitHub Actions

[Replay archive workflow](https://github.com/TTodlost/71319486889459735160/actions/workflows/replay-sync.yml) requests a run every five minutes, at minutes 2, 7, 12, and so on. GitHub schedules can be delayed or skipped; this is a resumable collector, not a precise timer. Runs on the default branch are serialized.

Each run restores the newest verified artifact, performs a bounded fetch, then uploads the entire archive: `catalog.sqlite3`, `.repl26a` bundles, and JSON sidecars. The catalog carries discovery checkpoints, fair retry queues, and rating history between runners. SQLite is backed up consistently and snapshot checksums are verified before files are restored. Two snapshots are retained; old snapshots are deleted only after a new upload is confirmed. A failed or timed-out fetch still saves committed progress when the runner remains available.

The initial push boots from `bootstrap/replay-state.tar.gz`, exported from the existing local collection. Later runs refuse to start an empty catalog if no live snapshot remains. To intentionally recover from the seed, use **Run workflow**, enable **bootstrap**, and understand that progress after the seed will be lost. A live artifact always takes precedence over the seed. No SSH key or custom token is needed by Actions: its built-in token has `actions: write` to download and rotate snapshots.

Fetch limits are in `.github/workflows/replay-sync.yml`: 20 history pages, 40 group details, 100 replay attempts, and a 180-second fetch deadline per run. HTTP timeout is 15 seconds with one retry and a 0.25-second delay between requests. An interrupted pass resumes its saved cursor next time. Discovering all history and downloading it are separate queues, so the first backfill takes multiple runs. Replays are kept when the website removes them; unavailable downloads remain pending for a later retry.

Artifacts expire after 90 days without a replacement and consume the repository's Actions storage allowance. Public-repository schedules also disable after 60 days without repository activity. Download backups periodically; for a large archive or permanent unattended retention, move snapshots to durable object storage. [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [artifact retention](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts), and [Actions storage billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Restore an artifact locally

Open a completed run, download its `battlecode-replay-state-...` artifact, and unzip it to obtain `replay-state.tar.gz`. Restore into an empty output directory:

```sh
python reconstruction/tools/replay_snapshot.py restore --snapshot replay-state.tar.gz --outdir replays/website
python reconstruction/tools/sync_replays.py --outdir replays/website --list --json
```

The snapshot format preserves replay bytes, sidecars, catalog state, and nanosecond file modification times. It excludes locks, partial downloads, and SQLite transient files. Unsafe paths, links, duplicate members, invalid catalogs, and checksum failures are rejected. Restoration never overwrites an existing archive.

## Run locally

```sh
# One resumable pass; omit the limits to work through all available history.
python reconstruction/tools/sync_replays.py --max-pages 20 --max-details 40 --max-downloads 100

# Keep running in the foreground.
python reconstruction/tools/sync_replays.py --watch 300

# Query downloaded replays without using the network.
python reconstruction/tools/sync_replays.py --list --json
python reconstruction/tools/sync_replays.py --list --team ee83f5e5-bcad-4923-86b4-b79ea6083d2e --min-elo 1500

# Fetch a specific match group without a global scan.
python reconstruction/tools/fetch_replays.py --match-group 64013d4f-257c-43c5-bf6a-6b0fd598a7f4
```

Default output is `replays/website`. The sync discovers global match history through the public website API. The single-download tool also accepts teams, ladder discovery, and match-group URLs; use `--help` for options.

## Historical Elo

Each catalog record and replay sidecar identifies the participating teams and keeps the match group's reported `elo_before` and `elo_after`. `elo_at_time` is derived at the individual game's `started_at` from ranked rating events and includes its source and timestamp. It stays unknown while the relevant timeline is incomplete, discontinuous, ambiguous, or outside the verified history window. Current ladder Elo is never substituted for historical Elo. As history fills in, subsequent passes refresh the sidecars.

## Checks

```sh
python -m unittest discover -s reconstruction/tests -p 'test_*.py' -v
```

Tests use offline HTTP fixtures and temporary catalogs, including interrupted pagination, historical rating gaps, fair retries, safe snapshots, and GitHub artifact persistence.
