from rag_system.chunking import infer_page_range, infer_section_path, stable_chunk_id


def test_stable_chunk_id_is_deterministic() -> None:
    assert stable_chunk_id("doc", "v1", 1, "hello") == stable_chunk_id("doc", "v1", 1, "hello")


def test_infer_page_range() -> None:
    assert infer_page_range("See page 4 and p. 9 for details") == (4, 9)


def test_infer_section_path() -> None:
    assert infer_section_path("# Policy\nbody\n## Exceptions") == ["Policy", "Exceptions"]
