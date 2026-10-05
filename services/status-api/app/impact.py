from __future__ import annotations

from datetime import datetime, timedelta

from .models import PublicStatus, worst_status

# A gap between samples longer than this is not counted as impact time, so a
# collector outage cannot inflate the duration of an incident.
MAX_SAMPLE_GAP = timedelta(minutes=10)

Sample = tuple[datetime, PublicStatus]
Impact = tuple[datetime, datetime, PublicStatus]


def significant_impacts(
    samples: list[Sample],
    min_impact_seconds: int,
    end: datetime,
) -> list[Impact]:
    """Runs of non-operational samples that lasted at least min_impact_seconds.

    Duration is measured from the first bad sample to the next sample (each gap
    capped at MAX_SAMPLE_GAP). A run seen in a single sample is ignored unless it
    is still ongoing: with sparse sampling one sample cannot show that an impact
    lasted. min_impact_seconds=0 keeps every run.
    """
    known = sorted(
        ((at, status) for at, status in samples if status != PublicStatus.UNKNOWN),
        key=lambda item: item[0],
    )
    impacts: list[Impact] = []
    index = 0
    while index < len(known):
        if known[index][1] == PublicStatus.OPERATIONAL:
            index += 1
            continue
        run_start = known[index][0]
        statuses: list[PublicStatus] = []
        duration = timedelta()
        while index < len(known) and known[index][1] != PublicStatus.OPERATIONAL:
            at, status = known[index]
            next_at = known[index + 1][0] if index + 1 < len(known) else end
            duration += min(max(next_at - at, timedelta()), MAX_SAMPLE_GAP)
            statuses.append(status)
            index += 1
        ongoing = index >= len(known)
        if min_impact_seconds > 0 and len(statuses) < 2 and not ongoing:
            continue
        if duration.total_seconds() >= min_impact_seconds:
            impacts.append((run_start, run_start + duration, worst_status(statuses)))
    return impacts


def bucket_statuses(
    samples: list[Sample],
    starts: list[datetime],
    bucket: timedelta,
    min_impact_seconds: int,
    end: datetime,
) -> list[PublicStatus]:
    """Status per time bucket: impacted only by a significant run overlapping it."""
    impacts = significant_impacts(samples, min_impact_seconds, end)
    result: list[PublicStatus] = []
    for start in starts:
        stop = start + bucket
        overlapping = [
            status for run_start, run_end, status in impacts if run_start < stop and run_end >= start
        ]
        has_data = any(
            at_status != PublicStatus.UNKNOWN and start <= at < stop for at, at_status in samples
        )
        if overlapping:
            result.append(worst_status(overlapping))
        else:
            result.append(PublicStatus.OPERATIONAL if has_data else PublicStatus.UNKNOWN)
    return result
