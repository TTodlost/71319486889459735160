import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import build_pages


class StaticPackageTests(unittest.TestCase):
    def test_only_public_assets_and_verified_fields_are_packaged(self):
        data = {'meta': {'validation': {'files': 1}, 'artifactId': 7},
                'matches': [{'teams': [{'champions': [{'items': [0] * 42, 'lastBuys': ['private-path-fixture'],
                                                       'handle': 1, 'itemsBought': [1]}]}]}]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'web').mkdir()
            for name in build_pages.ASSETS:
                (root / 'web' / name).write_text(name, encoding='utf-8')
            with patch.object(build_pages, 'ROOT', root):
                report = build_pages.publish(data, root / 'output')
            self.assertEqual(report['games'], 1)
            self.assertEqual({p.name for p in (root / 'output').iterdir()}, {*build_pages.ASSETS, 'data.json', '.nojekyll'})
            content = json.loads((root / 'output/data.json').read_text())
            champion = content['matches'][0]['teams'][0]['champions'][0]
            self.assertEqual(set(champion), {'items'})
            self.assertIn('lastBuys', data['matches'][0]['teams'][0]['champions'][0])

    def test_unverified_or_empty_data_is_rejected(self):
        for data in ({'meta': {'validation': {'files': 0}}, 'matches': []},
                     {'meta': {'validation': {'files': 2}}, 'matches': [{}]}):
            with self.assertRaisesRegex(ValueError, 'fully verified'):
                build_pages.publish(data, Path('unused'))


if __name__ == '__main__':
    unittest.main()
