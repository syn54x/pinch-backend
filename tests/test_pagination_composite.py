"""The composite (date desc, id desc) keyset paginator (M5 CP1, #19)."""

import uuid
from datetime import date

import pytest
from litestar.exceptions import ClientException

from pinch_backend.api.pagination import decode_date_cursor, encode_date_cursor


def test_cursor_round_trips() -> None:
    d, i = date(2026, 1, 30), uuid.uuid7()
    assert decode_date_cursor(encode_date_cursor(d, i)) == (d, i)


def test_garbage_cursor_is_a_client_error() -> None:
    with pytest.raises(ClientException):
        decode_date_cursor("not-a-cursor")
