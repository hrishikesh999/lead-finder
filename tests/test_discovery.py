from prospect_finder.discovery import _extract_url_from_description


def test_extract_url_basic():
    desc = "Visit our site at https://example.com for more info"
    assert _extract_url_from_description(desc) == "https://example.com"


def test_extract_url_http():
    assert _extract_url_from_description("http://foo.com bar") == "http://foo.com"


def test_extract_url_none_for_empty():
    assert _extract_url_from_description("") is None


def test_extract_url_none_for_none():
    assert _extract_url_from_description(None) is None


def test_extract_url_first_only():
    desc = "See https://first.com or https://second.com"
    result = _extract_url_from_description(desc)
    assert result == "https://first.com"


def test_extract_url_no_url():
    desc = "No links here, just text about HVAC certification."
    assert _extract_url_from_description(desc) is None


def test_extract_url_with_path():
    desc = "Visit https://example.com/courses/hvac-prep"
    assert _extract_url_from_description(desc) == "https://example.com/courses/hvac-prep"


def test_extract_url_strips_trailing_period():
    desc = "Visit https://example.com."
    assert _extract_url_from_description(desc) == "https://example.com"


def test_extract_url_strips_trailing_comma():
    desc = "See https://example.com, for details"
    assert _extract_url_from_description(desc) == "https://example.com"


def test_extract_url_strips_trailing_paren():
    desc = "More info (https://example.com)"
    assert _extract_url_from_description(desc) == "https://example.com"
