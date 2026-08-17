"""Validators.

Checksums matter more than they look: they turn a fuzzy OCR read into either a
verified identity or a definite error, with no middle ground where a plausible
wrong value can survive.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from ..models import Reason

# GSTIN: 2-digit state code, 10-char PAN, entity digit, 'Z', check character.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")

_GST_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_VALID_STATE_CODES = set(f"{i:02d}" for i in range(1, 39)) | {"97", "99"}


def gstin_check(value: str) -> tuple[bool, str]:
    """Validate a GSTIN including its mod-36 weighted check character."""
    if not value:
        return False, "EMPTY"
    v = re.sub(r"[\s\-]", "", str(value)).upper()
    if len(v) != 15:
        return False, "GSTIN_LENGTH"
    if not GSTIN_RE.match(v):
        return False, "GSTIN_FORMAT"
    if v[:2] not in _VALID_STATE_CODES:
        return False, "GSTIN_STATE_CODE"

    total = 0
    for i, ch in enumerate(v[:14]):
        idx = _GST_ALPHABET.find(ch)
        if idx < 0:
            return False, "GSTIN_FORMAT"
        factor = 2 if i % 2 else 1
        product = idx * factor
        total += product // 36 + product % 36
    expected = _GST_ALPHABET[(36 - (total % 36)) % 36]
    if expected != v[14]:
        return False, Reason.GSTIN_CHECKSUM_FAIL.value
    return True, ""


def pan_check(value: str) -> tuple[bool, str]:
    v = re.sub(r"\s", "", str(value or "")).upper()
    if not PAN_RE.match(v):
        return False, "PAN_FORMAT"
    if v[3] not in "ABCFGHLJPTK":
        return False, "PAN_ENTITY_TYPE"
    return True, ""


def iban_check(value: str) -> tuple[bool, str]:
    v = re.sub(r"\s", "", str(value or "")).upper()
    if not IBAN_RE.match(v):
        return False, "IBAN_FORMAT"
    rearranged = v[4:] + v[:4]
    digits = "".join(str(int(c, 36)) if c.isalpha() else c for c in rearranged)
    return (int(digits) % 97 == 1, "" if int(digits) % 97 == 1 else "IBAN_CHECKSUM")


def luhn_check(value: str) -> tuple[bool, str]:
    digits = [int(c) for c in re.sub(r"\D", "", str(value or ""))]
    if len(digits) < 2:
        return False, "LUHN_LENGTH"
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (total % 10 == 0, "" if total % 10 == 0 else "LUHN_CHECKSUM")


CHECKSUMS = {"gstin": gstin_check, "pan": pan_check,
             "iban": iban_check, "luhn": luhn_check}


def apply_validator(spec: dict, raw: str, typed) -> tuple[bool, str]:
    """Run one validator spec. Returns (passed, reason_code)."""
    kind = (spec.get("type") or "").lower()

    if kind == "regex":
        pattern = spec.get("pattern") or ".*"
        flags = re.IGNORECASE if spec.get("ignore_case") else 0
        try:
            if re.fullmatch(pattern, str(raw or "").strip(), flags):
                return True, ""
        except re.error:
            return True, ""   # a broken pattern must not block the document
        return False, Reason.REGEX_MISMATCH.value

    if kind == "length":
        n = len(str(raw or "").strip())
        lo, hi = spec.get("min", 0), spec.get("max", 10_000)
        return (lo <= n <= hi, "" if lo <= n <= hi else "LENGTH_VIOLATION")

    if kind == "checksum":
        fn = CHECKSUMS.get((spec.get("algorithm") or "").lower())
        if not fn:
            return True, ""
        return fn(str(raw or ""))

    if kind == "range":
        if typed is None:
            return False, Reason.TYPE_PARSE_FAILED.value
        try:
            if isinstance(typed, dt.date):
                lo = _rel_date(spec.get("min", "-5y"))
                hi = _rel_date(spec.get("max", "+1y"))
                return (lo <= typed <= hi,
                        "" if lo <= typed <= hi else Reason.RANGE_VIOLATION.value)
            val = Decimal(str(typed))
            lo = Decimal(str(spec.get("min", "-1e12")))
            hi = Decimal(str(spec.get("max", "1e12")))
            return (lo <= val <= hi,
                    "" if lo <= val <= hi else Reason.RANGE_VIOLATION.value)
        except Exception:
            return False, Reason.RANGE_VIOLATION.value

    if kind == "one_of":
        allowed = {str(x).lower() for x in spec.get("values", [])}
        v = str(raw or "").strip().lower()
        return (v in allowed, "" if v in allowed else "NOT_IN_ALLOWED_SET")

    if kind == "not_empty":
        v = str(raw or "").strip()
        return (bool(v), "" if v else "EMPTY_VALUE")

    return True, ""


def _rel_date(spec) -> dt.date:
    """'-5y' / '+1y' / '-90d' / an ISO date -> a date."""
    if isinstance(spec, dt.date):
        return spec
    s = str(spec).strip()
    m = re.fullmatch(r"([+-]?\d+)\s*([ydm])", s, re.IGNORECASE)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"y": 365, "m": 30, "d": 1}[unit] * n
        return dt.date.today() + dt.timedelta(days=days)
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return dt.date.today()


def run_validators(specs: list[dict], raw: str, typed) -> list[str]:
    """Return the list of failure reason codes (empty means all passed)."""
    failures: list[str] = []
    for spec in specs or []:
        ok, reason = apply_validator(spec, raw, typed)
        if not ok and reason:
            failures.append(reason)
    return failures
