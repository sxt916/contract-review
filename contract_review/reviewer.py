from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from docx import Document
from docx.document import Document as DocumentType
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P

from .amounts import find_uppercase_amount, money, parse_decimal

PARSER_VERSION = "1.0.0"


@dataclass
class ReviewError:
    location: str
    error_type: str
    original: str
    expected: str
    reason: str


def iter_blocks(document: DocumentType) -> Iterator[Paragraph | Table]:
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def _cell_text(cell) -> str:
    return " ".join(p.text.strip() for p in cell.paragraphs if p.text.strip()).strip()


def _unique_rows(table: Table) -> list[list[str]]:
    rows: list[list[str]] = []
    previous: list[str] | None = None
    for row in table.rows:
        current = [_cell_text(cell) for cell in row.cells]
        if current != previous:
            rows.append(current)
        previous = current
    return rows


def _header_indexes(row: list[str]) -> dict[str, int] | None:
    aliases = {
        "quantity": ("数量", "数 量"),
        "unit_price": ("单价", "单价（元）", "单价(元)", "含税单价"),
        "total": ("总价", "总价（元）", "总价(元)", "金额", "小计"),
    }
    found: dict[str, int] = {}
    for key, names in aliases.items():
        for index, value in enumerate(row):
            compact = re.sub(r"\s", "", value)
            if any(re.sub(r"\s", "", name) in compact for name in names):
                found[key] = index
                break
    return found if len(found) == 3 else None


def _section_title(text: str) -> str | None:
    match = re.match(r"^\s*第([一二三四五六七八九十百零〇0-9]+)条\s*(.*)", text)
    return f"第{match.group(1)}条 {match.group(2).strip()}".strip() if match else None


def extract_contract_no(filename: str, texts: list[str] | None = None) -> str | None:
    sources = [filename, *(texts or [])]
    patterns = [r"\b(NPA\d{4}[-_][A-Z0-9]+[-_]\d+)\b", r"合同编号\s*[：:]\s*([A-Z0-9][A-Z0-9_\-/]{5,})"]
    for source in sources:
        upper = source.upper()
        for pattern in patterns:
            match = re.search(pattern, upper)
            if match:
                return match.group(1).replace("_", "-")
    return None


def _arabic_amounts(text: str) -> list[Decimal]:
    results: list[Decimal] = []
    for raw in re.findall(r"(?:￥|¥)?\s*-?\d[\d,，]*(?:\.\d+)?\s*元?", text):
        value = parse_decimal(raw)
        if value is not None:
            results.append(money(value))
    return results


def _payment_amounts(text: str) -> list[Decimal]:
    """Only accept currency-marked values so item numbers/days are never payments."""
    results: list[Decimal] = []
    pattern = r"(?:人民币\s*)?(?:￥|¥)\s*\d[\d,，]*(?:\.\d+)?|\d[\d,，]*(?:\.\d+)?\s*元"
    for raw in re.findall(pattern, text):
        value = parse_decimal(raw)
        if value is not None:
            results.append(money(value))
    return results


def _check_uppercase(text: str, arabic: Decimal, location: str, errors: list[ReviewError]) -> None:
    uppercase = find_uppercase_amount(text)
    if uppercase is not None and uppercase != money(arabic):
        errors.append(ReviewError(location, "中文大写金额不一致", text, f"中文大写应对应 {money(arabic):.2f} 元", "阿拉伯数字金额与同一表达中的中文大写金额不一致"))


def _parse_payments(text: str, contract_total: Decimal, location: str, errors: list[ReviewError]) -> list[Decimal]:
    payments: list[Decimal] = []
    # Split clauses, but never split at decimal points inside monetary values.
    segments = [s.strip() for s in re.split(r"[；;\n]+|(?<=。)\s*(?=\d+[、])|(?<=。)\s*(?=（\d+）)", text) if s.strip()]
    payment_words = r"支付|付款|预付款|进度款|验收款|尾款|余款|剩余款项|结算"
    for index, segment in enumerate(segments, 1):
        if not re.search(payment_words, segment):
            continue
        item_location = f"{location}，第{index}笔付款"
        percentages = [Decimal(p.replace("，", ".")) for p in re.findall(r"(\d+(?:[.,，]\d+)?)\s*[%％]", segment)]
        amounts = _payment_amounts(re.sub(r"\d+(?:[.,，]\d+)?\s*[%％]", "", segment))
        fixed = amounts[0] if amounts else None
        proportional = money(contract_total * percentages[0] / 100) if percentages else None
        if fixed is not None:
            payments.append(fixed)
            _check_uppercase(segment, fixed, item_location, errors)
            if proportional is not None and fixed != proportional:
                errors.append(ReviewError(item_location, "付款比例与金额不一致", segment, f"{percentages[0]}% × {contract_total:.2f} = {proportional:.2f} 元", "同一笔付款写明的固定金额与比例换算结果不一致"))
        elif proportional is not None:
            payments.append(proportional)
        elif re.search(r"尾款|余款|剩余款项", segment):
            errors.append(ReviewError(item_location, "付款金额不明确", segment, "请明确填写金额或付款比例", "实际付款表述缺少可计算的金额或比例"))
    return payments


