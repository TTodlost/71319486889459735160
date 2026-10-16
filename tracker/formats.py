"""Lossless map26a/repl26a codecs recovered from cambc 2.1.1.

The wire format uses little-endian fixed-width fields. Numeric payload words are
kept as unsigned bit patterns; coordinate views reinterpret those as i32.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct
import zipfile


MAP_MAGIC = b"w62pam\0\0"
REPLAY_MAGIC = b"a62per\0\0"
SUPPORTED_VERSION = (0, 0, 1)
SUPPORTED_REPLAY_VERSIONS = frozenset((SUPPORTED_VERSION, (0, 0, 2)))
TERRAIN_WIRE_NAMES = ("bushes", "walls", "holes", "altars")


class FormatError(ValueError):
    """Malformed, truncated, or unsupported original-engine file."""

    def __init__(self, message: str, offset: int = 0):
        self.message = message
        self.offset = offset
        super().__init__(f"{message} (offset {offset})")


class MapValidationError(FormatError):
    """Exact InvalidMapFile status from the original semantic validator."""
    code = 7
    native_code = 7


class _Reader:
    def __init__(self, data: bytes, offset: int = 0):
        if not 0 <= offset <= len(data):
            raise FormatError("Invalid initial offset", offset)
        self.data = data
        self.pos = offset

    def take(self, size: int, label: str = "field") -> bytes:
        start = self.pos
        if size < 0 or size > len(self.data) - start:
            raise FormatError(f"Truncated {label}", start)
        self.pos += size
        return self.data[start:self.pos]

    def u8(self, label: str = "u8") -> int:
        return self.take(1, label)[0]

    def u16(self, label: str = "u16") -> int:
        return struct.unpack("<H", self.take(2, label))[0]

    def u32(self, label: str = "u32") -> int:
        return struct.unpack("<I", self.take(4, label))[0]

    def remaining(self) -> int:
        return len(self.data) - self.pos


def _signed32(word: int) -> int:
    return word if word < 0x80000000 else word - 0x100000000


# 0x140437F50: switch at 0x140438216 and subtype switch 0x14043832E.
_CHAMPION_WIRE = {
    0: (("priority",), "I", "turn_priority"),
    1: (("x", "y"), "II", "move"),
    2: (("target_tag", "target_index", "attack_index"), "BII", "attack"),
    3: (("ability_index", "target_tag", "target_index", "x", "y"), "IBIII", "cast"),
    4: ((), "", "recall"),
    5: (("x", "y"), "II", "submit_gems"),
    6: (("item_kind", "count"), "II", "buy_item"),
    7: ((), "", "cancel_channel"),
    8: (("consumable_kind", "count"), "II", "buy_consumable"),
    9: (("consumable_kind", "x", "y"), "BII", "use_consumable"),
    10: (("x", "y"), "II", "place_ward"),
    11: (("item_kind",), "I", "sell_item"),
}
_SANDBOX_EDIT_NAMES = (
    "target_tag", "target_index", "value_tag", "value_index",
    "word_0", "word_1", "word_2", "word_3", "float32_bits",
)


@dataclass(frozen=True)
class Action:
    tag: int
    subtype: int | None = None
    values: tuple[int, ...] = ()
    offset: int = field(default=0, compare=False)
    # v2 adds a draft byte. Keep it opaque until its gameplay meaning is known.
    draft_metadata: int | None = None

    @property
    def kind(self) -> str:
        if self.tag == 0 and self.subtype in _CHAMPION_WIRE:
            return _CHAMPION_WIRE[self.subtype][2]
        if self.tag == 2:
            return "teleport" if self.subtype == 0 else "sandbox_edit"
        return {3: "end_turn", 4: "draft", 5: "resign"}.get(self.tag, "invalid")

    @property
    def fields(self) -> dict[str, int]:
        if self.tag == 0 and self.subtype in _CHAMPION_WIRE:
            names = _CHAMPION_WIRE[self.subtype][0]
        elif self.tag == 2:
            names = ("target_tag", "x", "y") if self.subtype == 0 else _SANDBOX_EDIT_NAMES
        elif self.tag == 3:
            names = ("reason",)
        elif self.tag == 4:
            names = ("design_index",)
        else:
            names = ()
        out = dict(zip(names, self.values))
        for key in ("x", "y", "priority"):
            if key in out:
                out[key] = _signed32(out[key])
        return out


def _read_action(reader: _Reader, version: tuple[int, int, int] = SUPPORTED_VERSION) -> Action:
    start = reader.pos
    tag = reader.u8("replay action tag")
    subtype = None
    if tag == 0:
        subtype = reader.u8("replay action subtype")
        if subtype not in _CHAMPION_WIRE:
            raise FormatError("Invalid replay champion action subtype", start + 1)
        fmt = _CHAMPION_WIRE[subtype][1]
    elif tag == 2:
        subtype = reader.u8("replay sandbox subtype")
        if subtype >= 9:
            raise FormatError("Invalid replay sandbox action subtype", start + 1)
        fmt = "BII" if subtype == 0 else "BIBIIIIII"
    elif tag == 3:
        reason = reader.u8("replay end-turn reason")
        # 2.2.0 reader 0x140456610 allows reason 2 only for the v2 body.
        if reason >= (3 if version == (0, 0, 2) else 2):
            raise FormatError("Invalid replay end-turn reason", start + 1)
        return Action(tag, None, (reason,), start)
    elif tag == 4:
        subtype = reader.u8("draft selection subtype")
        design_index = reader.u32("draft selection")
        # 2.2.0 reader 0x1404566E2-0x140456703: v2 appends one byte,
        # accepting precisely 0, 1, 2 or 255; legacy records have no byte.
        metadata = reader.u8("draft metadata") if version == (0, 0, 2) else None
        if metadata is not None and metadata not in (0, 1, 2, 255):
            raise FormatError("Invalid draft metadata", reader.pos - 1)
        if subtype != 0 or design_index >= 12:
            raise FormatError("Invalid draft selection", start + 1)
        return Action(tag, subtype, (design_index,), start, metadata)
    elif tag == 5:
        return Action(tag, None, (), start)
    else:
        raise FormatError("Invalid replay action tag", start)
    values = struct.unpack("<" + fmt, reader.take(struct.calcsize("<" + fmt), "replay " + ("sandbox action" if tag == 2 else _CHAMPION_WIRE[subtype][2]))) if fmt else ()
    return Action(tag, subtype, tuple(values), start)


def parse_actions(data: bytes, count: int, offset: int = 0, *, allow_trailing: bool = False,
                  version: tuple[int, int, int] = SUPPORTED_VERSION) -> tuple[Action, ...]:
    """Decode exactly count records; reject unknown tags and incomplete payloads."""
    reader = _Reader(data, offset)
    if version not in SUPPORTED_REPLAY_VERSIONS:
        raise FormatError("Unsupported replay file version", offset)
    if count < 0 or count > reader.remaining():
        raise FormatError("Invalid replay record count", offset)
    actions = tuple(_read_action(reader, version) for _ in range(count))
    if not allow_trailing and reader.remaining():
        raise FormatError("Malformed replay actions: trailing bytes", reader.pos)
    return actions


def dumps_action(action: Action, *, version: tuple[int, int, int] = SUPPORTED_VERSION) -> bytes:
    """Serialize one validated action with precisely the original wire widths."""
    tag, subtype, values = action.tag, action.subtype, action.values
    if version not in SUPPORTED_REPLAY_VERSIONS:
        raise FormatError("Unsupported replay file version")
    if action.draft_metadata is not None and (tag != 4 or version != (0, 0, 2)):
        raise FormatError("Draft metadata requires a v2 draft action")
    if tag == 0 and subtype in _CHAMPION_WIRE:
        prefix = bytes((tag, subtype))
        fmt = _CHAMPION_WIRE[subtype][1]
    elif tag == 2 and isinstance(subtype, int) and 0 <= subtype < 9:
        prefix = bytes((tag, subtype))
        fmt = "BII" if subtype == 0 else "BIBIIIIII"
    elif tag == 3 and subtype is None and len(values) == 1 and values[0] in (range(3) if version == (0, 0, 2) else range(2)):
        return bytes((tag, values[0]))
    elif tag == 4 and subtype == 0 and len(values) == 1 and 0 <= values[0] < 12:
        suffix = b""
        if version == (0, 0, 2):
            if action.draft_metadata not in (0, 1, 2, 255):
                raise FormatError("Invalid draft metadata")
            suffix = bytes((action.draft_metadata,))
        return bytes((tag, subtype)) + struct.pack("<I", values[0]) + suffix
    elif tag == 5 and subtype is None and not values:
        return bytes((tag,))
    else:
        raise FormatError("Invalid action")
    try:
        return prefix + struct.pack("<" + fmt, *values)
    except (struct.error, OverflowError, TypeError) as exc:
        raise FormatError(f"Invalid action payload: {exc}") from exc


def dumps_actions(actions: tuple[Action, ...] | list[Action], *,
                  version: tuple[int, int, int] = SUPPORTED_VERSION) -> bytes:
    return b"".join(dumps_action(action, version=version) for action in actions)

@dataclass(frozen=True)
class BitMask:
    """Native row-major 8-by-8 block mask; each block is one u64."""
    width: int
    height: int
    bits: int = 0
    runs: tuple[int, ...] = field(default=(), compare=False, repr=False)

    @property
    def stride(self) -> int:
        return ((self.width + 7) // 8) * 8

    @property
    def bit_count(self) -> int:
        return ((self.width + 7) // 8) * ((self.height + 7) // 8) * 64

    def _index(self, x: int, y: int) -> int:
        return ((y // 8) * ((self.width + 7) // 8) + x // 8) * 64 + (y % 8) * 8 + x % 8

    def has(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height and bool((self.bits >> self._index(x, y)) & 1)

    @property
    def tiles(self) -> frozenset[tuple[int, int]]:
        return frozenset((x, y) for y in range(self.height) for x in range(self.width) if self.has(x, y))

    def rows(self) -> tuple[int, ...]:
        return tuple(sum(1 << x for x in range(self.width) if self.has(x, y)) for y in range(self.height))

    @classmethod
    def from_tiles(cls, width: int, height: int, tiles) -> BitMask:
        blocks_x = (width + 7) // 8
        bits = 0
        for x, y in tiles:
            if not 0 <= x < width or not 0 <= y < height:
                raise FormatError("Bit mask tile outside bounds")
            bits |= 1 << (((y // 8) * blocks_x + x // 8) * 64 + (y % 8) * 8 + x % 8)
        return cls(width, height, bits)


@dataclass(frozen=True)
class CampSpawn:
    camp_type: int
    x: int
    y: int


@dataclass(frozen=True)
class MapData:
    width: int
    height: int
    symmetry: int
    champions_per_team: int
    terrain: tuple[BitMask, ...]
    bush_clusters: tuple[int, ...]
    cluster_masks: tuple[BitMask, ...]
    team_a_spawn: tuple[int, int, int, int]
    camps: tuple[CampSpawn, ...]
    palette: tuple[int, ...] = (0,) * 15
    display_flag: int = 0
    version: tuple[int, int, int] = SUPPORTED_VERSION
    bush_runs: tuple[tuple[int, int], ...] = field(default=(), compare=False, repr=False)
    consumed: int = field(default=0, compare=False)
    offset: int = field(default=0, compare=False)

    @property
    def walls(self) -> BitMask:
        return self.terrain[1]

    @property
    def bushes(self) -> BitMask:
        return self.terrain[0]

    @property
    def holes(self) -> BitMask:
        return self.terrain[2]

    @property
    def altars(self) -> BitMask:
        return self.terrain[3]


@dataclass(frozen=True)
class Replay:
    map: MapData
    actions: tuple[Action, ...]
    file_version: tuple[int, int, int] = SUPPORTED_VERSION
    engine_version: tuple[int, int, int] = SUPPORTED_VERSION


def _read_bitmask(reader: _Reader, width: int, height: int) -> BitMask:
    # 0x14042F8E0: starts with a zero run; each u16 length toggles the bit.
    total = ((width + 7) // 8) * ((height + 7) // 8) * 64
    position = 0
    bit = 0
    bits = 0
    runs = []
    while position < total:
        run = reader.u16("bit mask RLE stream")
        if run > total - position:
            raise FormatError("RLE stream was longer than expected", reader.pos - 2)
        runs.append(run)
        if bit and run:
            bits |= ((1 << run) - 1) << position
        position += run
        bit ^= 1
    return BitMask(width, height, bits, tuple(runs))


def _read_bush_clusters(reader: _Reader, width: int, height: int):
    # 0x14042FB90: each pair is (u16 cluster ID, u16 run length).
    total = width * height
    clusters = []
    runs = []
    while len(clusters) < total:
        cluster_id = reader.u16("bush cluster RLE ID")
        run = reader.u16("bush cluster RLE length")
        if not run:
            raise FormatError("Bush cluster RLE run cannot have length 0", reader.pos - 2)
        if run > total - len(clusters):
            raise FormatError("Bush cluster RLE stream was longer than expected", reader.pos - 2)
        runs.append((cluster_id, run))
        clusters.extend([cluster_id] * run)
    return tuple(clusters), tuple(runs)


def _read_map(reader: _Reader) -> MapData:
    start = reader.pos
    if reader.take(8, "map file header") != MAP_MAGIC:
        raise FormatError("Map magic number did not match", start)
    version = tuple(reader.take(3, "map version"))
    if version != SUPPORTED_VERSION:
        raise FormatError("Unsupported map file version", start + 8)
    # 0x14042F020 checks >=69 bytes before reading this fixed prefix.
    if reader.remaining() < 69:
        raise FormatError("Expected file size was too small", reader.pos)
    palette = tuple(reader.u32("map palette") for _ in range(12))
    display_flag = reader.u8("map display flag")
    palette += tuple(reader.u32("map palette") for _ in range(3))
    symmetry = reader.u8("map symmetry")
    if symmetry >= 3:
        raise FormatError("Invalid symmetry type", reader.pos - 1)
    champions_per_team = reader.u8("champions per team")
    if not 1 <= champions_per_team <= 5:
        raise FormatError("Invalid number of champs per team", reader.pos - 1)
    width = reader.u16("map width")
    height = reader.u16("map height")
    if not width or not height:
        raise FormatError("Map width and height must be non-zero", reader.pos - 4)
    extra_masks = reader.u16("bush cluster mask count")
    terrain = tuple(_read_bitmask(reader, width, height) for _ in range(4))
    bush_clusters, bush_runs = _read_bush_clusters(reader, width, height)
    cluster_masks = tuple(_read_bitmask(reader, width, height) for _ in range(extra_masks))
    bbox_offset = reader.pos
    team_a_spawn = tuple(reader.u16("spawn bounding box") for _ in range(4))
    xmin, ymin, xmax, ymax = team_a_spawn
    if xmin >= xmax or ymin >= ymax:
        raise FormatError("Invalid spawn bounding box", bbox_offset)
    area = (xmax - xmin) * (ymax - ymin)
    if not 9 <= area <= 16:
        raise FormatError("Spawn region must have 9 to 16 units", bbox_offset)
    count = reader.u8("camp count")
    if count > 250:
        raise FormatError("Map camp limit is 250 before mirroring", reader.pos - 1)
    # Native preflight check 0x14042FEB4, before reading any camp.
    if reader.remaining() < count * 3:
        raise FormatError("Camp buffer was shorter than expected", reader.pos)
    camps = []
    for _ in range(count):
        camp_type = reader.u8("camp type")
        if camp_type >= 5:
            raise FormatError("Invalid camp type", reader.pos - 1)
        camps.append(CampSpawn(camp_type, reader.u8("camp x"), reader.u8("camp y")))
    return MapData(width, height, symmetry, champions_per_team, terrain,
                   bush_clusters, cluster_masks, team_a_spawn, tuple(camps),
                   palette, display_flag, version, bush_runs,
                   reader.pos - start, start)


def parse_map(data: bytes, offset: int = 0, *, allow_trailing: bool = False, validate: bool = True) -> MapData:
    """Parse map0.0.1 exactly; consumed is its length when embedded."""
    reader = _Reader(data, offset)
    result = _read_map(reader)
    if validate:
        validate_map(result)
    if not allow_trailing and reader.remaining():
        raise FormatError("Trailing map bytes", reader.pos)
    return result


def load_map(path: str | Path) -> MapData:
    return parse_map(Path(path).read_bytes())

def _reflect_map_tile(map_data: MapData, x: int, y: int) -> tuple[int, int]:
    if map_data.symmetry == 0:
        return map_data.width - 1 - x, y
    if map_data.symmetry == 1:
        return x, map_data.height - 1 - y
    return map_data.width - 1 - x, map_data.height - 1 - y


def _validate_map_terrain(map_data: MapData) -> None:
    """0x140447C40 through terrain/camp traversal, in native guard order."""
    width, height = map_data.width, map_data.height
    xmin, ymin, xmax, ymax = map_data.team_a_spawn
    if xmin < 0 or ymin < 0 or xmax > width or ymax > height:
        raise MapValidationError("Spawn regions must be within the map bounds.", map_data.offset)
    walls, holes, altars = map_data.walls.tiles, map_data.holes.tiles, map_data.altars.tiles
    blocked = walls | holes | altars
    # 0x1404487C0 checks both the serialized rectangle and its reflection.
    for y in range(ymin, ymax):
        for x in range(xmin, xmax):
            if (x, y) in blocked or _reflect_map_tile(map_data, x, y) in blocked:
                raise MapValidationError("Spawn regions must be clear of obstructions.", map_data.offset)
    bushes = map_data.bushes.tiles
    # 0x140448960 intentionally compares only bushes and walls.
    for tiles in (bushes, walls):
        if any(_reflect_map_tile(map_data, x, y) not in tiles for x, y in tiles):
            raise MapValidationError("Map must be symmetrical.", map_data.offset)
    seen = set()
    for tiles in (bushes, walls, holes, altars):
        if seen & tiles:
            raise MapValidationError("Bushes, walls, holes and altars cannot overlap.", map_data.offset)
        seen |= tiles
    for camp in map_data.camps:
        if not 0 <= camp.x < width or not 0 <= camp.y < height:
            raise MapValidationError("Camp positions must be within the map bounds.", map_data.offset)
        if (camp.x, camp.y) in blocked:
            raise MapValidationError("Camp spawn tiles must be traversable.", map_data.offset)


def _validate_camp_positions(map_data: MapData) -> None:
    # 0x1404455D0: insertion must succeed for both original and reflected
    # camp positions. Even a single camp on a symmetry fixed point is invalid.
    # Group membership/connectivity are maintained by the map-loader builder
    # 0x140444DB0; no user-supplied groups occur in this serialized format.
    positions = set()
    for camp in map_data.camps:
        for position in ((camp.x, camp.y), _reflect_map_tile(map_data, camp.x, camp.y)):
            if position in positions:
                raise MapValidationError("Camp positions overlap or are outside the map.", map_data.offset)
            positions.add(position)


def validate_map(map_data: MapData) -> None:
    """Apply native semantic guards to an already structurally valid map.

    Acceptance follows 0x140447BE0/0x140447C40/0x1404480A0. Serialized files
    derive their camp groups through 0x140444DB0, which maintains membership
    and mirror-aware cardinal connectivity; positions must remain unique.
    """
    if len(map_data.camps) > 250:
        raise MapValidationError("Maps support at most 250 camps and groups before mirroring.", map_data.offset)
    _validate_map_terrain(map_data)
    _validate_camp_positions(map_data)
    _validate_bush_cluster_ids(map_data)


def _validate_bush_cluster_ids(map_data: MapData) -> None:
    """0x1404480A0, preserving row scan and cardinal DFS validation."""
    width, height = map_data.width, map_data.height
    bushes = map_data.bushes.tiles
    visited = set()
    used_ids = set()
    count = len(map_data.cluster_masks)
    for y in range(height):
        for x in range(width):
            tile = x, y
            if tile in visited:
                continue
            cluster_id = map_data.bush_clusters[y * width + x]
            if tile not in bushes:
                if cluster_id:
                    raise MapValidationError("Bush cluster ID should be 0 for non-bush cells.", map_data.offset)
                continue
            if not 0 < cluster_id < count:
                raise MapValidationError("Bush cluster ID was out of range for the given number of clusters.", map_data.offset)
            if cluster_id in used_ids:
                raise MapValidationError("Bush cluster ID is reused by disconnected bush regions.", map_data.offset)
            used_ids.add(cluster_id)
            visited.add(tile)
            stack = [tile]
            while stack:
                tx, ty = stack.pop()
                for nx, ny in ((tx - 1, ty), (tx + 1, ty), (tx, ty - 1), (tx, ty + 1)):
                    neighbor = nx, ny
                    if neighbor not in bushes or neighbor in visited:
                        continue
                    if map_data.bush_clusters[ny * width + nx] != cluster_id:
                        raise MapValidationError("Cardinally connected bush tiles have different cluster IDs.", map_data.offset)
                    visited.add(neighbor)
                    stack.append(neighbor)



def _encode_bitmask(mask: BitMask) -> bytes:
    total = mask.bit_count
    if mask.bits < 0 or mask.bits >> total:
        raise FormatError("Bit mask exceeds padded dimensions")
    if mask.runs:
        pos = 0
        bit = 0
        source_bits = 0
        for run in mask.runs:
            if not 0 <= run <= 65535 or run > total - pos:
                break
            if bit and run:
                source_bits |= ((1 << run) - 1) << pos
            pos += run
            bit ^= 1
        else:
            if pos == total and source_bits == mask.bits:
                return struct.pack("<" + "H" * len(mask.runs), *mask.runs)
    runs = []
    position = 0
    bit = 0
    while position < total:
        shifted = mask.bits >> position
        remaining = total - position
        if bit:
            inverse = shifted ^ ((1 << remaining) - 1)
            run = remaining if not inverse else (inverse & -inverse).bit_length() - 1
        else:
            run = remaining if not shifted else (shifted & -shifted).bit_length() - 1
        position += run
        while run > 65535:
            runs.extend((65535, 0))
            run -= 65535
        runs.append(run)
        bit ^= 1
    return struct.pack("<" + "H" * len(runs), *runs)


def _encode_bush_clusters(map_data: MapData) -> bytes:
    clusters = map_data.bush_clusters
    if len(clusters) != map_data.width * map_data.height:
        raise FormatError("Incorrect bush cluster array length")
    if any(not 0 <= value <= 65535 for value in clusters):
        raise FormatError("Bush cluster ID exceeds u16")
    source_values = tuple(value for value, run in map_data.bush_runs for _ in range(run))
    if map_data.bush_runs and source_values == clusters and all(0 < run <= 65535 for _, run in map_data.bush_runs):
        pairs = map_data.bush_runs
    else:
        pairs = []
        for value in clusters:
            if pairs and pairs[-1][0] == value and pairs[-1][1] < 65535:
                pairs[-1] = (value, pairs[-1][1] + 1)
            else:
                pairs.append((value, 1))
    return b"".join(struct.pack("<HH", value, run) for value, run in pairs)


def dumps_map(map_data: MapData) -> bytes:
    """Encode semantic fields; preserve native RLE segmentation when unchanged."""
    if map_data.version != SUPPORTED_VERSION:
        raise FormatError("Unsupported map file version")
    if not 1 <= map_data.width <= 65535 or not 1 <= map_data.height <= 65535:
        raise FormatError("Invalid map dimensions")
    if len(map_data.palette) != 15 or len(map_data.terrain) != 4:
        raise FormatError("Map requires 15 palette words and 4 terrain masks")
    if map_data.symmetry not in (0, 1, 2) or not 1 <= map_data.champions_per_team <= 5:
        raise FormatError("Invalid map symmetry or champion count")
    if any(mask.width != map_data.width or mask.height != map_data.height for mask in (*map_data.terrain, *map_data.cluster_masks)):
        raise FormatError("Map mask dimensions do not match")
    xmin, ymin, xmax, ymax = map_data.team_a_spawn
    if xmin >= xmax or ymin >= ymax or not 9 <= (xmax - xmin) * (ymax - ymin) <= 16:
        raise FormatError("Invalid spawn bounding box")
    if len(map_data.camps) > 250 or any(not 0 <= camp.camp_type < 5 for camp in map_data.camps):
        raise FormatError("Invalid map camps")
    try:
        result = MAP_MAGIC + bytes(map_data.version)
        result += struct.pack("<12I", *map_data.palette[:12]) + bytes((map_data.display_flag,))
        result += struct.pack("<3I", *map_data.palette[12:])
        result += struct.pack("<BBHHH", map_data.symmetry, map_data.champions_per_team, map_data.width, map_data.height, len(map_data.cluster_masks))
        result += b"".join(_encode_bitmask(mask) for mask in map_data.terrain)
        result += _encode_bush_clusters(map_data)
        result += b"".join(_encode_bitmask(mask) for mask in map_data.cluster_masks)
        result += struct.pack("<4H", *map_data.team_a_spawn)
        result += bytes((len(map_data.camps),))
        result += b"".join(struct.pack("<BBB", camp.camp_type, camp.x, camp.y) for camp in map_data.camps)
        return result
    except (struct.error, OverflowError, TypeError) as exc:
        raise FormatError(f"Invalid map field: {exc}") from exc


def parse_replay(data: bytes) -> Replay:
    reader = _Reader(data)
    if reader.take(8, "replay header") != REPLAY_MAGIC:
        raise FormatError("Replay magic number did not match")
    file_version = tuple(reader.take(3, "replay version"))
    if file_version not in SUPPORTED_REPLAY_VERSIONS:
        raise FormatError("Unsupported replay file version", 8)
    engine_version = tuple(reader.take(3, "replay engine version"))
    if engine_version not in SUPPORTED_REPLAY_VERSIONS:
        raise FormatError("Unsupported replay engine version", 11)
    map_data = _read_map(reader)
    validate_map(map_data)
    count = reader.u32("replay record count")
    actions = parse_actions(data, count, reader.pos, version=file_version)
    return Replay(map_data, actions, file_version, engine_version)


def load_replay(path: str | Path) -> Replay:
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            try:
                data = archive.read("replay.repl26a")
            except KeyError as exc:
                raise FormatError("Archive has no replay.repl26a entry") from exc
    else:
        data = path.read_bytes()
    return parse_replay(data)


def dumps_replay(replay: Replay) -> bytes:
    if replay.file_version not in SUPPORTED_REPLAY_VERSIONS or replay.engine_version not in SUPPORTED_REPLAY_VERSIONS:
        raise FormatError("Unsupported replay version")
    return (REPLAY_MAGIC + bytes(replay.file_version) + bytes(replay.engine_version)
            + dumps_map(replay.map) + struct.pack("<I", len(replay.actions))
            + dumps_actions(replay.actions, version=replay.file_version))


