from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path(__file__).parents[1] / "contract_review" / "static"
SCRIPT = STATIC / "contract-check.js"
NODE = shutil.which("node")


class InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("id"):
            self.inputs[attributes["id"]] = attributes


def test_combined_page_has_independent_file_and_folder_pickers():
    parser = InputParser()
    parser.feed((STATIC / "contract-check.html").read_text())

    assert set(parser.inputs) >= {"words", "word-folder", "pdfs", "pdf-folder"}
    assert "webkitdirectory" not in parser.inputs["words"]
    assert "webkitdirectory" not in parser.inputs["pdfs"]
    assert "webkitdirectory" in parser.inputs["word-folder"]
    assert "webkitdirectory" in parser.inputs["pdf-folder"]
    assert "directory" in parser.inputs["word-folder"]
    assert "directory" in parser.inputs["pdf-folder"]


def run_selection_scenario() -> dict:
    if not NODE:
        pytest.skip("Node.js is required for frontend validation tests")
    assert SCRIPT.exists(), "combined frontend must have its own contract-check.js"
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    innerHTML:'', textContent:'', className:'', disabled:false, checked:false,
    addEventListener(){}, classList:{add(){},remove(){},contains(){return false}}
  });
  return elements.get(selector);
}
const context = {
  document: {
    body:{style:{}}, querySelector:element, querySelectorAll(){return []},
    addEventListener(){},
  },
  sessionStorage:{getItem(){return null},setItem(){}},
  setupDropzone(){}, setMessage(){}, esc(value){return String(value)},
  api:async()=>({items:[]}), formatTime(){return ''}, setTimeout(){},
  confirm(){return true}, FormData:function(){this.append=()=>{}},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const result = vm.runInContext(`(() => {
  choose('words', [{name:'A.docx',size:1},{name:'B.docx',size:1}]);
  choose('pdfs', [{name:'A.pdf',size:1}]);
  const defaults = {words:words.length,pdfs:pdfs.length};
  toggleFile('words',1,false);
  const afterUncheck = words.map(file=>file.name);
  toggleAll('words');
  currentPairs=[{word_index:0,pdf_index:0}];
  return {defaults,afterUncheck,afterSelectAll:words.map(file=>file.name),selection:submissionSelection()};
})()`, context);
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        [NODE, "-e", harness, str(SCRIPT)], check=True, capture_output=True, text=True
    )
    return json.loads(completed.stdout)


def test_combined_picker_defaults_all_files_and_allows_reselection():
    result = run_selection_scenario()

    assert result["defaults"] == {"words": 2, "pdfs": 1}
    assert result["afterUncheck"] == ["A.docx"]
    assert result["afterSelectAll"] == ["A.docx", "B.docx"]


def test_combined_submission_includes_unmatched_word_indexes():
    result = run_selection_scenario()

    assert result["selection"] == {
        "pairs": [{"word_index": 0, "pdf_index": 0}],
        "unmatchedWordIndices": [1],
    }


def test_standalone_pages_keep_their_original_scripts_and_results():
    comparison = (STATIC / "comparison.html").read_text()
    results = (STATIC / "comparison-results.html").read_text()

    assert 'src="comparison.js' in comparison
    assert "contract-check.js" not in comparison
    assert 'href="comparison-results.html"' in comparison
    assert 'src="comparison-results.js' in results
    assert "/api/combined-checks" not in (STATIC / "comparison-results.js").read_text()
