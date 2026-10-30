# tz

Civil dates and times, IANA time zones, and RFC 3339 / RFC 9557 text for
[Plenty](https://github.com/cjrh/plenty). The API follows Rust's
[jiff](https://docs.rs/jiff): a civil `DateTime` carries no zone, a `Zoned`
is an instant in a zone, and a local time that a transition skips or repeats
is resolved by a `Disambiguation` the caller names.

The IANA time-zone database is compiled into the package. It never reads
`/usr/share/zoneinfo` or `TZ`, so a program gives the same answers on every
machine. The package is Plenty source only.

## Install

Add the dependency to `plenty.toml`, then run `plenty fetch`:

```toml
[dependencies]
tz = { git = "https://github.com/plentylang/tz.git", tag = "v0.1.0" }
```

It needs Plenty 0.7.1 or later.

## Example

[`examples/meeting.plenty`](examples/meeting.plenty):

```python
import tz
from std.fmt import FormatError

def main() -> Result[(), tz.TzError | FormatError | AllocError | IoError]:
    new_york = tz.zone("America/New_York")?
    london = tz.zone("Europe/London")?
    meeting = tz.datetime(2024, 3, 9, 9, 30)?.to_zoned(&new_york, tz.Disambiguation.Reject)?
    next_week = meeting.checked_add(&tz.span(weeks=1), tz.Disambiguation.Reject)?
    print(f"{next_week}"?)?
    print(f"{next_week.in_zone(&london)?}"?)?
    print(meeting.span_until(&next_week).hours())?
    Ok(())
```

```text
2024-03-16T09:30:00-04:00[America/New_York]
2024-03-16T13:30:00+00:00[Europe/London]
167
```

A week later is the same wall-clock time, but only 167 hours later: New York
moved its clocks forward on 10 March. `tests/test_example.plenty` checks this
output.

## API

All values are small `@copy` classes. Constructors and arithmetic return
`Result[_, TzError]`; nothing traps on bad input or overflow.

| Type | Meaning |
|---|---|
| `Date`, `Time`, `DateTime` | Civil values in the proleptic Gregorian calendar, years -9999 to 9999, nanosecond precision |
| `Span` | Years, months, weeks, days (calendar units) and hours, minutes, seconds, nanoseconds (exact units), kept separately |
| `Offset` | A UTC offset, whole seconds, below 26 hours |
| `TimeZone` | An IANA zone, `utc()`, or `fixed(&offset)` |
| `Zoned` | A `std.time.Timestamp` with its zone, offset, and civil reading |
| `OffsetInfo` | Offset, DST flag, and abbreviation in force at an instant |
| `LocalOffsets` | `Unique(offset)`, `Gap(before, after)`, or `Fold(before, after)` for a local time |
| `Disambiguation` | `Compatible`, `Earlier`, `Later`, or `Reject` |
| `TzError` | `InvalidDate`, `InvalidTime`, `InvalidOffset`, `OutOfRange`, `UnknownZone`, `Syntax`, `Gap`, `Fold`, `OffsetConflict`, `TimeUnitsOnDate` |

Constructors: `date`, `date_from_epoch_day`, `time`, `midnight`, `datetime`,
`span` (keyword arguments, each defaulting to 0), `offset`, `offset_hours`,
`utc`, `fixed`, `zone`, `zoned`, and `now`.

Arithmetic follows jiff:

- `Date.checked_add` adds years and months first, clamping the day to the end
  of the month (`2024-01-31` plus one month is `2024-02-29`), then weeks and
  days. It rejects a span with exact units (`TimeUnitsOnDate`).
- `DateTime.checked_add` adds calendar units the same way, then exact time.
- `Zoned.checked_add` adds calendar units to the wall clock, resolves the
  result in the zone with the given `Disambiguation`, then adds exact time.
  So `span(days=1)` across a DST change keeps the wall-clock time, and
  `span(hours=24)` keeps the elapsed time.
- `Date.until` returns years, months, and days; `Zoned.span_until` returns
  exact units only.

`Disambiguation` resolves a local time that a gap skips or a fold repeats:

| | Gap | Fold |
|---|---|---|
| `Compatible` | Later instant (the clock moves forward by the gap) | Earlier instant |
| `Earlier` | Earlier instant | Earlier instant |
| `Later` | Later instant | Later instant |
| `Reject` | `Err(TzError.Gap)` | `Err(TzError.Fold)` |

`Compatible` is the default of RFC 5545, Temporal, and jiff.

Text:

- `parse_timestamp` accepts strict RFC 3339 only: `YYYY-MM-DDTHH:MM:SS`,
  `T` or `t` (or a space), an optional fraction of 1 to 9 digits, then `Z`,
  `z`, or `±HH:MM`. A leap second (`:60`) is `InvalidTime`, as `Timestamp`
  cannot represent one. `format_timestamp` writes UTC with `Z`.
- `parse_date`, `parse_time`, `parse_datetime`, and `parse_offset` read the
  ISO 8601 extended forms that `display` writes. Years outside 0 to 9999 use
  a sign and six digits, such as `-000001-01-01`.
- `parse_zoned` reads RFC 9557, such as
  `2024-11-03T01:30:00-05:00[America/New_York]`. The offset selects the
  instant; if it is not valid for the zone at that local time, the result is
  `OffsetConflict`. `[!zone]` is accepted. Later elective annotations such as
  `[u-ca=iso8601]` are ignored; critical ones (`[!u-ca=...]`) are `Syntax`.
- `Zoned.display` writes the same form. A fixed zone writes `[+05:30]`, and
  `utc()` writes `[UTC]`.

`tzdata_version()` returns the bundled release, and `zone_names()` every
zone and link name, newline-separated and sorted.

Not included: leap seconds, locale-aware formatting, `strftime`-style
patterns, non-Gregorian calendars, POSIX `TZ` strings as input, and spans
rounded or balanced into larger units.

## Data

Bundled data: **IANA tzdata 2026f**, the main data files plus `backward`
(344 zones, 253 links). `backzone` and `factory` are excluded, as in a
default `make install`.

`scripts/generate.py` downloads the pinned tzdata and tzcode archives,
checks their SHA-512 digests, builds the reference `zic` and `zdump`, and
compiles the data with `zic -b slim`. It then converts each TZif file into a
compact form under `src/zoneinfo/` (120,210 bytes in all) and writes
`src/tzdb.plenty` and `src/tzdb.index`. The format is described at the top
of `src/tzdb.plenty`: transitions as 40-bit times, each zone's types, and the
TZif footer rule as structured fields, so the package never parses POSIX
`TZ` strings at run time.

Generating Plenty source instead of reading TZif at run time keeps the
package free of file access and native code. Each zone is a separate
constant, so a program that never calls `tz.zone` links no zone data.

The script also writes `tests/data/zdump.txt`: every transition from 1800 to
2040 in every zone that has one (47,338 vectors), as `zdump` reports it.
`zdump` reads the same TZif files independently, so
`tests/test_conformance.plenty` checks this package's table search and
footer-rule evaluation against the reference implementation.

### Updating

1. Change `TZDB_VERSION` and both SHA-512 digests in `scripts/generate.py`
   (from `https://data.iana.org/time-zones/releases/`).
2. Run `python3 scripts/generate.py`. It needs Python 3.11 or later (standard
   library only), `make`, and a C compiler.
3. Run `plenty test`.
4. Commit the regenerated files, bump the package version, and tag it.

The script passes absolute paths to `zdump`, because `zdump` ignores the
`TZDIR` environment variable and would otherwise read the system's zone
files, which may be built from different data or options.

## Size and build time

Measured on x86-64 Linux with Plenty 0.7.1, `plenty build --release` from a
clean `target/`, without the compiled-function cache:

| Program | Executable | Build |
|---|---|---|
| Empty `main` | 122,008 bytes | 47 ms |
| Civil dates and `utc()` only | 157,768 bytes | 83 ms |
| `tz.zone(...)` lookup | 307,624 bytes | 86 ms |

A zone name is found by binary search over a sorted name table, so a lookup
adds its code and the zone data (about 150 KB) rather than one comparison per
name.

## Testing

```sh
plenty test
```

The tests cover the conformance vectors, named DST cases (New York, London,
Lord Howe's 30-minute shift, Apia skipping 30 December 2011, local mean
time), calendar arithmetic, range limits, and the RFC 3339 examples.

## Licence

The package is licensed under either of the [MIT licence](LICENSE-MIT) or the
[Apache License, Version 2.0](LICENSE-APACHE), at your option.

The time-zone data under `src/zoneinfo/`, `src/tzdb.plenty`,
`src/tzdb.index`, and `tests/data/` is derived from the IANA tz database,
which is in the public domain; see [`LICENSE-tzdata`](LICENSE-tzdata).
