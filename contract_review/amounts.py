from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_decimal(value: str) -> Decimal | None:
    cleaned = re.sub(r"[￥¥,$，\s元]", "", value.strip())
    cleaned = cleaned.replace("％", "%")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if not cleaned or cleaned in {".", "-", "-."}:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


_DIGITS = {"零": 0, "〇": 0, "壹": 1, "一": 1, "贰": 2, "貳": 2, "二": 2,
           "叁": 3, "參": 3, "三": 3, "肆": 4, "四": 4, "伍": 5, "五": 5,
           "陆": 6, "陸": 6, "六": 6, "柒": 7, "七": 7, "捌": 8, "八": 8,
           "玖": 9, "九": 9}
_SMALL = {"拾": 10, "十": 10, "佰": 100, "百": 100, "仟": 1000, "千": 1000}
_LARGE = {"万": 10_000, "萬": 10_000, "亿": 100_000_000, "億": 100_000_000}


def _integer_chinese(text: str) -> int:
    total = section = number = 0
    for char in text:
        if char in _DIGITS:
            number = _DIGITS[char]
        elif char in _SMALL:
            unit = _SMALL[char]
            section += (number or 1) * unit
            number = 0
        elif char in _LARGE:
            section += number
            total += section * _LARGE[char]
            section = number = 0
    return total + section + number


def chinese_money_to_decimal(text: str) -> Decimal | None:
    """Parse common financial uppercase RMB text into a two-decimal Decimal."""
    normalized = re.sub(r"[\s,，人民币￥¥圆正]", "", text)
    normalized = normalized.replace("圆", "元")
    if not any(c in normalized for c in _DIGITS):
        return None
    yuan_text = normalized.split("元", 1)[0] if "元" in normalized else normalized
    fraction_text = normalized.split("元", 1)[1] if "元" in normalized else ""
    yuan = _integer_chinese(yuan_text)
    jiao = fen = 0
    match = re.search(r"([零〇壹一贰貳二叁參三肆四伍五陆陸六柒七捌八玖九])角", fraction_text)
    if match:
        jiao = _DIGITS[match.group(1)]
    match = re.search(r"([零〇壹一贰貳二叁參三肆四伍五陆陸六柒七捌八玖九])分", fraction_text)
    if match:
        fen = _DIGITS[match.group(1)]
    return money(Decimal(yuan) + Decimal(jiao) / 10 + Decimal(fen) / 100)


def find_uppercase_amount(text: str) -> Decimal | None:
    pattern = r"(?:人民币)?[零〇壹一贰貳二叁參三肆四伍五陆陸六柒七捌八玖九拾十佰百仟千万萬亿億\s]+元(?:[零〇壹一贰貳二叁參三肆四伍五陆陸六柒七捌八玖九]角)?(?:[零〇壹一贰貳二叁參三肆四伍五陆陸六柒七捌八玖九]分)?[整正]?"
    match = re.search(pattern, text)
    return chinese_money_to_decimal(match.group(0)) if match else None

