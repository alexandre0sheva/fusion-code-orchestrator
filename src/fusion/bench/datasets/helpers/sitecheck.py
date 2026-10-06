"""Test helper for frontend tasks: read a built static site and ask structural questions of it.

This file is copied into a task's hidden tests as ``tests/sitecheck.py`` (``helpers: [sitecheck]``
in ``task.yaml``). It uses the standard library only, because it runs in the sandbox next to the
model's files. It does not run JavaScript or lay anything out: it parses ``index.html``, the CSS
(linked, or inside ``<style>``) and the scripts, and answers questions such as "is every input
labelled?", "is there a media query?" and "does any rule set text and background colours that are
hard to read?". The same colour arithmetic lives in ``fusion.bench.evaluators.a11y``; it is written
out here so the tests need nothing from Fusion.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
NAMED = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "silver": (192, 192, 192),
    "red": (255, 0, 0),
    "navy": (0, 0, 128),
    "yellow": (255, 255, 0),
}


class Element:
    def __init__(self, tag: str, attrs: dict[str, str], parent: Element | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Element] = []
        self.chunks: list[str] = []

    @property
    def text(self) -> str:
        """All text inside, whitespace collapsed."""
        inner = " ".join(c.text for c in self.children)
        return re.sub(r"\s+", " ", " ".join([*self.chunks, inner])).strip()

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    def has(self, name: str) -> bool:
        return name in self.attrs

    def ancestors(self) -> list[Element]:
        found, node = [], self.parent
        while node is not None:
            found.append(node)
            node = node.parent
        return found

    def walk(self) -> list[Element]:
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, None)
        self.node = self.root
        self.styles: list[str] = []
        self.scripts: list[str] = []
        self._raw: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {k: (v or "") for k, v in attrs}, self.node)
        self.node.children.append(element)
        if tag in ("style", "script"):
            self._raw = tag
        if tag not in VOID:
            self.node = element

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {k: (v or "") for k, v in attrs}, self.node)
        self.node.children.append(element)

    def handle_endtag(self, tag: str) -> None:
        node: Element | None = self.node
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.node = node.parent
        if tag in ("style", "script"):
            self._raw = None

    def handle_data(self, data: str) -> None:
        if self._raw == "style":
            self.styles.append(data)
        elif self._raw == "script":
            self.scripts.append(data)
        else:
            self.node.chunks.append(data)


class Page:
    """The site in the current directory (or ``root``)."""

    def __init__(self, root: str = ".", entry: str = "index.html") -> None:
        base = Path(root)
        self.html = (base / entry).read_text(encoding="utf-8")
        builder = _Builder()
        builder.feed(self.html)
        builder.close()
        self.tree = builder.root
        self.elements = [e for e in self.tree.walk() if e.tag != "#document"]
        css = list(builder.styles)
        js = list(builder.scripts)
        for link in self.find("link", rel="stylesheet"):
            target = base / link.get("href")
            if (
                link.get("href")
                and not link.get("href").startswith(("http", "//"))
                and target.is_file()
            ):
                css.append(target.read_text(encoding="utf-8"))
        for script in self.find("script"):
            target = base / script.get("src")
            if (
                script.get("src")
                and not script.get("src").startswith(("http", "//"))
                and target.is_file()
            ):
                js.append(target.read_text(encoding="utf-8"))
        self.css = re.sub(r"/\*.*?\*/", "", "\n".join(css), flags=re.S)
        self.js = "\n".join(js)

    # -- finding elements -----------------------------------------------------------------------

    def find(self, tag: str | None = None, **attrs: str | bool) -> list[Element]:
        """Elements of ``tag`` whose attributes match: a string must equal the value (a class
        matches one of its classes) and ``True`` only needs the attribute present."""
        found = []
        for el in self.elements:
            if tag is not None and el.tag != tag:
                continue
            ok = True
            for name, want in attrs.items():
                name = name.rstrip("_")
                if want is True:
                    ok = ok and el.has(name)
                elif name == "class":
                    ok = ok and want in el.get("class").split()
                else:
                    ok = ok and el.get(name) == want
            if ok:
                found.append(el)
        return found

    def first(self, tag: str, **attrs: str | bool) -> Element | None:
        found = self.find(tag, **attrs)
        return found[0] if found else None

    @property
    def title(self) -> str:
        el = self.first("title")
        return el.text if el else ""

    @property
    def lang(self) -> str:
        el = self.first("html")
        return el.get("lang") if el else ""

    def text(self) -> str:
        body = self.first("body")
        return body.text if body else ""

    def headings(self) -> list[tuple[int, str]]:
        return [(int(e.tag[1]), e.text) for e in self.elements if re.fullmatch(r"h[1-6]", e.tag)]

    # -- accessibility --------------------------------------------------------------------------

    def label_of(self, control: Element) -> str:
        """The accessible name of a form control ('' when it has none)."""
        for key in ("aria-label", "title"):
            if control.get(key).strip():
                return control.get(key).strip()
        ident = control.get("id")
        if control.get("aria-labelledby"):
            names = [self.by_id(i) for i in control.get("aria-labelledby").split()]
            return " ".join(n.text for n in names if n is not None).strip()
        if ident:
            for label in self.find("label", for_=ident):
                return label.text
        for parent in control.ancestors():
            if parent.tag == "label":
                return parent.text
        return ""

    def by_id(self, ident: str) -> Element | None:
        found = self.find(id=ident)
        return found[0] if found else None

    def name_of(self, el: Element) -> str:
        """The accessible name of a button or link: its text, label or an image's alt."""
        if el.get("aria-label").strip():
            return el.get("aria-label").strip()
        text = el.text
        if text:
            return text
        for inner in el.walk():
            if inner.tag == "img" and inner.get("alt").strip():
                return inner.get("alt").strip()
        return el.get("title").strip() or el.get("value").strip()

    def unlabelled_controls(self) -> list[Element]:
        skip = {"hidden", "submit", "button", "reset", "image"}
        return [
            e
            for e in self.elements
            if (
                e.tag in ("select", "textarea")
                or (e.tag == "input" and e.get("type", "text") not in skip)
            )
            and not self.label_of(e)
        ]

    def images_without_alt(self) -> list[Element]:
        return [e for e in self.find("img") if not e.has("alt")]

    # -- CSS ------------------------------------------------------------------------------------

    def rules(self) -> list[tuple[str, dict[str, str], bool]]:
        """``(selector, declarations, inside a media query)`` for every rule, flattened."""
        out: list[tuple[str, dict[str, str], bool]] = []
        depth_media: list[bool] = []
        text = self.css
        pos = 0
        buf = ""
        stack: list[str] = []
        while pos < len(text):
            ch = text[pos]
            if ch == "{":
                head = buf.strip()
                buf = ""
                stack.append(head)
                if head.startswith("@media") or head.startswith("@supports"):
                    depth_media.append(True)
                elif head.startswith("@"):
                    depth_media.append(False)
                else:
                    end = text.find("}", pos)
                    body = text[pos + 1 : end]
                    decls = {}
                    for part in body.split(";"):
                        if ":" in part:
                            key, _, value = part.partition(":")
                            decls[key.strip().lower()] = value.strip()
                    out.append((head, decls, any(depth_media)))
                    pos = end
                    stack.pop()
            elif ch == "}":
                if stack:
                    stack.pop()
                    if depth_media:
                        depth_media.pop()
                buf = ""
            else:
                buf += ch
            pos += 1
        return out

    def declared(self, prop: str) -> list[tuple[str, str]]:
        return [(sel, decls[prop]) for sel, decls, _ in self.rules() if prop in decls]

    def media_queries(self) -> list[str]:
        return re.findall(r"@media[^{]*", self.css)

    def has_viewport_meta(self) -> bool:
        return any(
            "width=device-width" in m.get("content") for m in self.find("meta", name="viewport")
        )

    def is_fluid(self) -> bool:
        """Viewport meta, and something that lets the layout adapt (a media query, a wrapping flex
        or auto grid, clamp or min), and no wide fixed width outside a media query."""
        adapts = bool(self.media_queries()) or bool(
            re.search(r"auto-fit|auto-fill|flex-wrap|clamp\(|min\(", self.css)
        )
        return self.has_viewport_meta() and adapts and not self.wide_fixed_widths()

    def wide_fixed_widths(self, limit: float = 420.0) -> list[tuple[str, str]]:
        wide = []
        for selector, decls, in_media in self.rules():
            if in_media:
                continue
            for prop in ("width", "min-width"):
                value = decls.get(prop, "")
                match = re.fullmatch(r"(\d+(?:\.\d+)?)px", value)
                if match and float(match.group(1)) > limit:
                    wide.append((selector, value))
        return wide

    def contrast_failures(self, minimum: float = 4.5) -> list[tuple[str, float]]:
        """Rules that set text and background colours together, or colour for the page itself,
        with a contrast ratio under ``minimum``."""
        page_fg, page_bg = (0, 0, 0), (255, 255, 255)
        rules = self.rules()
        for selector, decls, _ in rules:
            if re.fullmatch(r"(html|body|:root)(\s*,\s*(html|body|:root))*", selector.strip()):
                page_fg = parse_color(decls.get("color", "")) or page_fg
                page_bg = (
                    parse_color(decls.get("background-color", decls.get("background", "")))
                    or page_bg
                )
        failures = []
        for selector, decls, _ in rules:
            fg = parse_color(decls.get("color", ""))
            bg = parse_color(decls.get("background-color", decls.get("background", "")))
            page_rule = bool(re.fullmatch(r"(html|body|:root)", selector.strip()))
            if fg is not None and bg is not None:
                ratio = contrast(fg, bg)
            elif (fg is not None or bg is not None) and page_rule:
                ratio = contrast(fg or page_fg, bg or page_bg)
            else:
                continue
            if ratio < minimum:
                failures.append((selector, round(ratio, 2)))
        return failures

    def has_focus_style(self) -> bool:
        return any(":focus" in sel for sel, _, _ in self.rules())


def parse_color(text: str) -> tuple[int, int, int] | None:
    value = text.strip().lower().split("!")[0].strip()
    if value in NAMED:
        return NAMED[value]
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits[:3])
        if len(digits) in (6, 8) and re.fullmatch(r"[0-9a-f]+", digits):
            return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
    match = re.fullmatch(r"rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+).*\)", value)
    if match:
        r, g, b = (min(int(x), 255) for x in match.groups())
        return r, g, b
    return None


def _lum(rgb: tuple[int, int, int]) -> float:
    def f(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2])


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
