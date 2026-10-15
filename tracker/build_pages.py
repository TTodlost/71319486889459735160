"""Build the public static tracker from a verified replay dataset."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

import collector

ROOT = Path(__file__).resolve().parent
ASSETS = ('index.html', 'style.css', 'app.js', 'stats.js', 'snapshot.js')


def publish(data, outdir):
    matches = data.get('matches')
    validation = data.get('meta', {}).get('validation', {})
    if not isinstance(matches, list) or not matches or validation.get('files') != len(matches):
        raise ValueError('A nonempty, fully verified dataset is required for publication.')
    # Publish only dashboard fields. Decoder caches, archives, credentials,
    # filesystem paths, and runtime state never enter the Pages artifact.
    public = json.loads(json.dumps(data, allow_nan=False))
    for match in public['matches']:
        for team in match['teams']:
            for champion in team['champions']:
                for name in ('lastBuys', 'itemsBought', 'handle', 'gold', 'lateGold'):
                    champion.pop(name, None)
    public['meta']['hosting'] = 'github-pages'
    public['meta']['workflowUrl'] = 'https://github.com/TTodlost/71319486889459735160/actions/workflows/tracker-pages.yml'
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        shutil.copy2(ROOT / 'web' / name, outdir / name)
    collector.atomic_json(outdir / 'data.json', public)
    (outdir / '.nojekyll').write_text('', encoding='utf-8')
    return {'games': len(matches), 'artifactId': public['meta'].get('artifactId'),
            'dataBytes': (outdir / 'data.json').stat().st_size, 'outdir': str(outdir)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outdir', type=Path, default=ROOT.parent / '.site')
    parser.add_argument('--data', type=Path, help='Use an already verified dataset without network calls')
    args = parser.parse_args()
    data = collector.read_json(args.data) if args.data else collector.refresh(lambda message: print(message, flush=True))
    report = publish(data, args.outdir)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
