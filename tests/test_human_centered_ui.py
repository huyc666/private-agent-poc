from pathlib import Path

from html.parser import HTMLParser


ROOT = Path(__file__).resolve().parents[1]


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_id = {}
        self.elements = []

    def handle_starttag(self, tag, attrs):
        element = dict(attrs)
        self.elements.append(element)
        if element.get("id"):
            self.by_id[element["id"]] = element


def page(name):
    source = (ROOT / "web" / name).read_text(encoding="utf-8")
    elements = Elements()
    elements.feed(source)
    return source, elements


def test_chat_controls_have_accessible_names():
    _, soup = page("index.html")
    for element_id in ("panelBtn", "tabSessions", "tabApprovals", "tabUsage", "userBadge"):
        element = soup.by_id[element_id]
        assert element.get("tabindex") == "0"
        assert element.get("aria-label")
    assert soup.by_id["panelClose"].get("aria-label")


def test_console_tabs_and_table_scroll_are_accessible():
    _, soup = page("console.html")
    for tab in (element for element in soup.elements if "ctab" in element.get("class", "").split()):
        assert tab.get("tabindex") == "0"
        assert tab.get("role") == "button"
        assert tab.get("aria-label")
    for wrapper in (element for element in soup.elements if "table-wrap" in element.get("class", "").split()):
        assert wrapper.get("tabindex") == "0"
        assert wrapper.get("aria-label")


def test_chat_styles_include_mobile_layout_and_focused_scroll_regions():
    css, _ = page("index.html")
    assert "@media (max-width: 640px)" in css
    assert "#panel { visibility: hidden;" in css
    assert "#chat:focus-visible, #panel .panel-body:focus-visible" in css


def test_buttons_center_labels_without_changing_menu_alignment():
    css, _ = page("tokens.css")
    assert "button { display: inline-flex; align-items: center; justify-content: center;" in css
    assert '[role="button"] { display: inline-flex; align-items: center; justify-content: center;' in css
    css, _ = page("index.html")
    assert ".w-try { justify-content: flex-start;" in css


def test_composer_controls_share_height_and_bottom_alignment():
    css, _ = page("index.html")
    assert "#composer { display: flex; gap: 10px;" in css
    assert "align-items: flex-end; }" in css
    assert "#input { flex: 1;" in css and "min-height: 48px;" in css
    assert "#attach { background: #fff;" in css and "height: 48px; flex: none;" in css


def test_panel_close_is_a_consistent_icon_button():
    css, soup = page("index.html")
    assert "#panel .close { margin-left: auto; flex: none; width: 36px; height: 36px; padding: 0;" in css
    assert "#panel .close:hover { color: var(--accent); background: var(--accent-bg); border-color: var(--border); }" in css
    close = soup.by_id["panelClose"]
    assert close.get("type") == "button"
    assert close.get("aria-label")
