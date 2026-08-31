#!/usr/bin/env python3
"""Build index.html (what the App Store reads) from index.src.html (what we edit).

The Odoo App Store sanitises a module description before serving it, and two of
its habits break an ordinary HTML page:

  1. It strips the ``<style>`` element. Inline ``style=`` attributes survive, so
     this script flattens every rule in the source stylesheet onto the elements
     that match it. Without that step ``.amc img { max-width: 100% }`` is lost
     and the screenshots render at their natural width (up to 2880px), which is
     what makes the published page scroll sideways.

  2. It reads the file as latin-1. Every non-ASCII byte then arrives mangled --
     an em dash turns into "a" with a circumflex. So the output is written as
     pure ASCII, with every other character escaped to an HTML entity. Entities
     are ASCII themselves, which makes them immune to the encoding guess.

The rewrite is textual on purpose: only the ``style`` attribute of a tag is
touched and the rest of its source is copied byte for byte. That keeps the
camelCase SVG attributes (``viewBox``, ``markerWidth``, ``refX``) intact, which
a parse-and-reserialise pass would silently lowercase and break.

Usage:  python3 build_description.py
"""

from __future__ import annotations

import html.entities
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "index.src.html"
OUT = HERE / "index.html"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}

# Rules that cannot survive without a stylesheet (they need a pseudo-class or a
# sibling combinator). They are dropped rather than half-applied.
UNINLINABLE = re.compile(r":(hover|target|focus|active|visited)|~|\+")


# --------------------------------------------------------------------------
# stylesheet
# --------------------------------------------------------------------------

class Rule:
    __slots__ = ("selector", "parts", "decls")

    def __init__(self, selector: str, decls: str):
        self.selector = selector
        self.parts = parse_selector(selector)
        self.decls = decls


class Compound:
    """One simple selector plus the combinator that ties it to its left."""
    __slots__ = ("tag", "classes", "last_child", "combinator")

    def __init__(self, tag, classes, last_child, combinator):
        self.tag = tag
        self.classes = classes
        self.last_child = last_child
        self.combinator = combinator  # 'descendant' | 'child' | None (leftmost)


def parse_selector(selector: str) -> list[Compound] | None:
    """Parse the small subset of CSS this page uses. None = unsupported."""
    tokens = selector.replace(">", " > ").split()
    parts: list[Compound] = []
    combinator = None
    for token in tokens:
        if token == ">":
            combinator = "child"
            continue
        last_child = ":last-child" in token
        token = token.replace(":last-child", "")
        if token == "*":
            return None  # handled by the box-sizing compensation instead
        classes = re.findall(r"\.([\w-]+)", token)
        tag = re.match(r"^([\w-]+)", token)
        parts.append(Compound(tag.group(1).lower() if tag else None,
                              classes, last_child,
                              combinator or ("descendant" if parts else None)))
        combinator = None
    return parts or None


def load_stylesheet(text: str) -> tuple[list[Rule], str]:
    """Return (rules in source order, document with the <style> block removed)."""
    # Anchored to the start of a line so a mention of the tag in prose or in a
    # comment cannot be mistaken for the real block -- which silently truncates
    # the enclosing comment and swallows the markup after it.
    match = re.search(r"^[ \t]*<style>(.*?)^[ \t]*</style>[ \t]*$", text, re.S | re.M)
    if not match:
        sys.exit("index.src.html has no stylesheet block -- nothing to inline.")

    css = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
    rules: list[Rule] = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        decls = " ".join(body.split()).strip().rstrip(";")
        if not decls:
            continue
        # An element sized with a percentage and given padding needs
        # border-box, which used to come from the `.amc *` rule.
        if "padding" in decls and "box-sizing" not in decls:
            decls += "; box-sizing: border-box"
        for selector in selectors.split(","):
            selector = selector.strip()
            if not selector or UNINLINABLE.search(selector):
                continue
            rule = Rule(selector, decls)
            if rule.parts:
                rules.append(rule)

    document = text[:match.start()] + text[match.end():]
    return rules, document


# --------------------------------------------------------------------------
# document tree (source spans only -- the text itself is never re-serialised)
# --------------------------------------------------------------------------

