"""Tests for the pure-logic pieces of auth/moomoo_login.py.

The login flow itself (real OAuth handshake, real browser, local HTTP
listener) is an interactive, one-time script meant to be run by a human
-- see its module docstring. This file only covers the one piece of
pure logic worth a regression test: never displaying a real account id
embedded in a granted scope string.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from auth.moomoo_login import _sanitize_scope_for_display


def test_accid_is_masked_in_displayed_scope():
    assert _sanitize_scope_for_display("quote:read trade:read accid:123456") == "quote:read trade:read accid:***"


def test_wildcard_accid_is_left_alone():
    assert _sanitize_scope_for_display("quote:read trade:read accid:*") == "quote:read trade:read accid:*"


def test_none_scope_passes_through():
    assert _sanitize_scope_for_display(None) is None
