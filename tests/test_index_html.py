"""The custom `index.html`, and the one thing that can silently ruin it.

`flet publish` copies `src/assets/index.html` over its own, which is how
the wallet bridge gets a script tag and how the icons and theme colours
get declared. Everything in that head is inert markup, so nothing here
would ever catch a mistake in it -- except that the head is exactly where
a mistake stops being inert.

This is what happened: the file opens with a comment explaining which
markers `patch_index.py` rewrites, and that comment spelled out an HTML
comment terminator while warning against writing one. The browser ended
the comment there, found bare prose inside HEAD, and did what the parser
is specified to do -- implicitly closed HEAD and opened BODY. The base
tag, the title and both icon links landed in BODY, where a favicon link
is ignored, so Chrome fell back to `/favicon.ico`, got a 404, and showed
nothing. The file read correctly, the icon was served correctly, and
`document.querySelector('link[rel=icon]')` found it. Only
`document.head.children` gave it away.

So these parse the file the way a browser does and check that the head
still contains what the head is supposed to contain.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "src" / "assets" / "index.html"

#: Elements whose text content belongs there and is not stray prose.
TEXT_IS_FINE_IN = {"script", "style", "title"}


class HeadReader(HTMLParser):
    """What lands in the head, and any bare text that should not."""

    def __init__(self) -> None:
        super().__init__()
        self.in_head = False
        self.current: str | None = None
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.stray: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.current = tag
        if tag == "head":
            self.in_head = True
        elif tag == "body":
            self.in_head = False
        elif self.in_head:
            self.tags.append((tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        self.current = None
        if tag == "head":
            self.in_head = False

    def handle_data(self, data: str) -> None:
        if self.in_head and self.current not in TEXT_IS_FINE_IN and data.strip():
            self.stray.append(" ".join(data.split())[:70])


@pytest.fixture(scope="module")
def head() -> HeadReader:
    reader = HeadReader()
    reader.feed(INDEX.read_text(encoding="utf-8"))
    return reader


def test_no_bare_text_inside_head(head: HeadReader) -> None:
    """The failure mode itself: text in the head closes the head.

    A browser cannot keep parsing head content after it, so everything
    below the stray text -- title, icons, manifest -- silently becomes
    body content. Comment terminators inside comments are the way this
    happens; there is no other prose in there.
    """
    assert not head.stray, f"text in <head> closes it early: {head.stray}"


def test_the_icon_links_are_in_the_head(head: HeadReader) -> None:
    """Where a browser is willing to look for them. A `rel=icon` in the
    body parses fine, resolves fine, and is ignored."""
    rels = {attrs.get("rel"): attrs.get("href", "") for tag, attrs in head.tags
            if tag == "link"}
    assert "icon" in rels, f"no favicon link in <head>, only {sorted(rels)}"
    assert rels["icon"].startswith("favicon.png")
    assert "apple-touch-icon" in rels
    assert "manifest" in rels


def test_the_icon_url_is_versioned(head: HeadReader) -> None:
    """Browsers keep favicons in a store keyed by URL, which no cache
    header reaches. Changing the URL is the only thing they notice."""
    for tag, attrs in head.tags:
        if tag == "link" and attrs.get("rel") in ("icon", "apple-touch-icon"):
            assert "?v=" in attrs.get("href", ""), attrs


def test_the_title_and_base_are_in_the_head(head: HeadReader) -> None:
    """Both were casualties of the same bug -- and an empty
    `document.title` was the first visible symptom."""
    tags = [tag for tag, _ in head.tags]
    assert "title" in tags
    assert "base" in tags


def test_the_flet_markers_survive(head: HeadReader) -> None:
    """`patch_index.py` rewrites these by plain string replacement, and a
    build with them missing produces a page that never boots."""
    text = INDEX.read_text(encoding="utf-8")
    assert '<base href="/">' in text
    assert "var flet = {" in text
