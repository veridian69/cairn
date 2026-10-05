"""literal-terms/v1: a deterministic local anchor, not FTS5 or semantic matching.

Matching runs on a casefolded copy with an index back to original scalars; a
match is admissible only when both endpoints fall on whole original scalars.
No Unicode normalisation. Earliest original start wins; ties prefer the longest
original span, then longest folded term, then codepoint order.

The full query (leading and trailing whitespace stripped, internal whitespace
preserved exactly) is tried first and needs no word boundaries. Otherwise each
whitespace-separated folded term must sit on word boundaries, judged on the
ORIGINAL neighbouring scalars: a word character is one for which Python's
``str.isalnum()`` (per the runtime's Unicode database) is true, or ``_``.

Casefolding never shortens a scalar, so when the folded text has the same
length as the original the offsets are the identity and no index is built.
Only when some scalar expands is a compact ``array('I')`` owner map used.
"""

from array import array

MAX_TERMS = 32
LOCATOR_POLICY = "literal-terms/v1"


class TooManyTerms(Exception):
    pass


def _owners(text: str) -> array[int]:
    """Original scalar index owning each folded character."""
    owner: array[int] = array("I")
    for index, character in enumerate(text):
        owner.extend([index] * len(character.casefold()))
    return owner


def _word(character: str) -> bool:
    return character.isalnum() or character == "_"


def locate(text: str, query: str) -> tuple[int, int] | None:
    folded_query = query.strip().casefold()
    terms = sorted(set(folded_query.split()), key=lambda t: (-len(t), t))
    if not terms:
        return None
    if len(terms) > MAX_TERMS:
        raise TooManyTerms
    folded = text.casefold()
    owner = None if len(folded) == len(text) else _owners(text)

    def mapped(position: int, length: int) -> tuple[int, int] | None:
        if owner is None:
            return position, position + length
        end = position + length - 1
        if (position > 0 and owner[position - 1] == owner[position]) or (
            end + 1 < len(owner) and owner[end + 1] == owner[end]
        ):
            return None
        return owner[position], owner[end] + 1

    position = folded.find(folded_query)
    while position != -1:
        span = mapped(position, len(folded_query))
        if span is not None:
            return span
        position = folded.find(folded_query, position + 1)
    best: tuple[int, int] | None = None
    for term in terms:
        position = folded.find(term)
        while position != -1 and (
            best is None or (position if owner is None else owner[position]) <= best[0]
        ):
            span = mapped(position, len(term))
            if (
                span is not None
                and (span[0] == 0 or not _word(text[span[0] - 1]))
                and (span[1] == len(text) or not _word(text[span[1]]))
            ):
                # Earlier start wins; at the same start the longer span wins;
                # exact ties keep the earlier term in (longest, codepoint) order.
                if best is None or (span[0], -(span[1] - span[0])) < (
                    best[0],
                    -(best[1] - best[0]),
                ):
                    best = span
                break
            position = folded.find(term, position + 1)
    return best
