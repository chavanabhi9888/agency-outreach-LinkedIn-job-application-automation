from pathlib import Path

import pytest
pdf_parser = pytest.importorskip("agency_outreach.pdf_parser", reason="legacy PDF parser module is not part of this project any more")
ParseError, parse_agencies, rows_to_agencies = pdf_parser.ParseError, pdf_parser.parse_agencies, pdf_parser.rows_to_agencies

ROWS = [
    ["#", "Agency", "Hiring coverage", "Phone", "Email / contact", "Location", "Website"],
    ["1", "Prydan Consultancy", "IT + Non-IT", "+91 88842 66639", "info@prydan.com", "Ahmedabad", "prydan.com"],
    ["2", "Adecco India", "IT + engineering\n+ sales", "Contact form", "Contact form", "Pan-India", "adecco.com/en-in"],
    ["", "junk", "", "", "", "", ""],
    ["3", "short row"],
]


def test_rows_to_agencies():
    a = rows_to_agencies(ROWS)
    assert [x.rank for x in a] == [1, 2]
    assert a[0].email == "info@prydan.com" and a[0].whatsapp == "918884266639"
    assert a[1].email is None and a[1].whatsapp is None
    assert a[1].coverage == "IT + engineering + sales"          # newline collapsed
    assert a[1].website_url == "https://adecco.com/en-in"


def test_missing_pdf(tmp_path):
    with pytest.raises(ParseError):
        parse_agencies(tmp_path / "nope.pdf")


SAMPLE = Path("India_IT_NonIT_Recruitment_Agencies_50_Contact_List_2026.pdf")


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample PDF not present")
def test_real_pdf():
    assert len(parse_agencies(SAMPLE)) == 50
