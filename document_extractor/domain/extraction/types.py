"""Typed value parsers.

Amount parsing handles Indian digit grouping (1,23,456.00) alongside Western
grouping. Date parsing is deliberately constrained to an explicit format list:
dateutil.parser.parse() will silently resolve 03/04/2024 to March or April
depending on locale guesswork, which produces wrong data rather than missing
data.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

__all__ = ["ParseResult", "parse_amount", "parse_date", "parse_int",
           "parse_string", "parse_by_type"]

_CURRENCY = re.compile(
    r"(?:₹|Rs\.?|INR|USD|EUR|GBP|\$|€|£|¥|AED|SAR)\s*", re.IGNORECASE)
_TRAILING_SIGN = re.compile(r"\s*(CR|DR)\s*$", re.IGNORECASE)
_NUMERIC_OK = re.compile(r"^-?\d+(\.\d+)?$")


class ParseResult:
    __slots__ = ("value", "ok", "reason", "ambiguous", "note")

    def __init__(self, value=None, ok=False, reason="", ambiguous=False, note=""):
        self.value = value
        self.ok = ok
        self.reason = reason
        self.ambiguous = ambiguous
        self.note = note

    def __repr__(self):
        return f"ParseResult({self.value!r}, ok={self.ok}, reason={self.reason!r})"


# --------------------------------------------------------------------- amount
def parse_amount(text: str) -> ParseResult:
    """Parse a monetary amount without coercion.

    Handles: ₹1,23,456.00 · Rs. 1,234.50 · 1.234,56 (European) · (1,234.00)
    negative · 1,234.00 CR/DR · 1 234,56 (space grouping).

    Grouping separators are removed regardless of *pattern*, so Indian
    (2,2,3) and Western (3,3,3) grouping both work without a locale switch.
    """
    if text is None:
        return ParseResult(reason="EMPTY")
    s = str(text).strip()
    if not s:
        return ParseResult(reason="EMPTY")

    s = _CURRENCY.sub("", s).strip()

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    m = _TRAILING_SIGN.search(s)
    if m:
        if m.group(1).upper() == "CR":
            negative = True
        s = _TRAILING_SIGN.sub("", s).strip()

    if s.startswith("-"):
        negative = True
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    s = s.replace("\u00a0", " ").replace(" ", "")
    if not s:
        return ParseResult(reason="EMPTY")

    if not re.fullmatch(r"[\d.,'’]+", s):
        return ParseResult(reason="NON_NUMERIC")

    s = s.replace("'", "").replace("\u2019", "")

    # Decide the decimal separator by *position*: the last separator followed
    # by exactly two digits and no further separator is the decimal point.
    last_dot = s.rfind(".")
    last_comma = s.rfind(",")
    decimal_pos = -1
    if last_dot > last_comma:
        if re.fullmatch(r"\d{1,2}", s[last_dot + 1:]):
            decimal_pos = last_dot
    elif last_comma > last_dot:
        if re.fullmatch(r"\d{1,2}", s[last_comma + 1:]):
            decimal_pos = last_comma

    if decimal_pos >= 0:
        int_part = re.sub(r"[.,]", "", s[:decimal_pos])
        frac_part = s[decimal_pos + 1:]
        cleaned = f"{int_part}.{frac_part}" if frac_part else int_part
    else:
        cleaned = re.sub(r"[.,]", "", s)

    if not cleaned or not _NUMERIC_OK.match(cleaned):
        return ParseResult(reason="NON_NUMERIC")

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return ParseResult(reason="NON_NUMERIC")

    if negative:
        value = -value
    return ParseResult(value=value, ok=True)


# ----------------------------------------------------------------------- date
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_NUMERIC_DATE = re.compile(r"^(\d{1,4})[\-/.](\d{1,2})[\-/.](\d{1,4})$")


def parse_date(text: str, formats: list[str] | None = None,
               dayfirst: bool = True) -> ParseResult:
    """Parse a date against an explicit format list.

    If the value is genuinely ambiguous (day<=12 and month<=12 under a numeric
    D/M/Y pattern with no template guidance), the result is flagged ambiguous
    rather than resolved by guesswork.
    """
    if text is None:
        return ParseResult(reason="EMPTY")
    s = re.sub(r"\s+", " ", str(text).strip().replace(",", " ")).strip()
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE).strip()
    if not s:
        return ParseResult(reason="EMPTY")

    formats = formats or ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"]

    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        ambiguous = False
        m = _NUMERIC_DATE.match(s)
        if m and "%b" not in fmt and "%B" not in fmt:
            a, b = int(m.group(1)), int(m.group(2))
            # Both fields could be a month and the template does not pin it.
            if a <= 12 and b <= 12 and a != b and len(m.group(1)) <= 2:
                ambiguous = _competing_format(s, formats, parsed)
        return ParseResult(value=parsed, ok=True, ambiguous=ambiguous,
                           note=fmt, reason="AMBIGUOUS" if ambiguous else "")

    # Textual month fallback: "15 March 2024", "March 15 2024"
    tokens = re.split(r"[ \-/.]+", s)
    if len(tokens) == 3:
        lowered = [t.lower() for t in tokens]
        for i, tok in enumerate(lowered):
            if tok in _MONTHS:
                month = _MONTHS[tok]
                nums = [int(t) for j, t in enumerate(tokens)
                        if j != i and t.isdigit()]
                if len(nums) == 2:
                    day, year = (nums[0], nums[1]) if nums[1] > 31 else (nums[1], nums[0])
                    if year < 100:
                        year += 2000 if year < 70 else 1900
                    try:
                        return ParseResult(value=dt.date(year, month, day), ok=True,
                                           note="textual-month")
                    except ValueError:
                        return ParseResult(reason="RANGE_VIOLATION")
    return ParseResult(reason="TYPE_PARSE_FAILED")


def _competing_format(s: str, formats: list[str], first: dt.date) -> bool:
    """True if another configured format parses the same string to a different date."""
    for fmt in formats:
        try:
            other = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if other != first:
            return True
    return False


# ------------------------------------------------------------------ int / str
def parse_int(text: str) -> ParseResult:
    if text is None:
        return ParseResult(reason="EMPTY")
    s = re.sub(r"[,\s]", "", str(text).strip())
    if re.fullmatch(r"-?\d+", s):
        return ParseResult(value=int(s), ok=True)
    dec = parse_amount(text)
    if dec.ok and dec.value == dec.value.to_integral_value():
        return ParseResult(value=int(dec.value), ok=True)
    return ParseResult(reason="TYPE_PARSE_FAILED")


def parse_string(text: str) -> ParseResult:
    if text is None:
        return ParseResult(reason="EMPTY")
    s = re.sub(r"\s+", " ", str(text)).strip()
    return ParseResult(value=s, ok=bool(s), reason="" if s else "EMPTY")


def parse_by_type(text: str, data_type: str, *, formats=None,
                  dayfirst: bool = True) -> ParseResult:
    dt_ = (data_type or "string").lower()
    if dt_ in ("decimal", "amount", "money", "float", "number"):
        return parse_amount(text)
    if dt_ in ("date", "datetime"):
        return parse_date(text, formats, dayfirst)
    if dt_ in ("int", "integer"):
        return parse_int(text)
    return parse_string(text)
