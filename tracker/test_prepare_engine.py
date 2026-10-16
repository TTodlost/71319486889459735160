import hashlib
import io
import lzma
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import prepare_engine


class EnginePreparationTests(unittest.TestCase):
    def fixture(self, *, corrupt=False, link=False, missing=False):
        values = {'v0_0_1': b'\0asm version one', 'v0_0_2': b'\0asm version two'}
        assets = {version: ('mapeditor/assets/' + version + '.wasm', len(data),
                            hashlib.sha256(data).hexdigest()) for version, data in values.items()}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            unrelated = tarfile.TarInfo('../../unexpected')
            unrelated.size = 7
            archive.addfile(unrelated, io.BytesIO(b'ignored'))
            for version, data in values.items():
                if missing and version == 'v0_0_2':
                    continue
                entry = tarfile.TarInfo(assets[version][0])
                entry.size = len(data)
                if link:
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = '/unexpected'
                archive.addfile(entry, io.BytesIO(b'X' * len(data) if corrupt else data))
        binary = b'CLI CAMBC_ASSET_CACHEassets' + b'a' * 64 + lzma.compress(buffer.getvalue()) + b'CLI suffix'
        return binary, assets, values

    def test_only_verified_wasm_files_are_written_without_running_cli(self):
        binary, assets, values = self.fixture()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / 'cambc'
            executable.write_bytes(binary)
            with patch.object(prepare_engine, 'ASSETS', assets), \
                    patch.object(prepare_engine, 'BINARY_SHA256', {hashlib.sha256(binary).hexdigest()}):
                paths = prepare_engine.prepare_engine(executable, root / 'engines')
            self.assertEqual({p.name for p in (root / 'engines').iterdir()}, {'v0_0_1.wasm', 'v0_0_2.wasm'})
            self.assertEqual({version: path.read_bytes() for version, path in paths.items()}, values)
            self.assertFalse((root / 'unexpected').exists())

    def test_verified_cached_assets_do_not_require_cli(self):
        _, assets, values = self.fixture()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version, data in values.items():
                (root / (version + '.wasm')).write_bytes(data)
            with patch.object(prepare_engine, 'ASSETS', assets), patch.object(prepare_engine.shutil, 'which') as which:
                prepare_engine.prepare_engine(outdir=root)
            which.assert_not_called()

    def test_unknown_binary_is_rejected_before_asset_parsing(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported engine binary'):
            prepare_engine.extract_assets(b'untrusted executable')

    def test_invalid_assets_preserve_existing_cache(self):
        for options in ({'corrupt': True}, {'link': True}, {'missing': True}):
            with self.subTest(options=options):
                binary, assets, _ = self.fixture(**options)
                with TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    executable = root / 'cambc'
                    executable.write_bytes(binary)
                    outdir = root / 'engines'
                    outdir.mkdir()
                    previous = outdir / 'v0_0_1.wasm'
                    previous.write_bytes(b'previous cache')
                    with patch.object(prepare_engine, 'ASSETS', assets), \
                            patch.object(prepare_engine, 'BINARY_SHA256', {hashlib.sha256(binary).hexdigest()}):
                        with self.assertRaises(ValueError):
                            prepare_engine.prepare_engine(executable, outdir)
                    self.assertEqual(previous.read_bytes(), b'previous cache')
                    self.assertEqual({path.name for path in outdir.iterdir()}, {'v0_0_1.wasm'})


if __name__ == '__main__':
    unittest.main()
