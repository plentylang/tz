#!/usr/bin/env python3
"""Generate the bundled time-zone database from a pinned IANA tz release.

The script downloads the pinned tzdata and tzcode archives, checks their
SHA-512 digests, builds the reference `zic` and `zdump` from tzcode, compiles
the data with `zic -b slim`, and converts each TZif file into the package's
compact binary format (see src/tzdb.plenty and README.md). It also writes the
conformance vectors in tests/data/ from `zdump`, an independent reader of the
same TZif files.

Usage:
    python3 scripts/generate.py [--archives DIR] [--work DIR]

Needs Python 3.11+ (stdlib only), `make`, and a C compiler. Updating the data
is: change TZDB_VERSION and the digests, run this script, run `plenty test`,
commit, and tag.
"""

import argparse
import calendar
import hashlib
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

TZDB_VERSION = "2026f"
DIGESTS = {
    f"tzdata{TZDB_VERSION}.tar.gz": "26189803c8b3e903dcc467404e2feefb7d190cb1870249a23577ce31e9dcbf187e4704e0b45c8d1366d1c501bb5b53a2e80f8e2828b2163be15e532f6acab52d",
    f"tzcode{TZDB_VERSION}.tar.gz": "eefcea60c37857214e1eba768d1bbd096de2a8f5afc9a776555552922cc174b32892d7442097b699b77efcba4d85649ed7d13e752ffdbcaad1050749c6c7b5aa",
}
RELEASES = "https://data.iana.org/time-zones/releases/"
# The main data plus `backward`, as `make` installs by default. `backzone`
# (pre-1970 detail for merged zones) and `factory` are excluded.
SOURCES = ["africa", "antarctica", "asia", "australasia", "europe",
           "northamerica", "southamerica", "etcetera", "backward"]
FORMAT_VERSION = 1
# Conformance vectors cover transitions in these years. Later years exercise
# the footer rule; the cap keeps the vector file small.
VECTOR_FIRST_YEAR = 1800
VECTOR_LAST_YEAR = 2040

ROOT = Path(__file__).resolve().parent.parent


def fetch(archives: Path) -> None:
    archives.mkdir(parents=True, exist_ok=True)
    for name, digest in DIGESTS.items():
        path = archives / name
        if not path.exists():
            print(f"downloading {RELEASES}{name}", file=sys.stderr)
            with urllib.request.urlopen(RELEASES + name) as response:
                path.write_bytes(response.read())
        actual = hashlib.sha512(path.read_bytes()).hexdigest()
        if actual != digest:
            sys.exit(f"{name}: SHA-512 mismatch\n  expected {digest}\n  actual   {actual}")


def build_tools(archives: Path, work: Path) -> Path:
    source = work / "tzdb"
    source.mkdir()
    for name in DIGESTS:
        with tarfile.open(archives / name) as archive:
            archive.extractall(source, filter="data")
    version = (source / "version").read_text().strip()
    if version != TZDB_VERSION:
        sys.exit(f"archive version {version} != {TZDB_VERSION}")
    subprocess.run(["make", "-s", "zic", "zdump"], cwd=source, check=True,
                   stdout=subprocess.DEVNULL)
    return source


def zone_names(source: Path) -> tuple[list[str], dict[str, str]]:
    zones: list[str] = []
    links: dict[str, str] = {}
    for name in SOURCES:
        for line in (source / name).read_text().splitlines():
            fields = line.split("#", 1)[0].split()
            if not fields:
                continue
            if fields[0] == "Zone":
                zones.append(fields[1])
            elif fields[0] == "Link":
                links[fields[2]] = fields[1]
    for link in list(links):
        target = links[link]
        while target in links:
            target = links[target]
        links[link] = target
    return sorted(zones), links


# --- TZif reading (RFC 8536) ---------------------------------------------

