"""Prepare hash-verified replay engines from the official cambc 2.2.0 package.

The published CLI contains an XZ-compressed asset TAR. Read only the two portable
WASM engines; do not launch the CLI, unpack SDKs, or publish engine assets.
Official package metadata: https://pypi.org/pypi/cambc/2.2.0/json
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

ENGINE_VERSION = '2.2.0'
DEFAULT_OUTDIR = Path(__file__).resolve().parent / '.cache' / 'engine' / ENGINE_VERSION
BINARY_SHA256 = {
    '90d6ef233efa7054054aa574d1b05ba18cc8c7db62de5a3c1a8a90f0cbf87e42',  # Linux x86_64
    'cec591232fc1625b1f59cefc95104c9e11192e95d9ac926ebe1a704f10009550',  # Windows x86_64
}
ASSETS = {
    'v0_0_1': ('mapeditor/assets/v0_0_1-ctH8uuEb.wasm', 616952,
               'ce302dc6666ae43592f6936e572bc4baca92a345bff13ef7f986609ac666a0dc'),
    'v0_0_2': ('mapeditor/assets/v0_0_2-DFj19m76.wasm', 627412,
               'e123b59d9dea198762ab7a04db9b3cb6d06dfa991088e41c24750fc4cb88b947'),
}


def valid_asset(path, size, digest):
    return (path.is_file() and path.stat().st_size == size
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest)


def extract_assets(binary):
    """Return only known WASM bytes from an independently pinned CLI binary."""
    if hashlib.sha256(binary).hexdigest() not in BINARY_SHA256:
        raise ValueError('Unsupported engine binary; install the official cambc 2.2.0 package.')
    markers = list(re.finditer(rb'CAMBC_ASSET_CACHEassets[0-9a-f]{64}(\xfd7zXZ\x00)', binary))
    if len(markers) != 1:
        raise ValueError('The official engine asset archive was not found.')
    wanted = {values[0]: (version, values[1], values[2]) for version, values in ASSETS.items()}
    found = {}
    try:
        stream = lzma.LZMAFile(io.BytesIO(binary[markers[0].start(1):]))
        with stream, tarfile.open(fileobj=stream, mode='r|') as archive:
            for member in archive:
                if member.name not in wanted:
                    continue
                version, size, digest = wanted[member.name]
                if not member.isfile() or member.size != size or version in found:
                    raise ValueError('Invalid official replay engine asset.')
                contents = archive.extractfile(member).read(size + 1)
                if len(contents) != size or hashlib.sha256(contents).hexdigest() != digest:
                    raise ValueError('Official replay engine asset checksum mismatch.')
                found[version] = contents
                if len(found) == len(wanted):
                    break
    except (tarfile.TarError, lzma.LZMAError, EOFError) as error:
        raise ValueError('Unable to read the official engine asset archive.') from error
    if found.keys() != ASSETS.keys():
        raise ValueError('The official package lacks a required replay engine.')
    return found


def prepare_engine(binary=None, outdir=DEFAULT_OUTDIR):
    outdir = Path(outdir)
    paths = {version: outdir / (version + '.wasm') for version in ASSETS}
    if all(valid_asset(paths[version], values[1], values[2]) for version, values in ASSETS.items()):
        return paths
    binary = binary or shutil.which('cambc')
    if not binary:
        raise ValueError('Install the official cambc 2.2.0 package before preparing replay engines.')
    assets = extract_assets(Path(binary).read_bytes())
    outdir.mkdir(parents=True, exist_ok=True)
    for version, contents in assets.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=outdir, prefix=version + '.', suffix='.part', delete=False) as target:
                temporary = Path(target.name)
                target.write(contents)
            os.replace(temporary, paths[version])
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, help='Path to the official cambc 2.2.0 executable')
    parser.add_argument('--outdir', type=Path, default=DEFAULT_OUTDIR)
    args = parser.parse_args()
    paths = prepare_engine(args.binary, args.outdir)
    print(json.dumps({'engineVersion': ENGINE_VERSION,
                      'assets': {version: str(path) for version, path in paths.items()}}))


if __name__ == '__main__':
    main()
