import pytest

from evedm.resolver import TargetResolver


@pytest.fixture(scope="module")
def resolver(graph):
    return TargetResolver(graph)


def names(items):
    return [i["name"] for i in items]


def test_autocomplete_prefix_first(resolver):
    out = names(resolver.autocomplete("jit"))
    assert out[0] == "Jita"


def test_autocomplete_is_case_insensitive_and_needs_two_chars(resolver):
    assert names(resolver.autocomplete("TAMA"))[0] == "Tama"
    assert resolver.autocomplete("t") == []


def test_autocomplete_substring_after_prefix(resolver):
    out = names(resolver.autocomplete("caldari", limit=5))
    assert "New Caldari" in out


def test_autocomplete_limit(resolver):
    assert len(resolver.autocomplete("a", limit=3)) == 0
    assert len(resolver.autocomplete("am", limit=3)) == 3


def test_dotlan_system_link_wins(resolver):
    text = "Fortizar under attack in Amarr region, see https://evemaps.dotlan.net/system/Tama for details"
    assert names(resolver.extract(text)) == ["Tama"]


def test_dotlan_map_and_route_links(resolver):
    assert names(resolver.extract("https://evemaps.dotlan.net/map/Essence/Old_Man_Star")) == ["Old Man Star"]
    assert names(resolver.extract("route https://evemaps.dotlan.net/route/2:Jita:Amarr")) == ["Jita", "Amarr"]


def test_ingame_showinfo_link(resolver):
    assert names(resolver.extract("<url=showinfo:5//30002813>Tama</url>")) == ["Tama"]


def test_plain_names_including_multi_word(resolver):
    text = "Structure in Old Man Star reinforced, timer near New Caldari and 1DQ1-A"
    assert names(resolver.extract(text)) == ["Old Man Star", "New Caldari", "1DQ1-A"]


def test_ambiguous_text_returns_all_candidates_in_order(resolver):
    assert names(resolver.extract("Moving from Jita to Perimeter, then jita again")) == ["Jita", "Perimeter"]


def test_no_system_found(resolver):
    assert resolver.extract("nothing to see here") == []