class TZif:
    def __init__(self, data: bytes):
        if data[:4] != b"TZif" or data[4] < ord("2"):
            raise ValueError("not a TZif version 2+ file")
        # Skip the version 1 body.
        counts = struct.unpack(">6l", data[20:44])
        isutc, isstd, leap, timecnt, typecnt, charcnt = counts
        offset = 44 + timecnt * 5 + typecnt * 6 + charcnt + leap * 8 + isstd + isutc
        body = data[offset:]
        if body[:4] != b"TZif":
            raise ValueError("missing version 2 header")
        isutc, isstd, leap, timecnt, typecnt, charcnt = struct.unpack(">6l", body[20:44])
        if leap:
            raise ValueError("leap-second data is not supported")
        at = 44
        self.times = list(struct.unpack(f">{timecnt}q", body[at:at + 8 * timecnt]))
        at += 8 * timecnt
        self.indices = list(body[at:at + timecnt])
        at += timecnt
        self.types = []
        for i in range(typecnt):
            utoff, isdst, abbrind = struct.unpack(">lBB", body[at + 6 * i:at + 6 * i + 6])
            self.types.append((utoff, isdst, abbrind))
        at += 6 * typecnt
        chars = body[at:at + charcnt]
        at += charcnt + isstd + isutc
        self.abbreviations = []
        for utoff, isdst, abbrind in self.types:
            end = chars.index(b"\0", abbrind)
            self.abbreviations.append(chars[abbrind:end].decode("ascii"))
        footer = body[at:]
        if footer[:1] != b"\n" or footer[-1:] != b"\n":
            raise ValueError("malformed footer")
        self.footer = footer[1:-1].decode("ascii")


# --- POSIX TZ strings (with RFC 8536 section 3.3.1 extensions) -------------

class TZString:
    def __init__(self, text: str):
        self.text = text
        self.at = 0
        self.std_abbr = self.abbreviation()
        self.std_offset = -self.offset(24)
        self.dst = None
        if self.at < len(text):
            dst_abbr = self.abbreviation()
            dst_offset = self.std_offset + 3600
            if self.at < len(text) and text[self.at] != ",":
                dst_offset = -self.offset(24)
            self.expect(",")
            start = self.rule()
            self.expect(",")
            end = self.rule()
            self.dst = (dst_abbr, dst_offset, start, end)
        if self.at != len(text):
            raise ValueError(f"trailing text in TZ string {text!r}")

    def expect(self, char: str) -> None:
        if self.text[self.at:self.at + 1] != char:
            raise ValueError(f"expected {char!r} in {self.text!r}")
        self.at += 1

    def abbreviation(self) -> str:
        if self.text[self.at] == "<":
            end = self.text.index(">", self.at)
            name = self.text[self.at + 1:end]
            self.at = end + 1
        else:
            match = re.compile(r"[A-Za-z]{3,}").match(self.text, self.at)
            if not match:
                raise ValueError(f"bad abbreviation in {self.text!r}")
            name = match.group(0)
            self.at = match.end()
        return name

    def offset(self, max_hours: int) -> int:
        match = re.compile(r"([+-]?)(\d{1,3})(?::(\d{2}))?(?::(\d{2}))?").match(self.text, self.at)
        if not match:
            raise ValueError(f"bad offset in {self.text!r}")
        self.at = match.end()
        hours = int(match.group(2))
        if hours > max_hours:
            raise ValueError(f"offset hours out of range in {self.text!r}")
        value = hours * 3600 + int(match.group(3) or 0) * 60 + int(match.group(4) or 0)
        return -value if match.group(1) == "-" else value

    # A rule is (kind, day, month, week, weekday, seconds): kind 0 is Jn,
    # 1 is n, 2 is Mm.w.d.
    def rule(self) -> tuple[int, int, int, int, int, int]:
        text = self.text
        if text[self.at] == "J":
            match = re.compile(r"J(\d{1,3})").match(text, self.at)
            kind, day, month, week, weekday = 0, int(match.group(1)), 0, 0, 0
        elif text[self.at] == "M":
            match = re.compile(r"M(\d{1,2})\.(\d)\.(\d)").match(text, self.at)
            kind, day = 2, 0
            month, week, weekday = (int(g) for g in match.groups())
        else:
            match = re.compile(r"(\d{1,3})").match(text, self.at)
            kind, day, month, week, weekday = 1, int(match.group(1)), 0, 0, 0
        self.at = match.end()
        seconds = 7200
        if self.at < len(text) and text[self.at] == "/":
            self.at += 1
            seconds = self.offset(167)
        return (kind, day, month, week, weekday, seconds)


# --- Encoding --------------------------------------------------------------

class Abbreviations:
    def __init__(self):
        self.ids: dict[str, int] = {}

    def id(self, name: str) -> int:
        if name not in self.ids:
            self.ids[name] = len(self.ids)
        return self.ids[name]


