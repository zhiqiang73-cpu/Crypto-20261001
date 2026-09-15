"""统一数据有效性契约."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from trading.models import DataValidity


@dataclass
class DataRecord:
    name: str
    value: Optional[float]
    unit: str = ""
    source: str = ""
    event_time_ms: Optional[int] = None
    fetch_time_ms: int = 0
    validity: DataValidity = DataValidity.MISSING
    is_proxy: bool = False
    proxy_label: Optional[str] = None
    quality_reason: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["validity"] = self.validity.value
        return d

    @property
    def usable(self) -> bool:
        return self.validity == DataValidity.VALID and self.value is not None


def make_record(
    name: str,
    value: Optional[float],
    *,
    unit: str = "",
    source: str = "",
    event_time_ms: Optional[int] = None,
    fetch_time_ms: int = 0,
    max_age_sec: Optional[float] = None,
    is_proxy: bool = False,
    proxy_label: Optional[str] = None,
    now_ms: Optional[int] = None,
) -> DataRecord:
    """根据值与时效构造 DataRecord."""
    import time
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    fetch = fetch_time_ms or now
    if value is None:
        return DataRecord(
            name=name, value=None, unit=unit, source=source,
            event_time_ms=event_time_ms, fetch_time_ms=fetch,
            validity=DataValidity.MISSING,
            is_proxy=is_proxy, proxy_label=proxy_label,
            quality_reason="missing",
        )
    validity = DataValidity.VALID
    reason = None
    if max_age_sec is not None and event_time_ms is not None:
        age = (now - event_time_ms) / 1000.0
        if age > max_age_sec:
            validity = DataValidity.STALE
            reason = f"age={age:.0f}s>{max_age_sec}"
    return DataRecord(
        name=name, value=float(value), unit=unit, source=source,
        event_time_ms=event_time_ms, fetch_time_ms=fetch,
        validity=validity, is_proxy=is_proxy, proxy_label=proxy_label,
        quality_reason=reason,
    )
