from contract_review.pairing import pair_files


def test_contract_number_has_priority():
    result = pair_files(["甲公司-NPA2025-XPJCG-1554.docx"], ["乙公司-NPA2025-XPJCG-1554-盖章扫描件.pdf"])
    assert len(result["pairs"]) == 1
    assert result["pairs"][0]["method"] == "contract_no"


def test_normalized_filename_pairing():
    result = pair_files(["办公用品采购合同（定稿）.docx"], ["办公用品采购合同-盖章扫描版.pdf"])
    assert len(result["pairs"]) == 1


def test_duplicate_number_is_conflict():
    result = pair_files(["NPA2025-XPJCG-1554-a.docx", "NPA2025-XPJCG-1554-b.docx"], ["NPA2025-XPJCG-1554.pdf"])
    assert result["conflicts"]
    assert not result["pairs"]
