"""Phone numbers: written the way people write them, kept as E.164 (+447700900123), and checked before anything is
sent to them. Outbound texts and calls cost money and reach real people, so a number must be in a country the user
allows, and never a premium-rate or special-service range (the usual toll-fraud targets), whatever the agent says."""
from __future__ import annotations

import re

E164 = re.compile(r"^\+[1-9]\d{6,14}$")

# Ranges blocked even inside an allowed country (unless the user allows that exact prefix): premium rate, personal
# numbering, directory enquiries, satellite and international premium services, and the North American area codes of
# the "one ring" scams (Caribbean numbers that look domestic but bill like premium lines).
BLOCKED = (
    "+449", "+4487", "+4484", "+4470", "+44118", "+4482", "+4476",         # UK: 09, 087, 084, 070, 118, 082, 076
    "+1900", "+1976",                                                       # North America: premium
    "+1268", "+1284", "+1473", "+1649", "+1664", "+1767", "+1809", "+1829",
    "+1849", "+1868", "+1869", "+1876",                                     # Caribbean NANP
    "+870", "+881", "+882", "+883", "+979",                                 # satellite, international networks
)


class NumberError(ValueError):
    """A number that can't be used (the message says why, in words the agent and the user can act on)."""


def normalize(raw: str, default_prefix: str = "+44") -> str:
    """``raw`` as E.164: spaces, dashes, dots and brackets go, 00 becomes +, and a national number (07700 900123)
    gets ``default_prefix`` in place of its leading 0."""
    if not isinstance(raw, str):
        raise NumberError("expected a phone number")
    s = re.sub(r"[\s\-().]", "", raw.strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    elif s.startswith("0") and default_prefix:
        s = default_prefix + s[1:]
    elif not s.startswith("+") and s.isdigit() and default_prefix == "+1" and len(s) == 10:
        s = "+1" + s                                                          # 5550100123 in North America
    if not E164.match(s):
        raise NumberError(f"{raw!r} is not a phone number I can use (expected something like +44 7700 900123)")
    return s


def prefix_of(prefixes: list[str] | tuple[str, ...], number: str) -> str | None:
    """The longest of ``prefixes`` that ``number`` starts with."""
    hits = [p for p in prefixes if number.startswith(p)]
    return max(hits, key=len) if hits else None


def check_outbound(number: str, allow: list[str]) -> None:
    """Raise NumberError unless ``number`` (E.164) may be texted or called: in an allowed country (``allow``: E.164
    prefixes like "+44"), and not in a blocked range, unless the user allowed that range itself."""
    if not E164.match(number or ""):
        raise NumberError(f"{number!r} is not an E.164 phone number")
    allowed = prefix_of(allow, number)
    if allowed is None:
        raise NumberError(f"{number} is outside the countries allowed for calls and texts ({', '.join(allow) or 'none'}); "
                          f"the user can add its country code in Settings > Phone")
    blocked = prefix_of(BLOCKED, number)
    if blocked and len(blocked) > len(allowed):
        raise NumberError(f"{number} is a premium-rate or special-service number ({blocked}...), which is never "
                          f"called or texted")


def display(number: str) -> str:
    """A readable form for speech and the screen: +44 7700 900123."""
    if number.startswith("+44") and len(number) == 13:
        return f"+44 {number[3:7]} {number[7:]}"
    if number.startswith("+1") and len(number) == 12:
        return f"+1 {number[2:5]} {number[5:8]} {number[8:]}"
    return number