def review_docx(path: str | Path, filename: str | None = None) -> dict:
    document = Document(str(path))
    blocks = list(iter_blocks(document))
    paragraph_texts = [b.text.strip() for b in blocks if isinstance(b, Paragraph) and b.text.strip()]
    errors: list[ReviewError] = []
    line_totals: list[Decimal] = []
    contract_total: Decimal | None = None
    current_section = "合同标的"
    tables_found = 0

    for block in blocks:
        if isinstance(block, Paragraph):
            title = _section_title(block.text)
            if title:
                current_section = title
            continue
        rows = _unique_rows(block)
        header_at = header = None
        for index, row in enumerate(rows[:5]):
            header = _header_indexes(row)
            if header:
                header_at = index
                break
        if header_at is None or header is None:
            continue
        tables_found += 1
        for row_index, row in enumerate(rows[header_at + 1:], 1):
            joined = " ".join(row)
            if re.search(r"合\s*计|总\s*计", joined):
                candidates = _arabic_amounts(row[header["total"]] if header["total"] < len(row) else joined)
                if candidates:
                    contract_total = candidates[-1]
                    _check_uppercase(joined, contract_total, f"{current_section}，合计", errors)
                continue
            if max(header.values()) >= len(row):
                continue
            quantity = parse_decimal(row[header["quantity"]])
            unit_price = parse_decimal(row[header["unit_price"]])
            total = parse_decimal(row[header["total"]])
            if quantity is None or unit_price is None or total is None:
                continue
            total = money(total)
            expected = money(quantity * unit_price)
            line_totals.append(total)
            if total != expected:
                errors.append(ReviewError(f"{current_section}，第{row_index}项明细，总价", "明细计算不一致", f"数量 {quantity}，单价 {unit_price} 元，总价 {total:.2f} 元", f"{quantity} × {unit_price} = {expected:.2f} 元", "数量乘以单价并四舍五入到分后与行总价不一致"))

    if not tables_found:
        errors.append(ReviewError("合同标的", "无法识别明细表", "未找到数量、单价、总价列完整的表格", "请使用可编辑的标准商品明细表", "缺少必要表头"))
    if contract_total is None:
        errors.append(ReviewError("合同标的，合计", "无法识别合同总额", "未找到最终合计金额", "请在明细表合计行填写阿拉伯数字金额", "缺少可计算的合同总额"))
    elif money(sum(line_totals, Decimal("0"))) != contract_total:
        actual = money(sum(line_totals, Decimal("0")))
        errors.append(ReviewError("合同标的，合计", "明细合计不一致", f"明细行总价之和 {actual:.2f} 元，合同合计 {contract_total:.2f} 元", f"合同合计应为 {actual:.2f} 元", "所有有效商品行总价之和与最终合计金额不一致"))

    payment_texts: list[tuple[str, str]] = []
    in_payment = False
    payment_location = "结算方式及期限"
    for text in paragraph_texts:
        title = _section_title(text)
        if title:
            if re.search(r"结算|支付|付款", title):
                in_payment = True
                payment_location = title
            elif in_payment:
                in_payment = False
        if in_payment:
            payment_texts.append((payment_location, text))
        elif re.search(r"具体支付节点", text):
            payment_texts.append(("具体支付节点", text))

    if contract_total is not None:
        if not payment_texts:
            errors.append(ReviewError("结算方式及期限", "无法识别付款区域", "未找到付款章节或具体支付节点", "请明确填写付款安排", "无法安全提取实际付款项"))
        else:
            payments: list[Decimal] = []
            for location, text in payment_texts:
                payments.extend(_parse_payments(text, contract_total, location, errors))
            if not payments and not any(e.error_type == "付款金额不明确" for e in errors):
                errors.append(ReviewError(payment_location, "无法识别付款金额", "付款区域内未识别到有效金额或比例", "请明确填写每笔付款金额或比例", "无法计算付款合计"))
            elif payments and money(sum(payments, Decimal("0"))) != contract_total:
                total_paid = money(sum(payments, Decimal("0")))
                errors.append(ReviewError(payment_location, "付款合计不一致", f"付款合计 {total_paid:.2f} 元，合同总额 {contract_total:.2f} 元", f"付款合计应为 {contract_total:.2f} 元", "所有实际付款项之和与合同总额不一致"))

    return {
        "contract_no": extract_contract_no(filename or Path(path).name, paragraph_texts),
        "status": "passed" if not errors else "failed_review",
        "errors": [asdict(error) for error in errors],
        "summary": {"line_count": len(line_totals), "contract_total": f"{contract_total:.2f}" if contract_total is not None else None},
        "parser_version": PARSER_VERSION,
    }
