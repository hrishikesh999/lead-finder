import httpx
import respx

from prospect_finder.verification import _is_personal_email, _scrape_contact_email


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


# ── _scrape_contact_email ─────────────────────────────────────────────────────

@respx.mock
def test_scrape_finds_own_domain_email():
    respx.get("https://hvacschool.com/").mock(
        return_value=httpx.Response(200, text="Email us at john@hvacschool.com for more info")
    )
    for path in ["/contact", "/contact-us", "/about", "/about-us"]:
        respx.get(f"https://hvacschool.com{path}").mock(return_value=httpx.Response(404))

    result = _scrape_contact_email("hvacschool.com")
    assert result is not None
    assert result[0] == "john@hvacschool.com"
    assert result[1] == 90


@respx.mock
def test_scrape_finds_email_on_contact_page():
    respx.get("https://hvacschool.com/").mock(
        return_value=httpx.Response(200, text="No email on homepage")
    )
    respx.get("https://hvacschool.com/contact").mock(
        return_value=httpx.Response(200, text="Reach us: john@hvacschool.com")
    )
    for path in ["/contact-us", "/about", "/about-us"]:
        respx.get(f"https://hvacschool.com{path}").mock(return_value=httpx.Response(404))

    result = _scrape_contact_email("hvacschool.com")
    assert result is not None
    assert result[0] == "john@hvacschool.com"


@respx.mock
def test_scrape_skips_generic_prefixes():
    respx.get("https://hvacschool.com/").mock(
        return_value=httpx.Response(200, text="Contact: info@hvacschool.com")
    )
    for path in ["/contact", "/contact-us", "/about", "/about-us"]:
        respx.get(f"https://hvacschool.com{path}").mock(return_value=httpx.Response(404))

    result = _scrape_contact_email("hvacschool.com")
    assert result is None


@respx.mock
def test_scrape_skips_junk_domains():
    respx.get("https://hvacschool.com/").mock(
        return_value=httpx.Response(200, text="plugin@wordpress.com is not our email")
    )
    for path in ["/contact", "/contact-us", "/about", "/about-us"]:
        respx.get(f"https://hvacschool.com{path}").mock(return_value=httpx.Response(404))

    result = _scrape_contact_email("hvacschool.com")
    assert result is None


@respx.mock
def test_scrape_returns_none_when_no_email():
    for path in ["/", "/contact", "/contact-us", "/about", "/about-us"]:
        respx.get(f"https://hvacschool.com{path}").mock(
            return_value=httpx.Response(200, text="No email addresses here at all")
        )

    result = _scrape_contact_email("hvacschool.com")
    assert result is None


@respx.mock
def test_scrape_handles_network_errors_gracefully():
    for path in ["/", "/contact", "/contact-us", "/about", "/about-us"]:
        respx.get(f"https://hvacschool.com{path}").mock(side_effect=httpx.ConnectError("refused"))

    result = _scrape_contact_email("hvacschool.com")
    assert result is None
