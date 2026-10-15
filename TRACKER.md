# High Elo tracker

The dashboard is designed for GitHub Pages at
<https://ttodlost.github.io/71319486889459735160/>.
The **High Elo tracker** Actions workflow builds and deploys it after replay archive
runs and whenever tracker code changes. The deployment summary links to the site.

One-time repository setup: open **Settings → Pages → Build and deployment**, choose
**GitHub Actions** as the source, then run **High Elo tracker** in the Actions tab.
The workflow uses the built-in token; no personal token, SSH key, or credentials
are included in the public website.

GitHub Pages serves static files. Python runs in Actions to download the newest
verified replay snapshot, repair historical Elo through individual team histories,
and validate purchases against official match statistics. The browser calculates
filterable champion, item, synergy, and matchup statistics from the published data.
The Reload button checks the latest published dataset; archive collection and
deployment timing depend on GitHub Actions scheduling.

## Metrics

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
- Historical ranks cover the observed public team universe. The archive is partial,
  and maps from the same team/series are correlated.

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
