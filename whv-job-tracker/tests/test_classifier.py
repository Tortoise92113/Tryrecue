import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from classifier import _is_503, _is_network_error, _validate


# ── _is_503 ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg,expected", [
    ("503 Service Unavailable", True),
    ("status code 503", True),
    ("UNAVAILABLE: quota exceeded", True),
    ("The API is under high demand right now", True),
    ("server is overloaded, try again later", True),
    ("Invalid API key", False),
    ("JSONDecodeError: Expecting value", False),
    ("401 Unauthorized", False),
    ("", False),
])
def test_is_503_detection(msg, expected):
    assert _is_503(Exception(msg)) == expected


@pytest.mark.parametrize("msg,expected", [
    ("Could not contact DNS servers", True),
    ("Cannot connect to host generativelanguage.googleapis.com:443", True),
    ("getaddrinfo failed", True),
    ("network is unreachable", True),
    ("Invalid API key", False),
    ("503 Service Unavailable", False),
])
def test_is_network_error_detection(msg, expected):
    assert _is_network_error(Exception(msg)) == expected


# ── _validate ─────────────────────────────────────────────────────────────────

def test_validate_happy_path():
    out = _validate({
        "is_whv_friendly": 1, "visa_sponsorship": 0, "accommodation": 1,
        "urgency": "high", "note": "short-term role", "job_type": "farm",
        "regional_area": True,
    })
    assert out["is_whv_friendly"] == 1
    assert out["urgency"] == "high"
    assert out["job_type"] == "farm"
    assert out["regional_area"] == 1
    assert out["classifier_note"] == "short-term role"


def test_validate_invalid_urgency_defaults_to_normal():
    assert _validate({"urgency": "very-urgent"})["urgency"] == "normal"
    assert _validate({})["urgency"] == "normal"


def test_validate_unknown_job_type_becomes_other():
    assert _validate({"job_type": "submarine"})["job_type"] == "other"
    assert _validate({})["job_type"] == "other"


@pytest.mark.parametrize("raw,expected", [
    (True, 1),
    (1, 1),
    (False, 0),
    (0, 0),
    (None, None),
    ("maybe", None),
])
def test_validate_regional_area_coercion(raw, expected):
    assert _validate({"regional_area": raw})["regional_area"] == expected


def test_validate_note_truncated():
    from classifier import _NOTE_MAX_LEN
    long_note = "x" * (_NOTE_MAX_LEN + 100)
    out = _validate({"note": long_note})
    assert len(out["classifier_note"]) == _NOTE_MAX_LEN


def test_validate_falsy_whv_friendly():
    assert _validate({"is_whv_friendly": 0})["is_whv_friendly"] == 0
    assert _validate({"is_whv_friendly": None})["is_whv_friendly"] == 0


def test_validate_missing_keys_returns_defaults():
    out = _validate({})
    assert out["is_whv_friendly"] == 0
    assert out["visa_sponsorship"] == 0
    assert out["accommodation"] == 0
    assert out["urgency"] == "normal"
    assert out["job_type"] == "other"
    assert out["regional_area"] is None
    assert out["classifier_note"] == ""
