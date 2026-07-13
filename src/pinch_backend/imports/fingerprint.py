"""The duplicate fingerprint (PRD M4 #16): one versioned function.

The recipe is locked: hash of ``account_id | date | amount_minor |
normalized(description_raw)``, normalization = Unicode NFKC → casefold →
collapse whitespace runs → trim. Deliberately nothing cleverer — digits and
punctuation are kept, so "CHECK #1234" ≠ "CHECK #1235".

Changing the recipe means adding ``fingerprint_v2`` and recomputing the
stored Transaction.fingerprint column in the same change — it is a pure
function of retained source data, so recomputation is always possible.
"""

import hashlib
import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid
    from datetime import date

_WHITESPACE_RUNS = re.compile(r"\s+")


def normalize_description(raw: str) -> str:
    normalized = unicodedata.normalize("NFKC", raw).casefold()
    return _WHITESPACE_RUNS.sub(" ", normalized).strip()


def fingerprint_v1(
    account_id: "uuid.UUID", txn_date: "date", amount_minor: int, description_raw: str
) -> str:
    payload = "|".join(
        (
            str(account_id),
            txn_date.isoformat(),
            str(amount_minor),
            normalize_description(description_raw),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


compute_fingerprint = fingerprint_v1
"""What writers call; points at the current recipe version."""
