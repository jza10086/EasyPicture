"""Cheap process-local counters for reproducible performance investigations."""

from dataclasses import asdict, dataclass, fields


@dataclass(slots=True)
class PerformanceCounters:
    domain_events: int = 0
    change_sets: int = 0
    snapshots: int = 0
    frame_requests: int = 0
    software_frames: int = 0
    source_cache_hits: int = 0
    source_cache_misses: int = 0
    thumbnails: int = 0

    def reset(self) -> None:
        for field in fields(self):
            setattr(self, field.name, 0)

    def snapshot(self) -> dict[str, int]:
        return asdict(self)


performance_counters = PerformanceCounters()
