from prospect_finder.extraction import _MAX_HTML_CHARS, _build_follow_urls, _strip_html


def test_strip_html_removes_script():
    html = "<html><body><script>alert(1)</script><p>Hello World</p></body></html>"
    result = _strip_html(html)
    assert "alert" not in result
    assert "Hello World" in result


def test_strip_html_removes_style():
    html = "<html><body><style>body { color: red; }</style><p>Content</p></body></html>"
    result = _strip_html(html)
    assert "color: red" not in result
    assert "Content" in result


def test_strip_html_removes_nav():
    html = "<html><body><nav>Menu items</nav><main>Main content</main></body></html>"
    result = _strip_html(html)
    assert "Menu items" not in result
    assert "Main content" in result


def test_strip_html_truncates():
    html = "<p>" + "x" * (_MAX_HTML_CHARS * 2) + "</p>"
    result = _strip_html(html)
    assert len(result) <= _MAX_HTML_CHARS


def test_strip_html_preserves_text():
    html = "<html><body><h1>HVAC Exam Prep</h1><p>Founded by John Smith.</p></body></html>"
    result = _strip_html(html)
    assert "HVAC Exam Prep" in result
    assert "Founded by John Smith" in result


def test_build_follow_urls_simple():
    urls = _build_follow_urls("https://example.com")
    assert "https://example.com/about" in urls
    assert "https://example.com/about-us" in urls
    assert "https://example.com/team" in urls
    assert "https://example.com/our-story" in urls
    assert "https://example.com/contact" in urls
    assert len(urls) == 5


def test_build_follow_urls_strips_path():
    urls = _build_follow_urls("https://example.com/blog/post-123")
    for url in urls:
        assert "/blog/post-123" not in url
    assert "https://example.com/about" in urls


def test_build_follow_urls_strips_trailing_slash():
    urls = _build_follow_urls("https://example.com/")
    assert "https://example.com/about" in urls
