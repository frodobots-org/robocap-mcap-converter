"""Semantic profile of an MCAP, for golden-output equivalence checks.

A *profile* is the structural shape of a delivered MCAP that must survive a
refactor unchanged: which topics exist, their protobuf schema, message
counts, whether each topic's stored log_time stream is monotonic, the
absolute timestamp anchor, and the MCAP-level attachments/metadata records.

`profile_mcap()` extracts a profile without decoding message payloads (it
reads only channel/schema metadata + log_times), so it does not need the
protobuf registry and is robust to schema-internal changes we don't care
about. `compare_profiles()` diffs two profiles with configurable tolerance
on the timestamp anchor.

Scope note: bitstream-level checks (H.264 SPS-VUI sanity, DTS vs PTS) are a
planned follow-up cycle and are intentionally not covered here yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcap.reader import make_reader

# Default anchor tolerance: one 60fps frame interval. Absolute wall-clock
# anchors can legitimately wobble by sub-frame amounts across conversions;
# drift beyond this points at a unit/offset regression (cf. the F2 µs/ms bug).
DEFAULT_ANCHOR_TOLERANCE_NS = 16_000_000


@dataclass(frozen=True)
class TopicProfile:
    topic: str
    schema_name: str
    message_count: int
    first_log_time_ns: int | None
    last_log_time_ns: int | None
    monotonic: bool


@dataclass(frozen=True)
class MCAPProfile:
    topics: dict[str, TopicProfile]
    attachments: list[tuple[str, str]]  # (name, media_type), sorted
    metadata: dict[str, dict[str, str]]


@dataclass(frozen=True)
class ProfileDiff:
    equivalent: bool
    differences: list[str]


def profile_mcap(path: str | Path) -> MCAPProfile:
    """Read an MCAP and return its semantic profile.

    Reads in *stored* order (`log_time_order=False`) so that a non-monotonic
    stream is observable — the reader would otherwise sort by log_time and
    hide exactly the regression we want to catch.
    """
    acc: dict[str, dict] = {}
    with open(path, "rb") as fh:
        reader = make_reader(fh)
        for schema, channel, message in reader.iter_messages(log_time_order=False):
            topic = channel.topic
            lt = message.log_time
            entry = acc.get(topic)
            if entry is None:
                acc[topic] = {
                    "schema": schema.name if schema is not None else "",
                    "count": 1,
                    "first": lt,
                    "last": lt,
                    "monotonic": True,
                }
            else:
                if lt < entry["last"]:
                    entry["monotonic"] = False
                entry["last"] = lt
                entry["count"] += 1
        attachments = sorted(
            (a.name, a.media_type) for a in reader.iter_attachments()
        )
        metadata = {m.name: dict(m.metadata) for m in reader.iter_metadata()}

    topics = {
        topic: TopicProfile(
            topic=topic,
            schema_name=e["schema"],
            message_count=e["count"],
            first_log_time_ns=e["first"],
            last_log_time_ns=e["last"],
            monotonic=e["monotonic"],
        )
        for topic, e in acc.items()
    }
    return MCAPProfile(topics=topics, attachments=attachments, metadata=metadata)


def to_snapshot(profile: MCAPProfile) -> dict:
    """A small, structural JSON snapshot of a profile — the committable/storable
    golden reference, so the full MCAPs need not be kept.

    Includes only structure: per-topic schema + message_count + monotonic,
    attachment (name, media_type), and metadata record *names*. Deliberately
    EXCLUDES absolute timestamps and metadata VALUES, so the snapshot is a few
    KB and carries no episode content / PII / collection-time info — safe to
    keep in a repo or shared store. Catches the regression-critical drift
    (topic/schema/count/monotonic). The absolute-anchor (F2 µs/ms) check still
    needs the real files and runs via compare_mcap_files in a local/secured run.
    """
    return {
        "topics": {
            t: {
                "schema_name": p.schema_name,
                "message_count": p.message_count,
                "monotonic": p.monotonic,
            }
            for t, p in profile.topics.items()
        },
        "attachments": [list(a) for a in profile.attachments],
        "metadata_keys": sorted(profile.metadata),
    }


def compare_snapshot(snapshot: dict, candidate: MCAPProfile) -> ProfileDiff:
    """Compare a freshly profiled candidate MCAP against a stored structural
    snapshot (from `to_snapshot`). No big golden MCAP required."""
    diffs: list[str] = []
    golden_topics = snapshot.get("topics", {})
    for topic in sorted(set(golden_topics) | set(candidate.topics)):
        g = golden_topics.get(topic)
        c = candidate.topics.get(topic)
        if g is None:
            diffs.append(f"unexpected topic {topic} present in candidate")
            continue
        if c is None:
            diffs.append(f"missing topic {topic} in candidate")
            continue
        if g["schema_name"] != c.schema_name:
            diffs.append(f"{topic}: schema {g['schema_name']!r} != {c.schema_name!r}")
        if g["message_count"] != c.message_count:
            diffs.append(
                f"{topic}: message_count {g['message_count']} != {c.message_count}")
        if g["monotonic"] and not c.monotonic:
            diffs.append(f"{topic}: monotonic regression")

    cand_attachments = [list(a) for a in candidate.attachments]
    if snapshot.get("attachments", []) != cand_attachments:
        diffs.append("attachments differ from snapshot")
    if snapshot.get("metadata_keys", []) != sorted(candidate.metadata):
        diffs.append("metadata record names differ from snapshot")

    return ProfileDiff(equivalent=not diffs, differences=diffs)


def compare_mcap_files(
    golden: str | Path,
    candidate: str | Path,
    *,
    anchor_tolerance_ns: int = DEFAULT_ANCHOR_TOLERANCE_NS,
) -> ProfileDiff:
    """Profile both MCAP files and compare them. Convenience wrapper used by
    the CLI and the golden-fixtures harness."""
    return compare_profiles(
        profile_mcap(golden),
        profile_mcap(candidate),
        anchor_tolerance_ns=anchor_tolerance_ns,
    )


def compare_profiles(
    golden: MCAPProfile,
    candidate: MCAPProfile,
    *,
    anchor_tolerance_ns: int = DEFAULT_ANCHOR_TOLERANCE_NS,
) -> ProfileDiff:
    """Diff two profiles. Equivalent iff there are no differences.

    Flags: topic set mismatch, per-topic schema mismatch, message-count
    drift, monotonicity regression (golden monotonic, candidate not), and
    absolute-anchor drift beyond `anchor_tolerance_ns`. Attachments and
    metadata records must match exactly.
    """
    diffs: list[str] = []

    for topic in sorted(set(golden.topics) | set(candidate.topics)):
        g = golden.topics.get(topic)
        c = candidate.topics.get(topic)
        if g is None:
            diffs.append(f"unexpected topic {topic} present in candidate")
            continue
        if c is None:
            diffs.append(f"missing topic {topic} in candidate")
            continue
        if g.schema_name != c.schema_name:
            diffs.append(
                f"{topic}: schema {g.schema_name!r} != {c.schema_name!r}"
            )
        if g.message_count != c.message_count:
            diffs.append(
                f"{topic}: message_count {g.message_count} != {c.message_count}"
            )
        if g.monotonic and not c.monotonic:
            diffs.append(
                f"{topic}: monotonic regression "
                f"(golden log_time monotonic, candidate not)"
            )
        if g.first_log_time_ns is not None and c.first_log_time_ns is not None:
            drift = abs(g.first_log_time_ns - c.first_log_time_ns)
            if drift > anchor_tolerance_ns:
                diffs.append(
                    f"{topic}: first_log_time anchor drift {drift}ns "
                    f"> tolerance {anchor_tolerance_ns}ns"
                )

    if golden.attachments != candidate.attachments:
        diffs.append(
            f"attachments differ: {golden.attachments} != {candidate.attachments}"
        )
    if golden.metadata != candidate.metadata:
        diffs.append(
            f"metadata differ: {sorted(golden.metadata)} != {sorted(candidate.metadata)}"
        )

    return ProfileDiff(equivalent=not diffs, differences=diffs)


def _main(argv: list[str] | None = None) -> int:
    """CLI: compare two MCAPs, or emit a small structural snapshot.

      mcap-profile golden.mcap candidate.mcap     # compare; exit 0/1
      mcap-profile --snapshot golden.mcap          # print snapshot JSON to stdout

    The snapshot is the KB-scale golden reference to keep instead of the MCAP.
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="mcap-profile",
        description="Golden-output equivalence check / structural snapshot for MCAPs.",
    )
    parser.add_argument("golden", type=Path, nargs="?",
                        help="golden (reference) MCAP, or the file to snapshot")
    parser.add_argument("candidate", type=Path, nargs="?",
                        help="candidate MCAP to compare against golden")
    parser.add_argument("--snapshot", action="store_true",
                        help="emit a structural snapshot JSON for `golden` and exit")
    parser.add_argument(
        "--anchor-tolerance-ns", type=int, default=DEFAULT_ANCHOR_TOLERANCE_NS,
        help="max allowed absolute-anchor drift in ns (default: one 60fps frame)",
    )
    args = parser.parse_args(argv)

    if args.snapshot:
        if args.golden is None:
            parser.error("--snapshot requires a file")
        print(_json.dumps(to_snapshot(profile_mcap(args.golden)), indent=2))
        return 0
    if args.golden is None or args.candidate is None:
        parser.error("compare mode requires golden and candidate")

    diff = compare_mcap_files(
        args.golden, args.candidate, anchor_tolerance_ns=args.anchor_tolerance_ns,
    )
    if diff.equivalent:
        print(f"EQUIVALENT  {args.golden.name} ≡ {args.candidate.name}")
        return 0
    print(f"DIFFERENT   {args.golden.name} ≠ {args.candidate.name}")
    for d in diff.differences:
        print(f"  - {d}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
