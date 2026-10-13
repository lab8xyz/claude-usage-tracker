#!/usr/bin/env python3
"""The usage numbers go to the todo app and to Paperclip (with Paperclip's token)."""

import importlib.util
import os
import tempfile

_spec = importlib.util.spec_from_file_location("cut", "claude-usage-tracker.py")
cut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut)


def test_targets_read_paperclip_env_file():
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("PAPERCLIP_PORT=7900\nPAPERCLIP_TOKEN=abc123\n")
    targets = cut.usage_targets("https://todo/api/usage", "http://127.0.0.1:7900/api/usage", f.name)
    os.unlink(f.name)
    assert targets == [
        ("https://todo/api/usage", {}),
        ("http://127.0.0.1:7900/api/usage", {"Authorization": "Bearer abc123"}),
    ]


def test_paperclip_skipped_without_token_file():
    assert cut.usage_targets("", "http://127.0.0.1:7900/api/usage", "/nonexistent") == []


if __name__ == "__main__":
    test_targets_read_paperclip_env_file()
    test_paperclip_skipped_without_token_file()
    print("ok")