def encode(tzif: TZif, abbreviations: Abbreviations) -> bytes:
    """Encode one zone; the layout is documented in src/tzdb.plenty."""
    out = bytearray([FORMAT_VERSION, len(tzif.types)])
    for (utoff, isdst, _), name in zip(tzif.types, tzif.abbreviations):
        out += struct.pack(">lBH", utoff, isdst, abbreviations.id(name))
    out += struct.pack(">H", len(tzif.times))
    for time in tzif.times:
        if not -(1 << 39) <= time < (1 << 39):
            raise ValueError(f"transition {time} does not fit in 40 bits")
        out += struct.pack(">q", time)[3:]
    out += bytes(tzif.indices)
    if not tzif.footer:
        out.append(0)
        return bytes(out)
    rule = TZString(tzif.footer)
    if rule.dst is None:
        out.append(1)
        out += struct.pack(">lH", rule.std_offset, abbreviations.id(rule.std_abbr))
        return bytes(out)
    dst_abbr, dst_offset, start, end = rule.dst
    out.append(2)
    out += struct.pack(">lH", rule.std_offset, abbreviations.id(rule.std_abbr))
    out += struct.pack(">lH", dst_offset, abbreviations.id(dst_abbr))
    for kind, day, month, week, weekday, seconds in (start, end):
        out += struct.pack(">BHBBBl", kind, day, month, week, weekday, seconds)
    return bytes(out)


def constant(name: str) -> str:
    name = name.replace("+", "_PLUS_")
    name = re.sub(r"-(?=\d)", "_MINUS_", name)
    return "ZONE_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def plenty_string(text: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_/+\-]+", text):
        raise ValueError(f"unexpected character in {text!r}")
    return f'"{text}"'


LOOKUP = """\
def index_field(entry: i64, at: i64) -> i64:
    (i64(NAME_INDEX[4 * entry + at]) << 8) | i64(NAME_INDEX[4 * entry + at + 1])

# NAMES and the names looked up are ASCII, so scalar positions are bytes.
def code(text: &str, at: i64) -> i64:
    match ord(text[at]):
        case Ok(value):
            i64(value)
        case Err(_):
            -1

# -1, 0, or 1 as the name at NAMES[start:stop] sorts before, equal to, or
# after `name`.
def compare_name(start: i64, stop: i64, name: &str) -> i64:
    length = name.byte_len()
    mut at = 0
    while start + at < stop and at < length:
        left = code(&NAMES, start + at)
        right = code(name, at)
        if left != right:
            return -1 if left < right else 1
        at += 1
    # One is a prefix of the other: the shorter sorts first.
    if stop - start == length:
        0
    elif stop - start < length:
        -1
    else:
        1

# The number of the zone a canonical name or link names, or -1.
def find(name: &str) -> i64:
    if not name.isascii():
        return -1
    mut low = 0
    mut high = NAME_COUNT
    while low < high:
        middle = low + (high - low) // 2
        order = compare_name(index_field(middle, 0), index_field(middle + 1, 0) - 1, name)
        if order == 0:
            return index_field(middle, 2)
        if order < 0:
            low = middle + 1
        else:
            high = middle
    -1

# The data of the zone a canonical name or link names.
pub def lookup(name: &str) -> Option[bytes]:
    number = find(name)
    if number < 0:
        Nothing
    else:
        Some(zone_data(number))

# 32-bit FNV-1a.
def fnv(data: &bytes) -> i64:
    mut hash = 2166136261
    for byte in data:
        hash = ((hash ^ i64(*byte)) * 16777619) & 4294967295
    hash

# Whether `data` is the bundled data of the zone `name` names. This reads
# only the name index and one hash, so it links no zone data.
pub def is_bundled(name: &str, data: &bytes) -> bool:
    number = find(name)
    if number < 0:
        return False
    mut stored = 0
    for part in range(4):
        stored = (stored << 8) | i64(ZONE_HASHES[4 * number + part])
    fnv(data) == stored"""


def fnv(data: bytes) -> int:
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def accessor(zone: str) -> str:
    return constant(zone)[len("ZONE_"):].lower()


