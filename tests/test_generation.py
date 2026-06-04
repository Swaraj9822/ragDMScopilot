from rag_system.generation import build_grounded_prompt


def test_prompt_requires_grounding() -> None:
    prompt = build_grounded_prompt("What is revenue?", "Revenue was 10 on page 2.")

    assert "Use only the provided context" in prompt
    assert "What is revenue?" in prompt
    assert "Revenue was 10" in prompt
