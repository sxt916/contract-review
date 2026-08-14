from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from .reviewer import extract_contract_no


def normalized_stem(filename: str) -> str:
    value = Path(filename).stem.upper()
    value = re.sub(r"单章|盖章|扫描件|扫描版|签署版|定稿|最终版", "", value)
    return re.sub(r"[\s()（）\[\]【】._\-·]", "", value)


def pair_files(word_names: list[str], pdf_names: list[str]) -> dict:
    words = [{"index": i, "filename": name, "contract_no": extract_contract_no(name)} for i, name in enumerate(word_names)]
    pdfs = [{"index": i, "filename": name, "contract_no": extract_contract_no(name)} for i, name in enumerate(pdf_names)]
    pairs: list[dict] = []
    used_words: set[int] = set()
    used_pdfs: set[int] = set()

    # Contract number is authoritative, but duplicate numbers must remain conflicts.
    numbers = {item["contract_no"] for item in words + pdfs if item["contract_no"]}
    conflicts: list[dict] = []
    for number in sorted(numbers):
        ws = [item for item in words if item["contract_no"] == number]
        ps = [item for item in pdfs if item["contract_no"] == number]
        if len(ws) == len(ps) == 1:
            pairs.append({"word_index": ws[0]["index"], "pdf_index": ps[0]["index"], "contract_no": number, "confidence": 1.0, "method": "contract_no"})
            used_words.add(ws[0]["index"]); used_pdfs.add(ps[0]["index"])
        elif ws and ps:
            conflicts.append({"contract_no": number, "word_indices": [x["index"] for x in ws], "pdf_indices": [x["index"] for x in ps], "reason": "合同编号重复"})
            used_words.update(x["index"] for x in ws); used_pdfs.update(x["index"] for x in ps)

    remaining_words = [x for x in words if x["index"] not in used_words]
    remaining_pdfs = [x for x in pdfs if x["index"] not in used_pdfs]
    candidates: list[tuple[float, int, int]] = []
    for word in remaining_words:
        for pdf in remaining_pdfs:
            score = SequenceMatcher(None, normalized_stem(word["filename"]), normalized_stem(pdf["filename"])).ratio()
            candidates.append((score, word["index"], pdf["index"]))
    for score, wi, pi in sorted(candidates, reverse=True):
        if score < 0.72 or wi in used_words or pi in used_pdfs:
            continue
        competing = [c[0] for c in candidates if (c[1] == wi or c[2] == pi) and (c[1], c[2]) != (wi, pi)]
        if competing and score - max(competing) < 0.08:
            continue
        pairs.append({"word_index": wi, "pdf_index": pi, "contract_no": words[wi]["contract_no"] or pdfs[pi]["contract_no"], "confidence": round(score, 3), "method": "filename"})
        used_words.add(wi); used_pdfs.add(pi)

    return {
        "pairs": sorted(pairs, key=lambda x: x["word_index"]),
        "unmatched_words": [x for x in words if x["index"] not in used_words],
        "unmatched_pdfs": [x for x in pdfs if x["index"] not in used_pdfs],
        "conflicts": conflicts,
    }

