import re

_PRICE = re.compile(r"\d[\d\s.  ]*(?:,\d{1,2})?")


def parse_price_ore(text: str | None) -> int | None:
    """Parse a Swedish price like '1 299,00 kr' to integer öre; None if no price."""
    if not text:
        return None
    m = _PRICE.search(text)
    if not m:
        return None
    raw = m.group(0).strip()
    whole, _, frac = raw.partition(",")
    digits = re.sub(r"[\s.  ]", "", whole)
    if not digits:
        return None
    return int(digits) * 100 + int(frac.ljust(2, "0")) if frac else int(digits) * 100
