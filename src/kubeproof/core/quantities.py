"""Small Kubernetes quantity parser for v0.1 profile thresholds."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


class InvalidQuantity(ValueError):
    pass


_QUANTITY = re.compile(
    r"^(?P<number>[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))"
    r"(?P<suffix>Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|K|M|G|T|P|E|[eE][+-]?[0-9]+)?$"
)

_MULTIPLIERS: dict[str, Decimal] = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(2) ** 10,
    "Mi": Decimal(2) ** 20,
    "Gi": Decimal(2) ** 30,
    "Ti": Decimal(2) ** 40,
    "Pi": Decimal(2) ** 50,
    "Ei": Decimal(2) ** 60,
}


def parse_quantity(value: str) -> Decimal:
    """Return a non-negative quantity in base units."""
    match = _QUANTITY.fullmatch(value)
    if match is None:
        raise InvalidQuantity(f"invalid Kubernetes quantity: {value!r}")
    try:
        number = Decimal(match.group("number"))
        suffix = match.group("suffix") or ""
        if suffix.startswith(("e", "E")):
            result = number * (Decimal(10) ** int(suffix[1:]))
        else:
            result = number * _MULTIPLIERS[suffix]
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise InvalidQuantity(f"invalid Kubernetes quantity: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise InvalidQuantity(f"quantity must be finite and non-negative: {value!r}")
    return result
