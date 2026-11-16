"""Type stub for the `pytesseract` distribution.

pytesseract ships no `py.typed` marker and there is no `types-pytesseract`
package on PyPI, so mypy refuses to analyse it and reports
`import-untyped`. This stub supplies the small surface Foresight actually
uses instead of exempting the module from type checking.

Only the members referenced by `foresight/document_ingestion.py` are
declared. Extend this file if new pytesseract calls are added — a missing
member here surfaces as an `attr-defined` error, which is the intent.
"""

from typing import Any

from PIL.Image import Image

def image_to_string(
    image: Image | str | Any,
    lang: str | None = None,
    config: str = "",
    nice: int = 0,
    output_type: str = "string",
    timeout: int = 0,
) -> str: ...
def image_to_data(
    image: Image | str | Any,
    lang: str | None = None,
    config: str = "",
    nice: int = 0,
    output_type: str = "dict",
    timeout: int = 0,
) -> dict[str, list[Any]]: ...
def get_tesseract_version() -> Any: ...
