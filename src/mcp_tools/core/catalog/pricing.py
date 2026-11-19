"""Estimated price of a normalised request, in credits (1 credit = 0.001 USD)."""

import math
from dataclasses import dataclass

from mcp_tools.core.catalog.normalize import NormalizedRequest
from mcp_tools.core.catalog.spec import ModelSpec

CREDIT_USD = 0.001


@dataclass(frozen=True)
class Quote:
    per_job: int
    count: int
    unit: str
    units: float
    multiplier: float

    @property
    def total(self) -> int:
        return self.per_job * self.count

    def breakdown(self) -> dict[str, object]:
        return {
            "per_job": self.per_job,
            "count": self.count,
            "unit": self.unit,
            "units": self.units,
            "multiplier": self.multiplier,
        }


def quote(model: ModelSpec, request: NormalizedRequest) -> Quote:
    rule = model.pricing
    if rule.unit == "per_second":
        units = float(request.duration or 0) or 1.0
    elif rule.unit == "per_1k_chars":
        units = max(1.0, math.ceil(len(request.prompt or "") / 1000))
    else:
        units = 1.0
    multiplier = 1.0
    for name, table in rule.multipliers.items():
        value = request.aspect_ratio if name == "aspect_ratio" else request.params.get(name)
        if value is not None:
            multiplier *= table.get(str(value), 1.0)
    per_job = max(rule.minimum, math.ceil(rule.credits * units * multiplier))
    return Quote(per_job=per_job, count=request.count, unit=rule.unit, units=units, multiplier=multiplier)
