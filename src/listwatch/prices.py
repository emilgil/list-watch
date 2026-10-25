import re

# Thousands separators seen on amazon.se: space, NBSP, narrow NBSP, '.' (sv) and ',' (en).
_SEP = r"[   .,]"
# Grouped thousands ("1 299", "1,299") or a plain integer, then an optional 1–2 digit
# decimal part. The first price in the text wins, so "217,90 kr kr217,90" is read once.
_PRICE = re.compile(rf"(?<!\d)(\d{{1,3}}(?:{_SEP}\d{{3}})+|\d+)(?:([.,])(\d{{1,2}}))?(?!\d)")


def parse_price_ore(text: str | None) -> int | None:
    """Parse an amazon.se price ('1 299,00 kr', 'kr217,90', 'kr1,299.00') to integer öre."""
    if not text:
        return None
    m = _PRICE.search(text)
    if not m:
        return None
    whole, _, frac = m.groups()
    kronor = int(re.sub(r"\D", "", whole))
    return kronor * 100 + (int(frac.ljust(2, "0")) if frac else 0)
