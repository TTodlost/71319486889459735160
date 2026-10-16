"""Regression coverage for the native v1/v2 replay action layouts."""
import struct
import unittest

import formats


V1 = (0, 0, 1)
V2 = (0, 0, 2)


def envelope(action_bytes, count, version=V1, engine_version=None):
    # The embedded map format remains v1 in current v2 website bundles.
    empty = formats.BitMask(8, 8)
    map_data = formats.MapData(8, 8, 0, 1, (empty,) * 4, (0,) * 64,
                               (), (0, 0, 3, 3), ())
    return (formats.REPLAY_MAGIC + bytes(version) + bytes(engine_version or version)
            + formats.dumps_map(map_data) + struct.pack('<I', count) + action_bytes)


class ReplayVersionTests(unittest.TestCase):
    def test_legacy_actions_and_envelope_roundtrip_unchanged(self):
        wire = bytes.fromhex('040008000000 00061100000003000000 0301 05')
        replay = formats.parse_replay(envelope(wire, 4))
        self.assertEqual(replay.actions[0].values, (8,))
        self.assertIsNone(replay.actions[0].draft_metadata)
        self.assertEqual(replay.actions[1].values, (17, 3))
        self.assertEqual(formats.dumps_replay(replay), envelope(wire, 4))

    def test_v2_draft_metadata_preserves_record_alignment_and_roundtrip(self):
        # Native 2.2.0 reader 0x140456647/0x1404566E2 appends a byte to
        # each v2 draft record; champion action payloads keep their v1 widths.
        wire = bytes.fromhex('04000800000000 04000300000002 '
                             '00061100000003000000 0302 05')
        raw = envelope(wire, 5, V2)
        replay = formats.parse_replay(raw)
        self.assertEqual(replay.file_version, V2)
        self.assertEqual(replay.engine_version, V2)
        self.assertEqual(replay.map.version, V1)
        self.assertEqual([a.draft_metadata for a in replay.actions[:2]], [0, 2])
        self.assertEqual(replay.actions[2].values, (17, 3))
        self.assertEqual(replay.actions[3].values, (2,))
        self.assertEqual(formats.dumps_replay(replay), raw)

    def test_native_draft_metadata_bounds(self):
        # Native guard 0x1404566FB-0x140456703 accepts {0,1,2,255} only.
        prefix = bytes.fromhex('040008000000')
        for metadata in range(256):
            wire = prefix + bytes((metadata,))
            with self.subTest(metadata=metadata):
                if metadata in (0, 1, 2, 255):
                    actions = formats.parse_actions(wire, 1, version=V2)
                    self.assertEqual(actions[0].draft_metadata, metadata)
                    self.assertEqual(formats.dumps_actions(actions, version=V2), wire)
                else:
                    with self.assertRaisesRegex(formats.FormatError, 'draft metadata'):
                        formats.parse_actions(wire, 1, version=V2)

    def test_v2_draft_requires_metadata_and_checks_legacy_design_bounds(self):
        for wire in (bytes.fromhex('040008000000'),
                     bytes.fromhex('04010800000000'),
                     bytes.fromhex('04000c00000000')):
            with self.subTest(wire=wire.hex()):
                with self.assertRaises(formats.FormatError):
                    formats.parse_actions(wire, 1, version=V2)

    def test_v2_draft_truncation_and_trailing_bytes_are_rejected(self):
        wire = bytes.fromhex('04000800000000')
        for cut in range(len(wire)):
            with self.subTest(cut=cut):
                with self.assertRaises(formats.FormatError):
                    formats.parse_actions(wire[:cut], 1, version=V2)
        with self.assertRaisesRegex(formats.FormatError, 'trailing bytes'):
            formats.parse_actions(wire + b'\0', 1, version=V2)

    def test_end_turn_reason_two_is_v2_only(self):
        # Native reader 0x140456610-0x140456630 gates reason 2 on v2.
        self.assertEqual(formats.parse_actions(b'\x03\x02', 1, version=V2)[0].values, (2,))
        with self.assertRaisesRegex(formats.FormatError, 'end-turn reason'):
            formats.parse_actions(b'\x03\x02', 1)
        for version in (V1, V2):
            with self.assertRaisesRegex(formats.FormatError, 'end-turn reason'):
                formats.parse_actions(b'\x03\x03', 1, version=version)

    def test_unknown_versions_remain_rejected(self):
        for unknown in ((0, 0, 0), (0, 0, 3), (1, 0, 2)):
            for version, engine in ((unknown, V2), (V2, unknown)):
                with self.subTest(version=version, engine=engine):
                    with self.assertRaisesRegex(formats.FormatError, 'Unsupported replay'):
                        formats.parse_replay(envelope(b'\x05', 1, version, engine))
            with self.assertRaisesRegex(formats.FormatError, 'Unsupported replay'):
                formats.parse_actions(b'\x05', 1, version=unknown)

    def test_map_version_two_is_not_implicitly_accepted(self):
        raw = bytearray(envelope(b'\x05', 1, V2))
        raw[22:25] = bytes(V2)
        with self.assertRaisesRegex(formats.FormatError, 'Unsupported map'):
            formats.parse_replay(bytes(raw))

    def test_writer_rejects_missing_invalid_or_misplaced_metadata(self):
        for metadata in (None, 3, 254, 256):
            with self.subTest(metadata=metadata):
                action = formats.Action(4, 0, (8,), draft_metadata=metadata)
                with self.assertRaisesRegex(formats.FormatError, 'draft metadata'):
                    formats.dumps_action(action, version=V2)
        for action, version in ((formats.Action(4, 0, (8,), draft_metadata=0), V1),
                                (formats.Action(5, draft_metadata=0), V2)):
            with self.assertRaisesRegex(formats.FormatError, 'Draft metadata'):
                formats.dumps_action(action, version=version)


if __name__ == '__main__':
    unittest.main()
