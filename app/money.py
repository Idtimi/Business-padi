from decimal import Decimal, ROUND_HALF_UP


def to_kobo(naira) -> int:
    """Convert naira (int, float, or string) to integer kobo."""
    value = Decimal(str(naira)) * 100
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_naira(kobo: int) -> str:
    """Format integer kobo for display, e.g. 4250000 -> N42,500."""
    sign = "-" if kobo < 0 else ""
    naira, rem = divmod(abs(kobo), 100)
    if rem:
        return f"{sign}N{naira:,}.{rem:02d}"
    return f"{sign}N{naira:,}"