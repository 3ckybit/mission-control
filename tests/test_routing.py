import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NVIDIA_API_KEY", "test-key")
from app import pick_model, MODEL_FAST, MODEL_MAIN, MODEL_LARGE


def test_explicit_fast_mode():
    provider, model = pick_model([{"role": "user", "content": "hi"}], "fast")
    assert (provider, model) == ("nvidia", MODEL_FAST)


def test_explicit_main_mode():
    provider, model = pick_model([{"role": "user", "content": "hi"}], "main")
    assert (provider, model) == ("nvidia", MODEL_MAIN)


def test_explicit_deep_mode():
    provider, model = pick_model([{"role": "user", "content": "hi"}], "deep")
    assert (provider, model) == ("nvidia", MODEL_LARGE)


def test_claude_mode_without_key_falls_back_to_nvidia():
    import app
    old_key = app.ANTHROPIC_API_KEY
    app.ANTHROPIC_API_KEY = ""
    try:
        provider, model = pick_model([{"role": "user", "content": "hi"}], "claude")
        assert (provider, model) == ("nvidia", MODEL_LARGE)
    finally:
        app.ANTHROPIC_API_KEY = old_key


def test_claude_mode_over_budget_falls_back_to_nvidia():
    import app
    old_key = app.ANTHROPIC_API_KEY
    app.ANTHROPIC_API_KEY = "sk-ant-fake"
    try:
        from unittest.mock import patch
        with patch("app.get_spend_today", return_value=999.0):
            provider, model = pick_model([{"role": "user", "content": "hi"}], "claude")
            assert (provider, model) == ("nvidia", MODEL_LARGE)
    finally:
        app.ANTHROPIC_API_KEY = old_key


def test_auto_mode_short_message_picks_fast():
    provider, model = pick_model([{"role": "user", "content": "hi there"}], "auto")
    assert (provider, model) == ("nvidia", MODEL_FAST)


def test_auto_mode_medium_message_picks_main():
    msg = " ".join(["word"] * 50)
    provider, model = pick_model([{"role": "user", "content": msg}], "auto")
    assert (provider, model) == ("nvidia", MODEL_MAIN)


def test_auto_mode_long_message_picks_deep():
    msg = " ".join(["word"] * 150)
    provider, model = pick_model([{"role": "user", "content": msg}], "auto")
    assert (provider, model) == ("nvidia", MODEL_LARGE)
