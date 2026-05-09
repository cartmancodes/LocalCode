#!/usr/bin/env python3

import sys


def multiply(a, b):
    """Return the product of two numeric values."""
    if not isinstance(a, (int, float)) or isinstance(a, bool):
        raise TypeError(f"Invalid input for 'a': expected a numeric value, got {type(a).__name__}")
    if not isinstance(b, (int, float)) or isinstance(b, bool):
        raise TypeError(f"Invalid input for 'b': expected a numeric value, got {type(b).__name__}")
    return a * b


def _parse_number(value, name):
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid input for '{name}': expected a numeric value, got {value!r}") from exc


def main():
    raw = sys.stdin.read().strip().split()
    if len(raw) != 2:
        print("Error: expected exactly two numbers from stdin.", file=sys.stderr)
        return 1

    try:
        a = _parse_number(raw[0], "a")
        b = _parse_number(raw[1], "b")
        result = multiply(a, b)
    except (TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
