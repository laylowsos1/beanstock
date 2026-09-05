"""Tests for auth.token_storage.WindowsCredentialSecretStore.

test_real_round_trip_set_get_delete actually exercises the real Windows
Credential Manager (Win32 CredWriteW/CredReadW/CredDeleteW via ctypes) --
this is the one place in the suite that touches real OS state, and it
does so deliberately: this class's entire job is that OS integration,
so a fully-mocked test would not prove it works. It uses a uniquely
named, clearly-test-scoped credential and always deletes it afterward,
even on failure. It only runs on win32.

Every other test here is pure-Python logic (size limits, platform
guard) and touches nothing.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from auth.token_storage import WindowsCredentialSecretStore


def test_oversized_secret_rejected_before_any_windows_call(monkeypatch):
    store = WindowsCredentialSecretStore(credential_prefix="beanstock-test/moomoo")

    def fail_if_called():
        raise AssertionError("Should never reach the Windows API for an oversized secret.")

    monkeypatch.setattr(store, "_advapi32", lambda: fail_if_called())

    with pytest.raises(ValueError):
        store.set("oversized", "x" * 3000)


def test_non_windows_platform_raises_runtime_error_not_a_plaintext_fallback(monkeypatch):
    store = WindowsCredentialSecretStore(credential_prefix="beanstock-test/moomoo")
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError):
        store.get("anything")
    with pytest.raises(RuntimeError):
        store.set("anything", "value")
    with pytest.raises(RuntimeError):
        store.delete("anything")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager is only available on Windows.")
def test_real_round_trip_set_get_delete():
    store = WindowsCredentialSecretStore(credential_prefix="beanstock-test/moomoo")
    test_key = f"pytest-{uuid.uuid4().hex}"
    test_value = f"round-trip-value-{uuid.uuid4().hex}"

    assert store.get(test_key) is None  # nothing there yet

    try:
        store.set(test_key, test_value)
        assert store.get(test_key) == test_value
    finally:
        store.delete(test_key)

    assert store.get(test_key) is None  # cleaned up
    store.delete(test_key)  # idempotent -- deleting an absent key must not raise
