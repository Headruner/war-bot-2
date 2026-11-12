"""STR-14: the structure and corporation links in in-game mail bodies (pure parser, no I/O)."""

import pytest

from evedm.mailscan import (
    MAX_BODY_CHARS,
    MAX_FINDS,
    UPWELL_TYPE_IDS,
    Link,
    parse_links,
    structure_finds,
    system_of,
)

ASTRAHUS, FORTIZAR, RIFTER = 35832, 35833, 587
S1, S2, S3 = 1_035_466_617_946, 1_035_466_617_947, 1_035_466_617_948
CORP, CORP2 = 98_388_312, 98_000_123
upwell = UPWELL_TYPE_IDS.__contains__


def a(type_id: int, item_id: int | None, text: str, sep: str = "//") -> str:
    """One link the way the game writes it, inside the font tag it uses."""
    href = f"showinfo:{type_id}" + (f"{sep}{item_id}" if item_id is not None else "")
    return f'<font size="14" color="#ffd98d00"><a href="{href}">{text}</a></font>'


def test_a_structure_link_followed_by_a_corporation_link_is_one_find_with_its_owner():
    body = a(ASTRAHUS, S1, "Jita - Some Fort") + "<br>" + a(2, CORP, "Some Corp")
    [find] = structure_finds(body, upwell)
    assert (find.structure_id, find.type_id, find.name, find.owner_id) == (S1, ASTRAHUS, "Jita - Some Fort", CORP)


def test_pairs_one_after_another_each_get_their_own_owner():
    body = "<br>".join([a(ASTRAHUS, S1, "A - One"), a(2, CORP, "C1"), a(FORTIZAR, S2, "B - Two"), a(2, CORP2, "C2")])
    assert [(f.structure_id, f.owner_id) for f in structure_finds(body, upwell)] == [(S1, CORP), (S2, CORP2)]


def test_a_lone_structure_link_is_still_a_find_without_owner():
    body = f"Look at this: {a(ASTRAHUS, S1, 'Jita - Lonely')} and nothing else"
    assert [(f.structure_id, f.owner_id) for f in structure_finds(body, upwell)] == [(S1, None)]


def test_a_structure_followed_by_another_structure_has_no_owner_and_the_corporation_belongs_to_the_second():
    body = a(ASTRAHUS, S1, "A - One") + a(FORTIZAR, S2, "B - Two") + a(2, CORP, "Corp")
    assert [(f.structure_id, f.owner_id) for f in structure_finds(body, upwell)] == [(S1, None), (S2, CORP)]


def test_a_corporation_link_is_consumed_by_one_structure_only():
    body = a(ASTRAHUS, S1, "A - One") + a(2, CORP, "Corp") + a(2, CORP2, "Second corp")  # the 2nd corp is nobody's
    assert [(f.structure_id, f.owner_id) for f in structure_finds(body, upwell)] == [(S1, CORP)]


def test_an_alliance_link_is_never_an_owner():
    body = a(ASTRAHUS, S1, "A - One") + a(16159, 99_010_320, "Some Alliance")
    assert [(f.structure_id, f.owner_id) for f in structure_finds(body, upwell)] == [(S1, None)]


def test_only_real_corporation_ids_are_owners():
    for bad in (97_999_999, 2_147_483_647, 500_000, 2_000_000):  # between the player and NPC corporation ranges
        assert structure_finds(a(ASTRAHUS, S1, "A - One") + a(2, bad, "x"), upwell)[0].owner_id is None
    for good in (98_000_000, 2_147_483_646, 1_000_000, 1_999_999):  # player corporations and NPC corporations
        assert structure_finds(a(ASTRAHUS, S1, "A - One") + a(2, good, "x"), upwell)[0].owner_id == good


def test_links_that_are_not_structures_are_ignored():
    body = (
        a(RIFTER, 1_035_000_000_001, "a ship item")  # an item above 1e12 whose type is no structure
        + a(1377, 2_112_000_001, "a character")  # an ID far below 1e12
        + a(ASTRAHUS, 5_000_000, "too small an ID")
        + a(RIFTER, None, "type only")
        + a(ASTRAHUS, S1, "Jita - Real")
    )
    assert [f.structure_id for f in structure_finds(body, upwell)] == [S1]


