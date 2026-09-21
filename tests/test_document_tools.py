"""document_read/document_write: TOOL.md frontmatter, and a full round trip
through the isolated venv scripts/install-docs-env.ps1/.sh provisions (see
CLAUDE.md rule 16 -- python-docx/openpyxl/pypdf/reportlab never live in this
project's own .venv). The round trip is skipped, not failed, when that venv
has not been provisioned on the machine running the tests.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from evomesh.harness_tools import ToolContext, ToolRegistry, build_custom_tool
from evomesh.tools import parse_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_PYTHON = REPO_ROOT / ".runtime" / "docs" / "Scripts" / "python.exe"
if not DOCS_PYTHON.exists():  # POSIX layout, e.g. install-docs-env.sh
    DOCS_PYTHON = REPO_ROOT / ".runtime" / "docs" / "bin" / "python"

requires_docs_env = pytest.mark.skipif(
    not DOCS_PYTHON.exists(),
    reason="docs venv not provisioned; run scripts/install-docs-env.ps1 or .sh first",
)


@pytest.mark.parametrize("name", ["document_read", "document_write"])
def test_tool_definition_parses_and_splits_into_two_argv_entries(name: str) -> None:
    path = REPO_ROOT / "tools" / name / "TOOL.md"
    definition = parse_tool(path, path.read_text(encoding="utf-8"))

    assert definition.name == name
    # Regression: an unquoted `command: "<a>" "<b>"` is two YAML tokens, not
    # one string -- yaml.safe_load chokes on the trailing `"<b>"`. The whole
    # command must be one single-quoted YAML scalar so shlex sees exactly
    # the interpreter and the script, both as one argv entry each.
    argv = shlex.split(definition.command)
    assert len(argv) == 2
    assert argv[0].endswith("python.exe") or argv[0].endswith("/python")
    assert argv[1] == "{tool_dir}/scripts/" + name + ".py"


def _run(script: str, request: dict, *, expect_ok: bool = True) -> dict:
    script_path = REPO_ROOT / "tools" / script / "scripts" / f"{script}.py"
    result = subprocess.run(
        [str(DOCS_PYTHON), str(script_path), json.dumps(request)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if expect_ok:
        assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@requires_docs_env
@pytest.mark.parametrize("ext", ["docx", "pdf", "xlsx", "csv"])
def test_write_then_read_round_trip(tmp_path: Path, ext: str) -> None:
    target = tmp_path / f"report.{ext}"
    write_result = _run(
        "document_write",
        {
            "path": str(target),
            "title": "Q1 Report",
            "paragraphs": ["Revenue was up 4%."],
            "headers": ["Metric", "Value"],
            "rows": [["Revenue", "1.2M"]],
        },
    )
    assert write_result["path"] == str(target)
    assert write_result["bytes_written"] > 0
    assert target.is_file()

    read_result = _run("document_read", {"path": str(target)})
    assert read_result["type"] == ext
    assert read_result["truncated"] is False
    if ext in ("csv", "xlsx"):
        assert read_result["headers"] == ["Metric", "Value"]
        assert read_result["rows"] == [["Revenue", "1.2M"]]
    else:
        assert "Revenue was up 4%." in read_result["text"]


@requires_docs_env
def test_read_reports_a_clean_error_for_a_missing_file(tmp_path: Path) -> None:
    result = _run("document_read", {"path": str(tmp_path / "nope.docx")}, expect_ok=False)
    assert "error" in result


@requires_docs_env
def test_write_reports_a_clean_error_for_an_unsupported_extension(tmp_path: Path) -> None:
    result = _run("document_write", {"path": str(tmp_path / "nope.txt")}, expect_ok=False)
    assert "error" in result


@requires_docs_env
async def test_document_write_reached_through_the_real_harness_tool_dispatch(
    tmp_path: Path,
) -> None:
    """The exact path an agent's own tool call takes -- ToolRegistry.invoke
    over a Tool built by build_custom_tool() from the real TOOL.md, with
    "python" allow-listed the same way evomesh.yaml already has it -- not a
    subprocess shortcut. This is the "news-watcher answers a PDF request"
    scenario end to end for the one genuinely new piece: an agent's own
    document_write call, run the same way the harness runs it in production,
    landing a real .pdf in the agent's own job root.
    """
    path = REPO_ROOT / "tools" / "document_write" / "TOOL.md"
    definition = parse_tool(path, path.read_text(encoding="utf-8"))
    tool = build_custom_tool(definition, tool_dir=path.parent)
    context = ToolContext(root=tmp_path, shell_allow=frozenset({"python"}), shell_seconds=30.0)

    result = await ToolRegistry((tool,)).invoke(
        context,
        "document_write",
        {
            "request": json.dumps(
                {
                    "path": "news.pdf",
                    "title": "Latest headlines",
                    "headers": ["Headline", "Published"],
                    "rows": [[f"Headline {i}", "2026-09-21"] for i in range(1, 11)],
                }
            )
        },
    )

    assert "exit 0" in result
    landed = tmp_path / "news.pdf"
    assert landed.is_file()
    assert landed.read_bytes().startswith(b"%PDF")


@requires_docs_env
def test_xlsx_multi_sheet_and_selection(tmp_path: Path) -> None:
    target = tmp_path / "book.xlsx"
    _run(
        "document_write",
        {
            "path": str(target),
            "sheets": {
                "Summary": {"headers": ["A"], "rows": [["1"]]},
                "Detail": {"headers": ["B"], "rows": [["2"], ["3"]]},
            },
        },
    )

    detail = _run("document_read", {"path": str(target), "sheet": "Detail"})
    assert detail["sheet"] == "Detail"
    assert detail["sheet_names"] == ["Summary", "Detail"]
    assert detail["rows"] == [["2"], ["3"]]
