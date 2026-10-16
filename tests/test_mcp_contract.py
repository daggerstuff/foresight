"""Contract tests for the tool-result error flag and its owning dependencies.

mcp 2.0 renamed ``CallToolResult.isError`` to ``is_error`` (camelCase survives
only as the wire alias). Our auth middleware builds that object by keyword, so
a name drift turns rejected calls into silent successes. These tests pin the
Python-side names, the wire alias, and the fact that the packages are declared
rather than arriving transitively through ``fastmcp[client,server]``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolResult, TextContent

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_dependencies() -> set[str]:
    with _PYPROJECT.open("rb") as fh:
        requirements = tomllib.load(fh)["project"]["dependencies"]
    return {re.split(r"[<>=!;\[ ]", req, maxsplit=1)[0] for req in requirements}


class TestToolResultErrorFlag:
    def test_tool_result_uses_snake_case_error_field(self) -> None:
        assert "is_error" in ToolResult.model_fields

    def test_error_result_marks_the_call_failed(self) -> None:
        result = ToolResult(content=[TextContent(type="text", text="Authentication required")], is_error=True)
        assert result.is_error is True

    def test_studio_camel_case_keyword_fails_loudly(self) -> None:
        """The pre-2.0 name must not be silently swallowed into an extra field."""
        with pytest.raises(TypeError):
            ToolResult(content=[TextContent(type="text", text="x")], isError=True)

    def test_wire_format_keeps_camel_case_alias(self) -> None:
        fields = CallToolResult.model_fields
        assert "is_error" in fields
        assert fields["is_error"].alias == "isError"
        payload = CallToolResult(content=[], is_error=True).model_dump(by_alias=True, exclude_none=True)
        assert payload["isError"] is True


class TestDeclaredDependencies:
    @pytest.mark.parametrize("package", ["mcp", "python-dotenv"])
    def test_directly_imported_package_is_declared(self, package: str) -> None:
        assert package in _declared_dependencies()
