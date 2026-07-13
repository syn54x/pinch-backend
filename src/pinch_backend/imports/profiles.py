"""Import-profile shape identity (PRD M4 #16): normalized header tuple +
delimiter, and nothing subtler — a wrong auto-applied mapping silently
mis-signs amounts, so identity errs toward not matching.
"""


def normalized_header_tuple(cells: list[str]) -> list[str]:
    """Casefold + trim, order preserved: order is part of the identity —
    the same names in a different order is a different shape."""
    return [cell.strip().casefold() for cell in cells]


def shape_key(header_tuple: list[str], delimiter: str) -> str:
    """A deterministic string key for (headers, delimiter), joined on ASCII
    separator control characters that cannot appear in a delimiter and are
    vanishingly unlikely inside a header cell."""
    return delimiter + "\x1e" + "\x1f".join(header_tuple)
