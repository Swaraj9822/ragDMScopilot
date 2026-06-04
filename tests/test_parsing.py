import pytest

from rag_system.parsing import (
    SUPPORTED_EXTENSIONS,
    SpreadsheetParser,
    HtmlParser,
    TextParser,
    _rows_to_markdown_table,
    detect_parser_type,
)


class TestDetectParserType:
    def test_pdf_routes_to_llamaparse(self) -> None:
        assert detect_parser_type("report.pdf") == "llamaparse"

    def test_docx_routes_to_llamaparse(self) -> None:
        assert detect_parser_type("notes.docx") == "llamaparse"

    def test_pptx_routes_to_llamaparse(self) -> None:
        assert detect_parser_type("slides.pptx") == "llamaparse"

    def test_image_routes_to_llamaparse(self) -> None:
        for ext in (".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp", ".gif"):
            assert detect_parser_type(f"scan{ext}") == "llamaparse"

    def test_xlsx_routes_to_spreadsheet(self) -> None:
        assert detect_parser_type("data.xlsx") == "spreadsheet"

    def test_csv_routes_to_spreadsheet(self) -> None:
        assert detect_parser_type("export.csv") == "spreadsheet"

    def test_html_routes_to_html(self) -> None:
        assert detect_parser_type("page.html") == "html"
        assert detect_parser_type("page.htm") == "html"

    def test_text_routes_to_text(self) -> None:
        assert detect_parser_type("readme.txt") == "text"
        assert detect_parser_type("README.md") == "text"
        assert detect_parser_type("notes.markdown") == "text"

    def test_unsupported_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unsupported file format"):
            detect_parser_type("archive.zip")

    def test_case_insensitive(self) -> None:
        assert detect_parser_type("REPORT.PDF") == "llamaparse"
        assert detect_parser_type("Data.XLSX") == "spreadsheet"


class TestRowsToMarkdownTable:
    def test_basic_table(self) -> None:
        rows = [("Name", "Age"), ("Alice", 30), ("Bob", 25)]
        result = _rows_to_markdown_table(rows)
        assert "| Name | Age |" in result
        assert "| --- | --- |" in result
        assert "| Alice | 30 |" in result

    def test_none_values_become_empty(self) -> None:
        rows = [("A", "B"), (None, "val")]
        result = _rows_to_markdown_table(rows)
        assert "|  | val |" in result

    def test_empty_rows_returns_empty(self) -> None:
        assert _rows_to_markdown_table([]) == ""


class TestSpreadsheetParser:
    @pytest.mark.asyncio
    async def test_csv_parsing(self) -> None:
        csv_content = b"Name,Score\nAlice,95\nBob,87\n"
        parser = SpreadsheetParser()
        result = await parser.parse("doc1", "v1", "grades.csv", csv_content)
        assert "Alice" in result.markdown
        assert "95" in result.markdown
        assert result.metadata["parser"] == "spreadsheet"

    @pytest.mark.asyncio
    async def test_empty_csv(self) -> None:
        parser = SpreadsheetParser()
        result = await parser.parse("doc1", "v1", "empty.csv", b"")
        assert "Empty CSV" in result.markdown


class TestHtmlParser:
    @pytest.mark.asyncio
    async def test_html_parsing(self) -> None:
        html = b"<html><head><title>Test</title></head><body><h1>Hello</h1><p>World</p></body></html>"
        parser = HtmlParser()
        result = await parser.parse("doc1", "v1", "page.html", html)
        assert "Hello" in result.markdown
        assert "World" in result.markdown
        assert result.metadata["parser"] == "html"
        assert result.metadata["title"] == "Test"

    @pytest.mark.asyncio
    async def test_strips_script_tags(self) -> None:
        html = b"<html><body><script>alert('xss')</script><p>Content</p></body></html>"
        parser = HtmlParser()
        result = await parser.parse("doc1", "v1", "page.html", html)
        assert "alert" not in result.markdown
        assert "Content" in result.markdown


class TestTextParser:
    @pytest.mark.asyncio
    async def test_plain_text(self) -> None:
        parser = TextParser()
        result = await parser.parse("doc1", "v1", "notes.txt", b"Hello world")
        assert result.markdown == "Hello world"
        assert result.metadata["parser"] == "text"

    @pytest.mark.asyncio
    async def test_markdown_file(self) -> None:
        parser = TextParser()
        result = await parser.parse("doc1", "v1", "README.md", b"# Title\nBody")
        assert result.markdown == "# Title\nBody"
        assert result.metadata["parser"] == "markdown"


def test_supported_extensions_completeness() -> None:
    """All advertised formats should be in the set."""
    expected = {
        ".pdf", ".docx", ".doc", ".pptx", ".ppt",
        ".xlsx", ".csv",
        ".html", ".htm",
        ".txt", ".md", ".markdown", ".rst",
        ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".gif",
    }
    assert expected.issubset(SUPPORTED_EXTENSIONS)
