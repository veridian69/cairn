import pytest

from cairn.authority.literal_locator import TooManyTerms, locate


def test_full_literal_beats_an_earlier_single_term() -> None:
    text = "alpha beta\nthe gamma delta here"
    assert locate(text, "gamma delta") == (15, 26)


def test_full_literal_preferred_over_an_earlier_term() -> None:
    assert locate("delta x gamma delta", "gamma delta") == (8, 19)


def test_terms_require_word_boundaries() -> None:
    assert locate("cartography cart", "cart zebra") == (12, 16)


def test_single_term_query_is_the_full_literal_and_needs_no_boundaries() -> None:
    assert locate("cartography cart", "cart") == (0, 4)


def test_ties_prefer_the_longest_term_at_the_same_start() -> None:
    assert locate("abc", "a abc") == (0, 3)


def test_casefold_expansion_matches_the_whole_scalar() -> None:
    assert locate("Die Straße", "STRASSE") == (4, 10)


def test_partial_casefold_expansion_never_matches() -> None:
    assert locate("ß", "s") is None


def test_full_literal_ignores_boundaries_and_keeps_internal_whitespace() -> None:
    assert locate("xxfoo  baryy", "foo  bar") == (2, 10)
    assert locate("xxfoo baryy", "foo  bar") is None


def test_no_unicode_normalisation() -> None:
    assert locate("caf\u00e9", "cafe\u0301") is None


def test_too_many_terms_is_refused() -> None:
    with pytest.raises(TooManyTerms):
        locate("x", " ".join(f"t{i}" for i in range(33)))


def test_no_match_returns_none() -> None:
    assert locate("nothing here", "absent") is None


def test_full_literal_ignores_outer_whitespace() -> None:
    assert locate("gamma delta", " gamma delta\n") == (0, 11)


def test_blank_query_returns_none() -> None:
    assert locate("a b", " ") is None
    assert locate("a b", "") is None


def test_ligature_and_dotted_capital_i() -> None:
    assert locate("\ufb01le", "FILE") == (0, 3)
    assert locate("\ufb01le", "f") is None
    assert locate("\u0130stanbul", "istanbul") is None


def test_identity_and_expansion_paths_agree() -> None:
    cases = [
        ("one two gamma delta", "gamma delta"),
        ("cartography cart", "cart zebra"),
        ("alpha cart-wheel", "wheel cart"),
        ("nothing here", "absent"),
        ("abc", "a abc"),
    ]
    for text, query in cases:
        plain = locate(text, query)
        for extra in ("\u00df", "\ufb01"):
            shift = len(extra) + 1
            moved = locate(extra + " " + text, query)
            assert moved == (
                None if plain is None else (plain[0] + shift, plain[1] + shift)
            )
            assert locate(text + " " + extra, query) == plain
