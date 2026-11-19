import pytest
from agency_outreach.validators import extract_email, is_it_agency, normalize_indian_mobile


@pytest.mark.parametrize("raw,expected", [
    ("+91 88842 66639", "918884266639"),
    ("WhatsApp +91 81011 22022", "918101122022"),
    ("9884266639", "919884266639"),
    ("+91 22 4297 5100", None),   # Mumbai landline
    ("+91 40 2355 4545", None),   # Hyderabad landline
    ("1800 569 5985", None),      # toll free
    ("Contact form", None),
    ("", None),
])
def test_mobile(raw, expected):
    assert normalize_indian_mobile(raw) == expected


def test_email():
    assert extract_email("Vijay@SkyBridgeHR.in") == "vijay@skybridgehr.in"
    assert extract_email("Contact form") is None
    assert extract_email("Job-seeker contact via site") is None


def test_it_flag():
    assert is_it_agency("IT + Non-IT + Healthcare")
    assert is_it_agency("Executive + specialist + technology")
    assert not is_it_agency("BPO + retail + BFSI + logistics + industrial + staffing")
    assert not is_it_agency("Education + sales + operations + support + staffing")
