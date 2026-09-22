"""Reference identities joined to the unchanged core metric catalog.

Only the tracked Markdown supplies documented topics. XTOP names come from an
observed snapshot; their paths are known only when a core Source supplies one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pompa.catalog import METRICS, Metric, Source

Family = Literal["TOP", "OPT", "SET", "XTOP"]
Provenance = Literal["documented", "observed"]


class ReferenceError(ValueError):
    """The tracked reference no longer matches the supported grammar."""


@dataclass(frozen=True, slots=True)
class ReferenceIdentity:
    identity: str
    family: Family
    index: int
    name: str
    topic: str | None
    description: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Capability:
    reference: ReferenceIdentity
    key: str
    metric: Metric | None = None
    source: Source | None = None
    source_priority: int | None = None


_SECTIONS: tuple[tuple[str, Family, int], ...] = (
    ("## Sensor Topics:", "TOP", 3),
    ("## Option PCB Topics:", "OPT", 3),
    ("## Command Topics:", "SET", 4),
)
_HEADERS = {
    "TOP": ("ID", "Topic", "Response/Description"),
    "OPT": ("ID", "Topic", "Response/Description"),
    "SET": ("ID", "Topic", "Description", "Value/Range"),
}
_IDENTITY = re.compile(r"^(TOP|OPT|SET|XTOP)(0|[1-9][0-9]*)$")
_PATH = re.compile(r"^[A-Za-z0-9_]+/[A-Za-z0-9_]+$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# The observed snapshot swaps these pairs relative to the documented table.
# Keep documented paths authoritative and reject any further name drift.
_OBSERVED_NAME_DISCREPANCIES = {
    "TOP111": ("Z2_Sensor_Settings", "Z1_Sensor_Settings"),
    "TOP112": ("Z1_Sensor_Settings", "Z2_Sensor_Settings"),
    "TOP123": ("Z1_Pump_State", "Z2_Pump_State"),
    "TOP124": ("Z2_Pump_State", "Z1_Pump_State"),
}


def _identity(raw: str, family: Family) -> int:
    match = _IDENTITY.fullmatch(raw)
    if match is None or match.group(1) != family:
        raise ReferenceError(f"Invalid {family} identity: {raw!r}")
    return int(match.group(2))


def _unique(entries: tuple[ReferenceIdentity, ...]) -> None:
    identities: set[str] = set()
    topics: dict[str, str] = {}
    for entry in entries:
        if entry.identity in identities:
            raise ReferenceError(f"Duplicate identity: {entry.identity}")
        identities.add(entry.identity)
        if entry.topic is not None:
            previous = topics.get(entry.topic)
            if previous is not None:
                raise ReferenceError(
                    f"Duplicate topic {entry.topic}: {previous}, {entry.identity}"
                )
            topics[entry.topic] = entry.identity


def _contiguous(entries: tuple[ReferenceIdentity, ...], family: Family, first: int) -> None:
    indexes = sorted(entry.index for entry in entries if entry.family == family)
    if not indexes or indexes != list(range(first, indexes[-1] + 1)):
        raise ReferenceError(f"Missing or non-contiguous {family} identities")


def parse_documented(text: str) -> tuple[ReferenceIdentity, ...]:
    """Parse the three supported tables in MQTT-Topics.md, in family/index order."""
    lines = text.splitlines()
    entries: list[ReferenceIdentity] = []
    for heading, family, width in _SECTIONS:
        positions = [i for i, line in enumerate(lines) if line.strip() == heading]
        if len(positions) != 1:
            raise ReferenceError(f"Expected one {heading} section")
        start = positions[0] + 1
        end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
        section = lines[start:end]
        headers = [i for i, line in enumerate(section) if line.strip().startswith("ID |")]
        if len(headers) != 1:
            raise ReferenceError(f"Expected one {family} table header")
        header = headers[0]
        if tuple(cell.strip() for cell in section[header].split("|")) != _HEADERS[family]:
            raise ReferenceError(f"Malformed {family} table header")
        if header + 1 >= len(section) or not all(
            re.fullmatch(r":?-{3,}:?", cell.strip())
            for cell in section[header + 1].split("|")
        ) or len(section[header + 1].split("|")) != width:
            raise ReferenceError(f"Malformed {family} table separator")
        row_start = header + 2
        row_end = next((i for i in range(row_start, len(section)) if not section[i].strip()), len(section))
        if row_start == row_end:
            raise ReferenceError(f"Empty {family} table")
        for line in section[row_start:row_end]:
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) != width or any(not cell for cell in cells):
                raise ReferenceError(f"Malformed {family} row: {line!r}")
            raw_id, raw_topic = cells[:2]
            index = _identity(raw_id, family)
            if family == "SET":
                if not _NAME.fullmatch(raw_topic):
                    raise ReferenceError(f"Invalid SET name: {raw_topic!r}")
                name, topic = raw_topic, f"commands/{raw_topic}"
                description = " | ".join(cells[2:])
            else:
                if not _PATH.fullmatch(raw_topic) or not raw_topic.startswith(
                    "main/" if family == "TOP" else "optional/"
                ):
                    raise ReferenceError(f"Invalid {family} topic: {raw_topic!r}")
                name, topic = raw_topic.split("/", 1)[1], raw_topic
                description = cells[2]
            entries.append(ReferenceIdentity(raw_id, family, index, name, topic, description, "documented"))
        # A table row after a blank would otherwise disappear silently.
        if any(re.match(r"^(?:TOP|OPT|SET)[^ ]*\s*\|", line.strip()) or
               line.lstrip().startswith("|") for line in section[row_end:]):
            raise ReferenceError(f"Row outside {family} table")
    result = tuple(sorted(entries, key=lambda e: (dict(TOP=0, OPT=1, SET=2)[e.family], e.index)))
    _unique(result)
    for family, first in (("TOP", 0), ("OPT", 0), ("SET", 1)):
        _contiguous(result, family, first)
    return result


def parse_observed(text: str, documented: tuple[ReferenceIdentity, ...]) -> tuple[ReferenceIdentity, ...]:
    """Validate observed TOP names and extract XTOP names without guessing paths."""
    lines = text.splitlines()
    if not lines or lines[0] != "Topic\tName\tValue\tDescription":
        raise ReferenceError("Malformed observed header")
    documented_top = {e.identity: e for e in documented if e.family == "TOP"}
    seen: set[str] = set()
    discrepancies: set[str] = set()
    xtops: list[ReferenceIdentity] = []
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) != 4 or not all(cells[:3]):
            raise ReferenceError(f"Malformed observed row: {line!r}")
        raw_id, name = cells[:2]
        match = _IDENTITY.fullmatch(raw_id)
        if match is None or match.group(1) not in ("TOP", "XTOP") or not _NAME.fullmatch(name):
            raise ReferenceError(f"Invalid observed identity/name: {line!r}")
        if raw_id in seen:
            raise ReferenceError(f"Duplicate observed identity: {raw_id}")
        seen.add(raw_id)
        if match.group(1) == "TOP":
            reference = documented_top.get(raw_id)
            if reference is None:
                raise ReferenceError(f"Observed TOP name conflicts with reference: {raw_id}")
            if reference.name != name:
                if _OBSERVED_NAME_DISCREPANCIES.get(raw_id) != (reference.name, name):
                    raise ReferenceError(f"Observed TOP name conflicts with reference: {raw_id}")
                discrepancies.add(raw_id)
        else:
            xtops.append(ReferenceIdentity(raw_id, "XTOP", int(match.group(2)), name, None,
                                           cells[3] or None, "observed"))
    if seen.intersection(documented_top) != set(documented_top):
        raise ReferenceError("Observed TOP coverage differs from documented TOP coverage")
    if discrepancies != set(_OBSERVED_NAME_DISCREPANCIES):
        raise ReferenceError("Verified observed TOP discrepancies changed")
    result = tuple(sorted(xtops, key=lambda e: e.index))
    _unique(result)
    _contiguous(result, "XTOP", 0)
    if len({e.name for e in result}) != len(result):
        raise ReferenceError("Duplicate observed XTOP name")
    return result


def build_capabilities(
    documented: tuple[ReferenceIdentity, ...],
    observed: tuple[ReferenceIdentity, ...],
    metrics: tuple[Metric, ...] = METRICS,
) -> tuple[Capability, ...]:
    """Join reference identities with core objects; reject conflicting core facts."""
    baseline = documented + observed
    _unique(baseline)
    by_id = {entry.identity: entry for entry in baseline}
    associations: dict[str, tuple[Metric, Source, int]] = {}
    primary_ids: set[str] = set()
    for metric in metrics:
        for priority, source in enumerate(metric.sources):
            reference = by_id.get(source.id)
            if reference is None:
                raise ReferenceError(f"Core source has no reference identity: {source.id}")
            if source.id in associations:
                raise ReferenceError(f"Core identity used twice: {source.id}")
            if reference.family == "XTOP":
                # The snapshot proves the name; Stage 1 runtime proved core extra/ paths.
                if source.topic != f"extra/{reference.name}":
                    raise ReferenceError(f"Core XTOP name conflicts with observation: {source.id}")
            elif reference.topic != source.topic:
                raise ReferenceError(f"Core topic conflicts with reference: {source.id}")
            associations[source.id] = (metric, source, priority)
            if priority == 0:
                primary_ids.add(source.id)
    result = []
    for reference in baseline:
        association = associations.get(reference.identity)
        key = (association[0].key if reference.identity in primary_ids else
               f"{reference.family.lower()}_{reference.index}")
        result.append(Capability(reference, key, *association) if association else Capability(reference, key))
    if len({entry.key for entry in result}) != len(result):
        raise ReferenceError("Duplicate effective capability key")
    return tuple(result)


def reference_dir() -> Path:
    """The same docs/reference/heishamon tree locally and in the backend image."""
    module = Path(__file__).resolve()
    packaged = module.parents[1] / "docs/reference/heishamon"
    return packaged if packaged.is_dir() else module.parents[2] / "docs/reference/heishamon"


@lru_cache(maxsize=1)
def effective_capabilities() -> tuple[Capability, ...]:
    root = reference_dir()
    documented = parse_documented((root / "MQTT-Topics.md").read_text(encoding="utf-8"))
    observed = parse_observed((root / "realne_dane.md").read_text(encoding="utf-8"), documented)
    return build_capabilities(documented, observed)
