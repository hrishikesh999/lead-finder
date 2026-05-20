import pytest

from prospect_finder.verification import _is_personal_email


def test_is_personal_true():
    assert _is_personal_email("john@example.com") is True
    assert _is_personal_email("jane.doe@example.com") is True
    assert _is_personal_email("j.smith@example.com") is True
    assert _is_personal_email("founder@example.com") is True


def test_is_personal_false_generic():
    assert _is_personal_email("info@example.com") is False
    assert _is_personal_email("contact@example.com") is False
    assert _is_personal_email("support@example.com") is False
    assert _is_personal_email("hello@example.com") is False
    assert _is_personal_email("admin@example.com") is False
    assert _is_personal_email("sales@example.com") is False
    assert _is_personal_email("team@example.com") is False
    assert _is_personal_email("noreply@example.com") is False
    assert _is_personal_email("no-reply@example.com") is False


def test_is_personal_case_insensitive():
    assert _is_personal_email("INFO@example.com") is False
    assert _is_personal_email("HELLO@example.com") is False
    assert _is_personal_email("Support@example.com") is False
