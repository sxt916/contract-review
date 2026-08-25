from __future__ import annotations

import re
import unicodedata
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

PARSER_VERSION = "1.3.0"


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


def _normalize_header(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[\s()（）\[\]【】/\\:_-]", "", normalized)


def _header_indexes(row: list[str]) -> dict[str, int] | None:
    aliases = {
        "unit_price": ("单价", "含税单价", "结算单价"),
        "total": ("总价", "金额", "小计", "价款"),
    }
    found: dict[str, int] = {}
    for key, names in aliases.items():
        for index, value in enumerate(row):
            compact = _normalize_header(value)
            if any(_normalize_header(name) in compact for name in names):
                found[key] = index
                break
    return found if "total" in found else None


def _basis_candidates(row: list[str]) -> list[dict[str, str | int]]:
    groups = (
        ("quantity", ("数量", "数目", "件数")),
        ("weight", ("理论重量", "结算重量", "实际重量", "净重", "总重量", "重量")),
    )
    candidates: list[dict[str, str | int]] = []
    for index, value in enumerate(row):
        compact = _normalize_header(value)
        for kind, aliases in groups:
            if any(_normalize_header(alias) in compact for alias in aliases):
                candidates.append({"index": index, "label": value.strip() or aliases[0], "kind": kind})
                break
    return candidates


def _basis_stats(rows: list[list[str]], start_at: int, candidate_index: int, unit_index: int, total_index: int) -> tuple[int, int]:
    valid = matches = 0
    for row in rows[start_at:]:
        if re.search(r"合\s*计|总\s*计", " ".join(row)) or max(candidate_index, unit_index, total_index) >= len(row):
            continue
        basis = parse_decimal(row[candidate_index])
        unit_price = parse_decimal(row[unit_index])
        total = parse_decimal(row[total_index])
        if basis is None or unit_price is None or total is None:
            continue
        valid += 1
        if money(basis * unit_price) == money(total):
            matches += 1
    return valid, matches


def _select_basis(rows: list[list[str]], header_at: int, header: dict[str, int]) -> dict[str, str | int] | None:
    if "unit_price" not in header:
        return None
    candidates = _basis_candidates(rows[header_at])
    scored: list[dict[str, str | int]] = []
    for candidate in candidates:
        valid, matches = _basis_stats(rows, header_at + 1, int(candidate["index"]), header["unit_price"], header["total"])
        scored.append({**candidate, "valid": valid, "matches": matches})
    exact = [candidate for candidate in scored if candidate["kind"] == "quantity"]
    if exact:
        return max(exact, key=lambda candidate: (int(candidate["matches"]), int(candidate["valid"])))
    inferred = [candidate for candidate in scored if int(candidate["valid"]) >= 2 and int(candidate["matches"]) / int(candidate["valid"]) >= 0.8]
    return max(inferred, key=lambda candidate: (int(candidate["matches"]) / int(candidate["valid"]), int(candidate["matches"]))) if inferred else None


def _display_row(row: list[str]) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for value in row:
        compact = re.sub(r"\s+", " ", value).strip()
        if compact and compact not in seen:
            seen.add(compact)
            values.append(compact)
    return "；".join(values)


def _row_identity(header_row: list[str], row: list[str]) -> str:
    identities: list[str] = []
    for index, header in enumerate(header_row):
        compact = _normalize_header(header)
        if index < len(row) and row[index].strip() and any(name in compact for name in ("工程号", "产品名称", "名称", "图号")):
            identities.append(f"{header.strip()} {row[index].strip()}")
        if len(identities) == 2:
            break
    return f"（{'，'.join(identities)}）" if identities else ""


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


def _currency_amounts(text: str) -> list[Decimal]:
    """Return numeric amounts explicitly tied to a currency marker or 元."""
    results: list[Decimal] = []
    pattern = r"(?:人民币\s*)?(?:(?:￥|¥)\s*)?[【\[（(]?\s*-?\d[\d,，]*(?:\.\d+)?\s*[】\]）)]?\s*元|(?:￥|¥)\s*[【\[（(]?\s*-?\d[\d,，]*(?:\.\d+)?\s*[】\]）)]?"
    for raw in re.findall(pattern, text):
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
        errors.append(ReviewError(
            location,
            "金额大小写不一致",
            f"阿拉伯数字 {money(arabic):.2f} 元；中文大写 {uppercase:.2f} 元",
            "阿拉伯数字与中文大写金额应一致，请人工核对正确金额",
            "当前只有阿拉伯数字和中文大写两项依据，系统无法自动确定哪一项正确",
        ))


def _check_explicit_amount_pairs(text: str, location: str, errors: list[ReviewError]) -> None:
    """Check unambiguous Arabic/uppercase pairs without guessing among amounts."""
    for clause in re.split(r"[；;。\n]+", text):
        amounts = _currency_amounts(clause)
        if len(amounts) == 1 and find_uppercase_amount(clause) is not None:
            _check_uppercase(clause, amounts[0], location, errors)


def _payment_percentages(text: str) -> list[Decimal]:
    """Return percentages that describe payment rather than unrelated rates."""
    excluded_before = re.compile(r"(?:税率|容差率|违约金(?:比例)?|利率|折扣率|质保期)\s*(?:为|是|按)?\s*$")
    excluded_after = re.compile(r"^\s*(?:的|全额)?\s*(?:增值税|税款|税费|税率|容差率|违约金|利率|折扣率|质保期)")
    percentages: list[Decimal] = []
    for match in re.finditer(r"(\d+(?:[.,，]\d+)?)\s*[%％]", text):
        before = text[max(0, match.start() - 12):match.start()]
        after = text[match.end():match.end() + 12]
        if excluded_before.search(before) or excluded_after.search(after):
            continue
        percentages.append(Decimal(match.group(1).replace("，", ".").replace(",", ".")))
    return percentages


def _parse_payments(text: str, contract_total: Decimal, location: str, errors: list[ReviewError]) -> list[Decimal]:
    payments: list[Decimal] = []
    # Keep comma-linked expressions together (for example "50%，即40元").
    segments = [s.strip() for s in re.split(r"[；;。\n]+", text) if s.strip()]
    payment_words = r"支付|付款|预付款|进度款|验收款|尾款|余款|剩余款项|结算"
    for index, segment in enumerate(segments, 1):
        if not re.search(payment_words, segment):
            continue
        item_location = f"{location}，第{index}笔付款"
        percentages = _payment_percentages(segment)
        amounts = _payment_amounts(re.sub(r"\d+(?:[.,，]\d+)?\s*[%％]", "", segment))
        if amounts:
            payments.extend(amounts)
            if len(amounts) == 1 and len(percentages) == 1:
                proportional = money(contract_total * percentages[0] / 100)
                if amounts[0] != proportional:
                    errors.append(ReviewError(item_location, "付款比例与金额不一致", segment, f"{percentages[0]}% × {contract_total:.2f} = {proportional:.2f} 元", "同一笔付款写明的固定金额与比例换算结果不一致"))
        else:
            payments.extend(money(contract_total * percentage / 100) for percentage in percentages)
    return payments


def review_docx(path: str | Path, filename: str | None = None) -> dict:
    document = Document(str(path))
    blocks = list(iter_blocks(document))
    paragraph_texts = [b.text.strip() for b in blocks if isinstance(b, Paragraph) and b.text.strip()]
    errors: list[ReviewError] = []
    line_totals: list[Decimal] = []
    contract_total: Decimal | None = None
    contract_total_text: str | None = None
    contract_total_location = "合同标的，合计"
    contract_total_uppercase: Decimal | None = None
    current_section = "合同标的"
    checks: list[str] = []

    for text in paragraph_texts:
        _check_explicit_amount_pairs(text, "合同正文", errors)
        if contract_total is None and re.search(r"合同总额|合同金额|总金额|价税合计|总价", text):
            amounts = _currency_amounts(text)
            if amounts:
                contract_total = amounts[0]

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
        header_row = rows[header_at]
        basis = _select_basis(rows, header_at, header)
        if basis is not None:
            checks.append(
                f"{basis['label']} × {header_row[header['unit_price']]} = {header_row[header['total']]}"
                f"（{basis['matches']}/{basis['valid']} 项吻合）"
            )
        for row_index, row in enumerate(rows[header_at + 1:], 1):
            joined = _display_row(row)
            if re.search(r"合\s*计|总\s*计", joined):
                total_text = row[header["total"]] if header["total"] < len(row) else joined
                candidates = _currency_amounts(total_text) or _arabic_amounts(total_text)
                if candidates:
                    contract_total = candidates[0]
                    contract_total_text = joined
                    contract_total_location = f"{current_section}，合计"
                    contract_total_uppercase = find_uppercase_amount(joined)
                continue
            if header["total"] >= len(row):
                continue
            total = parse_decimal(row[header["total"]])
            if total is None:
                continue
            total = money(total)
            line_totals.append(total)
            if basis is not None and "unit_price" in header and max(int(basis["index"]), header["unit_price"], header["total"]) < len(row):
                quantity = parse_decimal(row[int(basis["index"])])
                unit_price = parse_decimal(row[header["unit_price"]])
                if quantity is not None and unit_price is not None:
                    expected = money(quantity * unit_price)
                    if total != expected:
                        identity = _row_identity(header_row, row)
                        errors.append(ReviewError(
                            f"{current_section}，第{row_index}项明细{identity}",
                            "明细计算不一致",
                            f"{basis['label']} {quantity}，{header_row[header['unit_price']]} {unit_price}，{header_row[header['total']]} {total:.2f} 元",
                            f"{quantity} × {unit_price} = {expected:.2f} 元",
                            f"{basis['label']}乘以{header_row[header['unit_price']]}并四舍五入到分后与行总价不一致",
                        ))

    if contract_total is not None and line_totals:
        actual = money(sum(line_totals, Decimal("0")))
        checks.append(f"{len(line_totals)} 项分项总价求和 = 合同合计")
        if contract_total_uppercase is not None and not (actual == contract_total == contract_total_uppercase):
            original = f"分项合计 {actual:.2f} 元；合计栏阿拉伯数字 {contract_total:.2f} 元；中文大写 {contract_total_uppercase:.2f} 元"
            if actual == contract_total_uppercase:
                expected = f"合计栏阿拉伯数字疑似应为 {actual:.2f} 元"
                reason = "分项总价之和与中文大写金额一致，合计栏阿拉伯数字与二者不一致"
            elif actual == contract_total:
                expected = f"中文大写金额疑似应对应 {actual:.2f} 元"
                reason = "分项总价之和与合计栏阿拉伯数字一致，中文大写金额与二者不一致"
            elif contract_total == contract_total_uppercase:
                expected = "请核对各分项总价，分项合计应与合计金额一致"
                reason = "合计栏阿拉伯数字与中文大写金额一致，分项总价之和与二者不一致"
            else:
                expected = "三个金额互不一致，请人工核对正确金额"
                reason = "分项总价之和、合计栏阿拉伯数字与中文大写金额均不一致，系统无法自动确定正确值"
            errors.append(ReviewError(contract_total_location, "合计金额不一致", original, expected, reason))
        elif actual != contract_total:
            expression = " + ".join(f"{value:.2f}" for value in line_totals)
            difference = abs(actual - contract_total)
            errors.append(ReviewError(
                "合同标的，合计",
                "明细合计不一致",
                f"分项总价：{expression} = {actual:.2f} 元；合同合计：{contract_total:.2f} 元",
                f"两者应一致（当前差额 {difference:.2f} 元）",
                "所有有效商品行总价之和与最终合计金额不一致",
            ))
    elif contract_total is not None and contract_total_uppercase is not None and contract_total_uppercase != contract_total:
        _check_uppercase(contract_total_text or "", contract_total, contract_total_location, errors)

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
        if payment_texts:
            payments: list[Decimal] = []
            for location, text in payment_texts:
                payments.extend(_parse_payments(text, contract_total, location, errors))
            if payments:
                total_paid = money(sum(payments, Decimal("0")))
                checks.append(f"{len(payments)} 笔付款金额求和 = 合同总额")
                if total_paid != contract_total:
                    errors.append(ReviewError(payment_location, "付款合计不一致", f"付款合计 {total_paid:.2f} 元，合同总额 {contract_total:.2f} 元", f"付款合计应为 {contract_total:.2f} 元", "所有实际付款项之和与合同总额不一致"))

    unique_errors: list[ReviewError] = []
    seen_errors: set[tuple[str, str, str, str, str]] = set()
    for error in errors:
        key = (error.location, error.error_type, error.original, error.expected, error.reason)
        if key not in seen_errors:
            seen_errors.add(key)
            unique_errors.append(error)
    unique_checks = list(dict.fromkeys(checks))

    return {
        "contract_no": extract_contract_no(filename or Path(path).name, paragraph_texts),
        "status": "passed" if not unique_errors else "failed_review",
        "errors": [asdict(error) for error in unique_errors],
        "summary": {"line_count": len(line_totals), "contract_total": f"{contract_total:.2f}" if contract_total is not None else None, "checks": unique_checks},
        "parser_version": PARSER_VERSION,
    }