def write_zones(zones: list[str], links: dict[str, str]) -> None:
    names = [accessor(zone) for zone in zones]
    if len(set(names)) != len(names) or any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names):
        raise ValueError("zone accessor names collide or are invalid")
    aliases: dict[str, list[str]] = {}
    for link, target in links.items():
        aliases.setdefault(target, []).append(link)
    lines = [
        f"# Generated by scripts/generate.py from IANA tzdata {TZDB_VERSION}. Do not edit.",
        "#",
        "# One function per canonical zone, named by lowercasing the zone name and",
        "# writing \"/\" and \"-\" as \"_\", \"+\" as \"_plus_\", and a \"-\" before a",
        "# digit as \"_minus_\": America/New_York is america_new_york() and",
        "# Etc/GMT+5 is etc_gmt_plus_5(). Each function links only its own zone's",
        "# data; tz.zone(name) links every zone. Links (alternative names) have no",
        "# function here: call the canonical zone's, which comments list them under.",
        "import lib",
        "import tzdb",
        "",
        "def bundled(name: str, data: bytes) -> lib.TimeZone:",
        "    match lib.bundled_zone(&name, data):",
        "        case Ok(zone):",
        "            zone",
        "        # Unreachable: each caller passes its own zone's name and data, and",
        "        # tests/test_accessors.plenty checks every function.",
        "        case Err(_):",
        "            lib.utc()",
    ]
    for zone, name in zip(zones, names):
        lines.append("")
        also = sorted(aliases.get(zone, []))
        lines.append(f"# {zone}" + (f"; also {', '.join(also)}" if also else ""))
        lines.append(f"pub def {name}() -> lib.TimeZone:")
        lines.append(f"    bundled({plenty_string(zone)}, tzdb.{constant(zone)})")
    (ROOT / "src" / "zones.plenty").write_text("\n".join(lines) + "\n")
    test = [
        f"# Generated by scripts/generate.py from IANA tzdata {TZDB_VERSION}. Do not edit.",
        "# Every tz.zones function returns its own zone, with the data tz.zone finds.",
        "import std.test",
        "import tz",
        "# A file cannot import both tz and tz.zones by those names, so alias it.",
        "import tz.zones as zones",
        "",
        "def same(made: tz.TimeZone, name: str) -> Result[(), std.test.TestError | tz.TzError]:",
        "    std.test.equal(&made.iana_name(), &Some(name))?",
        "    std.test.equal(&made, &tz.zone(&name)?)",
        "",
        "def test_every_accessor() -> Result[(), std.test.TestError | tz.TzError]:",
    ]
    for zone, name in zip(zones, names):
        test.append(f"    same(zones.{name}(), {plenty_string(zone)})?")
    test.append("    Ok(())")
    (ROOT / "tests" / "test_accessors.plenty").write_text("\n".join(test) + "\n")



def write_module(zones: list[str], links: dict[str, str], abbreviations: Abbreviations) -> None:
    lines = [
        f"# Generated by scripts/generate.py from IANA tzdata {TZDB_VERSION}. Do not edit.",
        "#",
        "# Each zone's data is a separate constant, so a program links only the",
        "# zones its code can reach. `lookup` reaches every zone; each function of",
        "# zones.plenty reaches one.",
        "#",
        "# Zone layout (big-endian), format version 1:",
        "#   u8 format, u8 type count,",
        "#   types: i32 UT offset seconds, u8 is_dst, u16 abbreviation id,",
        "#   u16 transition count, 40-bit signed transition times (UT seconds,",
        "#   ascending),",
        "#   u8 type index per transition,",
        "#   u8 footer kind: 0 none (the last type continues), 1 fixed",
        "#   (i32 offset, u16 abbreviation), 2 daylight saving (standard i32 offset",
        "#   and u16 abbreviation, daylight i32 offset and u16 abbreviation, then",
        "#   start and end rules: u8 kind (0 Jn, 1 n, 2 Mm.w.d), u16 day, u8 month,",
        "#   u8 week, u8 weekday, i32 local seconds).",
        "",
        f'pub const VERSION: str = "{TZDB_VERSION}"',
        "",
    ]
    if len({constant(zone) for zone in zones}) != len(zones):
        raise ValueError("zone constant names collide")
    for zone in zones:
        lines.append(f'pub const {constant(zone)}: bytes = include_bytes("zoneinfo/{zone}")')
    # A match over every name compiles to about 90 KB of comparisons, so
    # names are found by binary search over NAMES with a small index:
    # per name a u16 offset into NAMES and a u16 zone number, then a
    # sentinel offset one past the end.
    names = sorted(set(zones) | set(links))
    text = "\n".join(names)
    if len(text) + 1 >= 1 << 16:
        raise ValueError("names do not fit 16-bit offsets")
    number = {zone: index for index, zone in enumerate(zones)}
    index = bytearray()
    offset = 0
    for name in names:
        index += struct.pack(">HH", offset, number[links.get(name, name)])
        offset += len(name) + 1
    index += struct.pack(">HH", offset, 0)
    (ROOT / "src" / "tzdb.index").write_bytes(bytes(index))
    lines += ["", "# Canonical zones and links, newline-separated and sorted by byte."]
    lines.append('pub const NAMES: str = "' + "\\n".join(plenty_string(name)[1:-1] for name in names) + '"')
    lines.append(f"const NAME_COUNT: i64 = {len(names)}")
    lines.append('const NAME_INDEX: bytes = include_bytes("tzdb.index")')
    hashes = b"".join(struct.pack(">I", fnv((ROOT / "src" / "zoneinfo" / zone).read_bytes())) for zone in zones)
    (ROOT / "src" / "tzdb.hashes").write_bytes(hashes)
    lines.append("# 32-bit FNV-1a of each zone's data, by zone number.")
    lines.append('const ZONE_HASHES: bytes = include_bytes("tzdb.hashes")')
    lines += ["", LOOKUP, "", "def zone_data(number: i64) -> bytes:", "    match number:"]
    for zone in zones:
        lines.append(f"        case {number[zone]}:")
        lines.append(f"            {constant(zone)}")
    lines += ["        case _:", '            b""', ""]
    lines += ["pub def abbreviation(id: u16) -> str:", "    match id:"]
    for name, index in sorted(abbreviations.ids.items(), key=lambda item: item[1]):
        if not re.fullmatch(r"[A-Za-z0-9+\-]+", name):
            raise ValueError(f"unexpected abbreviation {name!r}")
        lines.append(f"        case {index}:")
        lines.append(f'            "{name}"')
    lines += ["        case _:", '            ""', ""]
    (ROOT / "src" / "tzdb.plenty").write_text("\n".join(lines))