class Node:
    __slots__ = ("tag", "classes", "parent", "children", "start", "in_svg")

    def __init__(self, tag, classes, parent, start, in_svg):
        self.tag = tag
        self.classes = classes
        self.parent = parent
        self.children: list[Node] = []
        self.start = start
        self.in_svg = in_svg


class TreeBuilder(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.line_starts.append(i + 1)
        self.root = Node(None, [], None, 0, False)
        self.stack = [self.root]
        self.nodes: list[Node] = []

    def abs_offset(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def _open(self, tag, attrs):
        parent = self.stack[-1]
        classes = ""
        for name, value in attrs:
            if name == "class":
                classes = value or ""
        node = Node(tag, classes.split(), parent, self.abs_offset(),
                    parent.in_svg or tag == "svg")
        parent.children.append(node)
        self.nodes.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._open(tag, attrs)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def compound_matches(part: Compound, node: Node) -> bool:
    if part.tag and part.tag != node.tag:
        return False
    if any(c not in node.classes for c in part.classes):
        return False
    if part.last_child:
        siblings = node.parent.children if node.parent else []
        if not siblings or siblings[-1] is not node:
            return False
    return True


def rule_matches(rule: Rule, node: Node) -> bool:
    parts = rule.parts
    if not compound_matches(parts[-1], node):
        return False
    current = node.parent
    for i in range(len(parts) - 2, -1, -1):
        # The combinator joining parts[i] to parts[i + 1] is stored on the right one.
        if parts[i + 1].combinator == "child":
            if current is None or not compound_matches(parts[i], current):
                return False
        else:
            while current is not None and not compound_matches(parts[i], current):
                current = current.parent
            if current is None:
                return False
        current = current.parent
    return True


# --------------------------------------------------------------------------
# rewriting
# --------------------------------------------------------------------------

def tag_span(text: str, start: int) -> int:
    """Offset just past the '>' that closes the tag opening at `start`."""
    i, quote = start + 1, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return i + 1
        i += 1
    sys.exit(f"unterminated tag at offset {start}")


def inject(tag_src: str, decls: str) -> str:
    """Merge `decls` into the tag's style attribute; anything already inline wins."""
    match = re.search(r"""\sstyle\s*=\s*(["'])(.*?)\1""", tag_src, re.S)
    if match:
        existing = " ".join(match.group(2).split()).strip().rstrip(";")
        merged = f"{decls}; {existing}" if existing else decls
        return tag_src[:match.start()] + f' style="{quote(merged)}"' + tag_src[match.end():]
    close = "/>" if tag_src.rstrip().endswith("/>") else ">"
    return tag_src[:tag_src.rfind(close)].rstrip() + f' style="{quote(decls)}"' + close


def quote(value: str) -> str:
    """Make a declaration list safe inside a double-quoted attribute.

    Font stacks such as `"Segoe UI"` carry double quotes, which would otherwise
    close the attribute early and drop every declaration after them.
    """
    return value.replace('"', "&quot;")


def to_ascii(text: str) -> str:
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
        else:
            name = html.entities.codepoint2name.get(ord(ch))
            out.append(f"&{name};" if name else f"&#{ord(ch)};")
    return "".join(out)


def main() -> None:
    source = SRC.read_text(encoding="utf-8")
    rules, document = load_stylesheet(source)

    builder = TreeBuilder(document)
    builder.feed(document)
    builder.close()

    edits = []
    styled = 0
    for node in builder.nodes:
        if node.in_svg:
            continue
        decls: dict[str, str] = {}
        for rule in rules:                     # source order == cascade order
            if rule_matches(rule, node):
                for decl in rule.decls.split(";"):
                    if ":" in decl:
                        prop, value = decl.split(":", 1)
                        decls[prop.strip()] = value.strip()
        if decls:
            flat = "; ".join(f"{p}: {v}" for p, v in decls.items())
            end = tag_span(document, node.start)
            edits.append((node.start, end, inject(document[node.start:end], flat)))
            styled += 1

    for start, end, replacement in sorted(edits, reverse=True):
        document = document[:start] + replacement + document[end:]

    document = to_ascii(document)
    if any(ord(ch) > 127 for ch in document):
        sys.exit("output is not pure ASCII")

    OUT.write_text(document, encoding="ascii")
    print(f"{OUT.name}: {styled} elements styled inline, "
          f"{len(rules)} rules applied, {len(document):,} bytes, ASCII only")


if __name__ == "__main__":
    main()
