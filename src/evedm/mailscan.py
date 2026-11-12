"""Structure and owner links in the HTML of in-game mails (STR-14). Pure functions: no I/O, nothing is stored here.

A pilot drags a structure's link into a mail, then the link of its owner corporation, one pair after another. ESI
returns the body as EVE HTML; a game link is ``<a href="showinfo:<type_id>//<item_id>">text</a>`` (a single ``/`` is
accepted too, a type-only link ``showinfo:587`` has no item). The scout keeps only what these functions return: the
structure ID, its type, the link text (the structure's name) and the owner corporation's ID. The body itself, the
subject, the sender and the recipients are never looked at.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import NamedTuple

from .universe import GateGraph

STRUCTURE_MIN_ID = 1_000_000_000_000  # structures, customs offices, corporation offices and all items live above this
CORPORATION_TYPE_ID = 2  # alliances (16159) are never owners
MAX_BODY_CHARS = 200_000  # a longer body is ignored
MAX_FINDS = 200  # per mail
MAX_NAME_CHARS = 200
# Upwell structure types (inventory category 65) known without asking ESI; the scout adds the types its type_groups
# table knows with category 65
UPWELL_TYPE_IDS = frozenset(
    {35825, 35826, 35827, 35832, 35833, 35834, 35835, 35836, 35840, 35841}
    | {37534, 40340, 47512, 47513, 47514, 47515, 47516, 81826}
)
_SHOWINFO = re.compile(r"showinfo:(\d{1,10})(?:/{1,2}(\d{1,19}))?")


class Link(NamedTuple):
    """One game link of a mail: ``item_id`` is None for a type-only link."""

    type_id: int
    item_id: int | None
    text: str


class MailFind(NamedTuple):
    """A structure named in a mail, with the corporation link that followed it (None: a lone structure link)."""

    structure_id: int
    type_id: int
    name: str | None
    owner_id: int | None


class _LinkParser(HTMLParser):
    """Collects the ``showinfo`` links in document order with their unescaped, whitespace-collapsed text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self._open: tuple[int, int | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        self._close()  # an <a> never closed: the next one ends it
        href = next((v for k, v in attrs if k == "href" and v), "")
        found = _SHOWINFO.fullmatch(href.strip())
        if found:
            self._open = (int(found[1]), int(found[2]) if found[2] else None)
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._close()

    def finish(self) -> None:
        """Close a link still open at the end of the text."""
        self._close()

    def _close(self) -> None:
        if self._open is not None:
            self.links.append(Link(self._open[0], self._open[1], " ".join("".join(self._text).split())))
        self._open, self._text = None, []


def parse_links(body: str) -> list[Link]:
    """The game links of a mail body in document order (``[]`` for a body longer than ``MAX_BODY_CHARS``)."""
    if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
        return []
    parser = _LinkParser()
    parser.feed(body)
    parser.close()
    parser.finish()
    return parser.links


def _is_corporation(link: Link) -> bool:
    cid = link.item_id or 0
    return link.type_id == CORPORATION_TYPE_ID and (98_000_000 <= cid < 2_147_483_647 or 1_000_000 <= cid < 2_000_000)


def structure_finds(body: str, is_structure_type: Callable[[int], bool]) -> list[MailFind]:
    """Structures named in a mail, each with the corporation link that comes next in the document.

    A link is a structure when its item ID is at least 1e12 and ``is_structure_type(type_id)`` holds. A structure
    followed by anything but a corporation link has no owner (it is still a find). Every link is the next link of
    one other link at most, so a corporation link belongs to one structure only. A structure named twice in one mail
    keeps its last pair; a later lone link does not erase an owner an earlier pair gave it.
    """
    links = parse_links(body)
    finds: dict[int, MailFind] = {}
    for i, link in enumerate(links):
        if link.item_id is None or link.item_id < STRUCTURE_MIN_ID or not is_structure_type(link.type_id):
            continue
        nxt = links[i + 1] if i + 1 < len(links) else None
        owner = nxt.item_id if nxt is not None and _is_corporation(nxt) else None
        previous = finds.get(link.item_id)
        if previous is not None and owner is None:
            owner = previous.owner_id
        elif previous is None and len(finds) >= MAX_FINDS:
            break
        finds[link.item_id] = MailFind(link.item_id, link.type_id, link.text[:MAX_NAME_CHARS] or None, owner)
    return list(finds.values())


def system_of(name: str | None, graph: GateGraph) -> int | None:
    """The solar system ID named by the part of a structure name before the first " - " (None when unknown)."""
    if not name or " - " not in name:
        return None
    idx = graph.find(name.split(" - ", 1)[0])
    return None if idx is None else graph.system_id(idx)
