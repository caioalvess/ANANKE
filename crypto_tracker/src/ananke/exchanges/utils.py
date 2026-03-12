"""Shared utilities for exchange implementations."""


def safe_float(value: str | int | float | None) -> float:
    """Convert to float, returning 0.0 for empty strings or None."""
    if value is None or value == "":
        return 0.0
    return float(value)
