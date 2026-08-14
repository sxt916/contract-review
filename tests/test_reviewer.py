from pathlib import Path

from docx import Document

from contract_review.reviewer import review_docx


def make_contract(path: Path, wrong_line: bool = False, payment: str | None = None):
    doc = Document()
    doc.add_paragraph("合同编号：NPA2026-XWZCG-1874")
    doc.add_paragraph("第一条 合同标的")
    table = doc.add_table(rows=1, cols=4)
    for cell, value in zip(table.rows[0].cells, ["名称", "数量", "单价（元）", "总价（元）"]):
        cell.text = value
    values = [["商品A", "2", "10.005", "20.00" if wrong_line else "20.01"], ["商品B", "1", "79.99", "79.99"]]
    for values_row in values:
        cells = table.add_row().cells
        for cell, value in zip(cells, values_row):
            cell.text = value
    cells = table.add_row().cells
    for cell, value in zip(cells, ["合计", "", "", "100.00"]):
        cell.text = value
    doc.add_paragraph("第八条 结算方式及期限")
    doc.add_paragraph(payment or "1、验收后支付100%，即人民币100.00元（人民币壹佰元整）。")
    doc.add_paragraph("第九条 违约责任")
    doc.add_paragraph("违约金为合同金额的10%。")
    doc.save(path)


def test_valid_contract(tmp_path):
    path = tmp_path / "contract.docx"
    make_contract(path)
    result = review_docx(path)
    assert result["status"] == "passed"
    assert result["contract_no"] == "NPA2026-XWZCG-1874"


def test_wrong_line_and_total(tmp_path):
    path = tmp_path / "contract.docx"
    make_contract(path, wrong_line=True)
    result = review_docx(path)
    types = {error["error_type"] for error in result["errors"]}
    assert "明细计算不一致" in types
    assert "明细合计不一致" in types


def test_fixed_and_ratio_mismatch(tmp_path):
    path = tmp_path / "contract.docx"
    make_contract(path, payment="1、预付款50%，即40.00元；2、验收款60.00元。")
    result = review_docx(path)
    assert any(e["error_type"] == "付款比例与金额不一致" for e in result["errors"])
