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
        self.file_labels: list[tuple[str | None, str]] = []
        self._label_for: str | None = None
        self._label_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("id"):
            self.inputs[attributes["id"]] = attributes
        if tag == "label" and attributes.get("for"):
            self._label_for = attributes["for"]
            self._label_text = []

    def handle_data(self, data):
        if self._label_for:
            self._label_text.append(data)

    def handle_endtag(self, tag):
        if tag == "label" and self._label_for:
            self.file_labels.append((self._label_for, "".join(self._label_text).strip()))
            self._label_for = None
            self._label_text = []


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


@pytest.mark.parametrize(
    "page,expected_labels",
    [
        (
            "comparison.html",
            {
                ("words", "选择文件"), ("word-folder", "选择文件夹"),
                ("pdfs", "选择文件"), ("pdf-folder", "选择文件夹"),
            },
        ),
        ("amount-review.html", {("files", "选择文件"), ("folder", "选择文件夹")}),
        (
            "contract-check.html",
            {
                ("words", "选择文件"), ("word-folder", "选择文件夹"),
                ("pdfs", "选择文件"), ("pdf-folder", "选择文件夹"),
            },
        ),
    ],
)
def test_every_upload_area_exposes_file_and_folder_buttons(page, expected_labels):
    parser = InputParser()
    parser.feed((STATIC / page).read_text())

    assert expected_labels <= set(parser.file_labels)


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


@pytest.mark.parametrize(
    "script_name,colspan",
    [("contract-check.js", 6), ("contract-check-results.js", 7)],
)
def test_failed_review_renders_compact_trigger_and_full_width_detail_row(script_name, colspan):
    if not NODE:
        pytest.skip("Node.js is required for frontend validation tests")
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    innerHTML:'', textContent:'', className:'', disabled:false, checked:false, hidden:false,
    addEventListener(){}, classList:{add(){},remove(){},contains(){return false}},
    reset(){}, querySelectorAll(){return []}, setAttribute(){},
  });
  return elements.get(selector);
}
const context = {
  document: {
    body:{style:{}}, querySelector:element, querySelectorAll(){return []},
    addEventListener(){},
  },
  sessionStorage:{getItem(){return null},setItem(){}},
  labels:{failed_review:'审核未通过'}, setMessage(){}, esc(value){return String(value)},
  api:async()=>({items:[],total:0,page:1,page_size:20}), formatTime(){return 'time'},
  setTimeout(){}, confirm(){return true}, URLSearchParams,
  FormData:function(){this.append=()=>{}},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const task = {
  id:'task-1', word_filename:'A.docx', pdf_filename:'A.pdf',
  comparison_status:'completed', review_status:'failed_review', similarity:0.9,
  created_at:'2026-08-27T00:00:00Z', review_errors:[
    {location:'第一条',error_type:'金额错误',original:'100',expected:'200',reason:'不一致'},
    {location:'第二条',error_type:'金额错误',original:'300',expected:'400',reason:'不一致'},
  ],
};
const html = context.taskRows([task], 1, 20);
process.stdout.write(html);
"""
    completed = subprocess.run(
        [NODE, "-e", harness, str(STATIC / script_name)],
        check=True,
        capture_output=True,
        text=True,
    )
    html = completed.stdout

    assert "2条未通过" in html
    assert "查看 2 条未通过原因" not in html
    assert 'class="review-detail-row"' in html
    assert f'colspan="{colspan}"' in html
    assert "review-chevron" in html


def test_dropzone_does_not_reopen_file_input_when_picker_button_is_clicked():
    if not NODE:
        pytest.skip("Node.js is required for frontend validation tests")
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const handlers = {};
let inputClicks = 0;
const zone = {
  addEventListener(name, callback){handlers[name]=callback;},
  classList:{add(){},remove(){}},
};
const input = {
  files:[], click(){inputClicks++;}, addEventListener(){},
};
const context = {document:{querySelector(){return {textContent:'',classList:{toggle(){}}};}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.setupDropzone(zone,input,()=>{});
handlers.click({target:{closest(){return {};}}});
const afterButton = inputClicks;
handlers.click({target:{closest(){return null;}}});
process.stdout.write(JSON.stringify({afterButton,afterZone:inputClicks}));
"""
    completed = subprocess.run(
        [NODE, "-e", harness, str(STATIC / "common.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {"afterButton": 0, "afterZone": 1}
