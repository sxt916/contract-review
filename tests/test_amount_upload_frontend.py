import json
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
SCRIPT = Path(__file__).parents[1] / "contract_review" / "static" / "amount-review.js"


def _choose(files: list[dict]) -> dict:
    if not NODE:
        pytest.skip("Node.js is required for frontend validation tests")
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {innerHTML:'', textContent:'', className:'', disabled:false, addEventListener(){}});
  return elements.get(selector);
}
const context = {
  document: {querySelector: element},
  sessionStorage: {getItem(){return null}, setItem(){}},
  setupDropzone(){},
  setMessage(text){context.lastMessage=text},
  lastMessage:'',
  esc(value){return String(value)},
  api: async()=>({}),
  formatTime(){return ''},
  setTimeout(){},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.testFiles = JSON.parse(process.argv[2]);
const result = vm.runInContext(`choose(testFiles); JSON.stringify({
  selectedNames: selected.map(file => file.name),
  message: lastMessage,
  disabled: document.querySelector('#upload').disabled
})`, context);
process.stdout.write(result);
"""
    completed = subprocess.run(
        [NODE, "-e", harness, str(SCRIPT), json.dumps(files, ensure_ascii=False)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_amount_picker_keeps_valid_docx_and_explains_each_rejection():
    result = _choose([
        {"name": "有效合同.docx", "size": 1024},
        {"name": "旧合同.doc", "size": 1024},
        {"name": "报价单.pdf", "size": 1024},
        {"name": "超大合同.docx", "size": 21 * 1024 * 1024},
    ])

    assert result["selectedNames"] == ["有效合同.docx"]
    assert "旧合同.doc：旧版 Word 格式" in result["message"]
    assert "报价单.pdf：仅支持 .docx 格式" in result["message"]
    assert "超大合同.docx：文件大小 21.00 MB，超过 20 MB 限制" in result["message"]
    assert result["disabled"] is False


def test_amount_picker_accepts_first_ten_and_names_each_extra_file():
    files = [{"name": f"合同{index}.docx", "size": 1024} for index in range(1, 12)]

    result = _choose(files)

    assert result["selectedNames"] == [f"合同{index}.docx" for index in range(1, 11)]
    assert "合同11.docx：超过一次最多 10 份的限制" in result["message"]


def test_amount_picker_silently_ignores_hidden_and_word_temporary_files():
    result = _choose([
        {"name": "有效合同.docx", "size": 1024},
        {"name": ".DS_Store", "size": 1024},
        {"name": "._合同副本.docx", "size": 1024},
        {"name": ".隐藏合同.docx", "size": 1024},
        {"name": "~$有效合同.docx", "size": 1024},
    ])

    assert result["selectedNames"] == ["有效合同.docx"]
    assert result["message"] == ""
