from render_pipeline import MAX_OUTPUT_LENGTH, apply_render_scripts


def test_replace_capture_disabled_invalid_and_order():
    scripts = [
        {"enabled": False, "find_regex": "A", "replace_string": "X"},
        {"enabled": True, "find_regex": r"(A)", "replace_string": r"<\1>"},
        {"enabled": True, "find_regex": r"<A>", "replace_string": "B"},
        {"enabled": True, "find_regex": "[", "replace_string": "bad"},
    ]
    assert apply_render_scripts("A", scripts) == "B"


def test_output_limit():
    result = apply_render_scripts("a", [{"enabled": True, "find_regex": "a", "replace_string": "x" * (MAX_OUTPUT_LENGTH + 1)}])
    assert len(result) == MAX_OUTPUT_LENGTH
