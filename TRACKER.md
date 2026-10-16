# High Elo tracker

The dashboard is designed for GitHub Pages at
<https://ttodlost.github.io/71319486889459735160/>.
The **High Elo tracker** Actions workflow builds and deploys it after replay archive
runs and whenever tracker code changes. The deployment summary links to the site.

One-time repository setup: open **Settings → Pages → Build and deployment**, choose
**GitHub Actions** as the source, then run **High Elo tracker** in the Actions tab.
The workflow uses the built-in token; no personal token, SSH key, or credentials
are included in the public website.

GitHub Pages serves static files. Python runs in Actions to read the current
leaderboard, fetch each current top-20 team's latest 25 completed ranked series
within 24 hours, and validate replay purchases against official match statistics.
Per-team histories avoid the global archive's unfinished backfill and timestamp
pagination gaps. Series shared by two leaders are downloaded and counted once.
The browser calculates
filterable champion, item, synergy, and matchup statistics from the published data.
The Reload button checks the latest published dataset; archive collection and
deployment timing depend on GitHub Actions scheduling. The page shows the actual
match span, leaderboard snapshot time, collection cap, and build time separately.
Each build queries the current leaderboard and recent histories even when the
archive artifact has not changed. Current replay files and decoded purchases are
cached between builds; old archive progress cannot block fresh statistics.

Replay bundles may be generated asynchronously. The downloader follows the API's
returned job poll URL and retry delay, with at most 12 polls or 60 seconds of
waiting before deferring the download. Pending JSON is never saved as a replay.
If the job URL returns 404, it rechecks the original bundle URL once within the
same retry budget. Invalid responses also share that budget. A persistent HTTP 429
stops the archive pass while retaining its checkpoint and remaining download queue.

## Metrics

- **Win rates → By map** compares champion, item and team results across maps.
  Each cell shows the win rate and wins / appearances from both outcomes.
  Minimum appearances applies separately to each entity on each map; cells
  below it show a dash. Existing cohort, queue, map and team filters apply.
  Select a champion to narrow item samples, or a map heading to focus on it.
  Set the minimum to zero to see small samples.
- Champion win rate includes both outcomes and mirror picks. Minimum appearances
  controls the ranking display and can be set to zero to show every sample.
- **Elo gained** uses actual ranked-series `ratingAfter − ratingBefore`, divided by
  the total maps in that series and summed across selected champion appearances.
  A hero present in all five maps receives the series change once. Heroes share
  their team's Elo movement; this measures association, not individual causal credit.
- Item win rate includes champion appearances that bought or finished with the
  item and always uses both outcomes, even when the build display shows wins only.
- Direct buys, final ownership, and buys in the last 10% of rounds have separate
  rates. Five shards automatically combine into an emblem; this is not another buy.
- Synergy compares a teammate pair's observed win rate with the mean of the two
  heroes' cohort win rates. Negative lift indicates an underperforming pairing.
  Opponent matchups compare observed win rate with the mean of the hero's baseline
  and the complement of its opponent's baseline. Samples and correlations matter;
  these figures do not prove that a pairing or item caused the result.
- Top N means the team's rank on the current published leaderboard snapshot,
  regardless of its rank when the match was played. N is limited to the collected
  top 20. “Both teams” requires two current members; “At least one” counts only
  qualifying teams' observations. Queue filters reflect the available ranked sample.
- This is a capped recent sample, not all games in 24 hours. Teams may have fewer
  than 25 eligible series. Only completed maps with usable replay statistics are
  included; maps from the same team/series are correlated.

## Local checks

Python 3.11+ and Node 22+; no third-party packages:

```sh
python -m unittest discover -s tracker -p 'test_*.py' -v
node tracker/test_static.mjs
python tracker/build_pages.py --outdir .site
python -m http.server 8766 --directory .site
```

To package an already verified dataset without network access, add
`--data path/to/data.json`. Only dashboard assets and public derived data enter the
Pages artifact. A failed refresh/build leaves the previous deployment available.