def write_vectors(source: Path, compiled: Path, zones: list[str]) -> int:
    """Write (zone, UT second, offset, is_dst, abbreviation) at each transition.

    zdump -v prints each transition as the second before and the second at
    which it takes effect; the test checks both.
    """
    pattern = re.compile(
        r"^(\S+)\s+\w{3} (\w{3}) +(\d+) (\d\d):(\d\d):(\d\d) (-?\d+) UT = .* (\S+) isdst=(\d) gmtoff=(-?\d+)$")
    months = {name: index for index, name in enumerate(calendar.month_abbr) if name}
    out = [f"# zdump -v of IANA tzdata {TZDB_VERSION}, years {VECTOR_FIRST_YEAR}-{VECTOR_LAST_YEAR}.",
           "# Zone lines start with Z; others are: UT seconds, offset, is_dst, abbreviation."]
    count = 0
    # zdump ignores the TZDIR environment variable and resolves relative
    # names against its compiled-in directory (the system's zoneinfo), so
    # pass absolute paths and map them back to names.
    prefix = f"{compiled}/"
    result = subprocess.run(
        [str(source / "zdump"), "-v", "-c", f"{VECTOR_FIRST_YEAR},{VECTOR_LAST_YEAR}",
         *(prefix + zone for zone in zones)],
        check=True, capture_output=True, text=True)
    current = None
    for line in result.stdout.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        path, month, day, hour, minute, second, year, abbr, isdst, gmtoff = match.groups()
        if not VECTOR_FIRST_YEAR <= int(year) < VECTOR_LAST_YEAR:
            continue
        if not path.startswith(prefix):
            raise ValueError(f"unexpected zdump line {line!r}")
        zone = path[len(prefix):]
        seconds = calendar.timegm((int(year), months[month], int(day), int(hour), int(minute), int(second)))
        if zone != current:
            out.append(f"Z {zone}")
            current = zone
        out.append(f"{seconds} {gmtoff} {isdst} {abbr}")
        count += 1
    (ROOT / "tests" / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "tests" / "data" / "zdump.txt").write_text("\n".join(out) + "\n")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archives", type=Path, help="directory caching the downloaded archives")
    parser.add_argument("--work", type=Path, help="scratch directory (default: a temporary directory)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        work = args.work or Path(temporary)
        work.mkdir(parents=True, exist_ok=True)
        archives = args.archives or work / "archives"
        fetch(archives)
        source = build_tools(archives, work)
        compiled = work / "zoneinfo"
        subprocess.run([str(source / "zic"), "-b", "slim", "-d", str(compiled),
                        *(str(source / name) for name in SOURCES)], check=True)
        zones, links = zone_names(source)
        target = ROOT / "src" / "zoneinfo"
        if target.exists():
            shutil.rmtree(target)
        abbreviations = Abbreviations()
        total = 0
        for zone in zones:
            data = encode(TZif((compiled / zone).read_bytes()), abbreviations)
            path = target / zone
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            total += len(data)
        write_module(zones, links, abbreviations)
        write_zones(zones, links)
        vectors = write_vectors(source, compiled, zones)
        shutil.copyfile(source / "LICENSE", ROOT / "LICENSE-tzdata")
        print(f"tzdata {TZDB_VERSION}: {len(zones)} zones, {len(links)} links, "
              f"{len(abbreviations.ids)} abbreviations, {total} bytes, {vectors} vectors",
              file=sys.stderr)


if __name__ == "__main__":
    main()