def test_the_type_predicate_decides_what_a_structure_is():
    body = a(99_001, S1, "Jita - Unknown kind")
    assert structure_finds(body, upwell) == []
    assert [f.structure_id for f in structure_finds(body, lambda t: t == 99_001)] == [S1]


def test_a_single_slash_and_type_only_links_are_parsed():
    links = parse_links(a(ASTRAHUS, S1, "One", sep="/") + a(RIFTER, None, "Rifter") + a(2, CORP, "Corp"))
    assert links == [Link(ASTRAHUS, S1, "One"), Link(RIFTER, None, "Rifter"), Link(2, CORP, "Corp")]


def test_other_links_and_links_without_an_address_are_not_game_links():
    body = '<a href="https://example.com/x">web</a><a name="anchor">no href</a><a href="killReport:1:abc">kill</a>'
    assert parse_links(body) == []
    assert parse_links(a(ASTRAHUS, S1, "One") + '<a href="showinfo:abc//x">bad</a>') == [Link(ASTRAHUS, S1, "One")]


def test_link_text_is_unescaped_and_whitespace_is_collapsed():
    body = a(ASTRAHUS, S1, "  Jita   -\n Tom &amp; Jerry&#39;s  &lt;Fort&gt; ")
    assert structure_finds(body, upwell)[0].name == "Jita - Tom & Jerry's <Fort>"


def test_nested_markup_in_the_link_text_counts_as_text_and_an_open_link_ends_with_the_text():
    assert parse_links('<a href="showinfo:35832//1035466617946">Jita - <b>Bold</b> Fort')[0].text == "Jita - Bold Fort"


def test_an_empty_name_is_none_and_a_long_one_is_cut():
    assert structure_finds(a(ASTRAHUS, S1, "   "), upwell)[0].name is None
    assert len(structure_finds(a(ASTRAHUS, S1, "x" * 500), upwell)[0].name) == 200


def test_a_structure_named_twice_keeps_its_last_pair_but_a_later_lone_link_keeps_the_owner():
    twice = a(ASTRAHUS, S1, "A - Old") + a(2, CORP, "C1") + a(ASTRAHUS, S1, "A - New") + a(2, CORP2, "C2")
    [find] = structure_finds(twice, upwell)
    assert (find.name, find.owner_id) == ("A - New", CORP2)
    lone_later = a(ASTRAHUS, S1, "A - Old") + a(2, CORP, "C1") + a(ASTRAHUS, S1, "A - New")
    [find] = structure_finds(lone_later, upwell)
    assert (find.name, find.owner_id) == ("A - New", CORP)  # the later link has no corporation: the pair stays


def test_a_mail_names_at_most_200_structures_and_a_huge_body_is_ignored():
    many = "".join(a(ASTRAHUS, 1_000_000_000_000 + n, f"S - {n}") for n in range(MAX_FINDS + 50))
    assert len(structure_finds(many, upwell)) == MAX_FINDS
    assert structure_finds(a(ASTRAHUS, S1, "x") + " " * MAX_BODY_CHARS, upwell) == []
    assert parse_links(" " * (MAX_BODY_CHARS + 1)) == []
    assert structure_finds("", upwell) == [] and structure_finds(None, upwell) == []  # type: ignore[arg-type]


def test_the_well_known_upwell_types_are_listed():
    assert {35825, 35832, 35833, 35834, 35835, 35836, 35840, 35841, 37534, 40340, 81826} <= UPWELL_TYPE_IDS


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Jita - Some Fort", 30000142),
        ("jita - lower case", 30000142),
        ("  Jita  - padded", 30000142),
        ("1DQ1-A - Keep", 30004759),
        ("J-Home - x", None),  # no such system
        ("Jita", None),  # no " - ": not a structure name
        ("Jita-Tradehub", None),
        ("", None),
        (None, None),
    ],
)
def test_the_system_comes_from_the_text_before_the_first_dash(graph, name, expected):
    assert system_of(name, graph) == expected


def test_only_the_first_dash_separates_the_system():
    class Graph:  # a name with a second dash still reads its first part
        def find(self, name):
            return 3 if name == "Aaa" else None

        def system_id(self, i):
            return 1000 + i

    assert system_of("Aaa - Bbb - Ccc", Graph()) == 1003  # type: ignore[arg-type]
