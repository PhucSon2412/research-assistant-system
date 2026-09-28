"""Bounded keyword query for natural-language questions.

``plainto_tsquery`` ANDs every token, so a question such as "What rank do the
adapters have?" also requires "what", "do" and "the" in the chunk. Here
PostgreSQL's own parser (``ts_debug`` with the ``simple`` configuration that
builds ``index_entries.keywords``) tokenizes the input, so every lexeme is
normalized exactly as the index is and no reindex is needed. Function words and
out-of-range lengths are dropped, a hyphenated compound becomes one term made
of its parts, and at most ``keyword_max_terms`` distinct terms are kept in input
order. A chunk must match ``min_match`` of the kept terms: the configured share,
at least ``keyword_min_match_terms``, at most all of them. The tsquery text is
built only from quoted lexemes and bound as a parameter, never spliced into SQL.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import SearchPolicy

TS_CONFIG = "simple"  # must match index_entries.keywords (migration 0009)

# PostgreSQL's Snowball english.stop list plus a few question words, and common
# Vietnamese function syllables. Topic words are deliberately not listed.
STOPWORDS = frozenset("""
i me my myself we our ours ourselves you your yours yourself yourselves he him his himself she her
hers herself it its itself they them their theirs themselves what which who whom this that these
those am is are was were be been being have has had having do does did doing a an the and but if or
because as until while of at by for with about against between into through during before after
above below to from up down in out on off over under again further then once here there when where
why how all any both each few more most other some such no nor not only own same so than too very s
t can will just don should now
whose much many would could may might must shall also per via
và của là các những có không được trong cho với về gì nào bao nhiêu như thế này đó khi để từ một bị
thì mà hay hoặc ở ra sao đã sẽ đang rằng theo tại
""".split())

COMPOUND = frozenset({"asciihword", "hword", "numhword"})
PART = frozenset({"hword_asciipart", "hword_part", "hword_numpart"})


def quote(lexeme: str) -> str:
    """One tsquery operand matching exactly this lexeme (tsquery input quoting)."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


@dataclass(frozen=True)
class KeywordQuery:
    terms: tuple[tuple[str, ...], ...]  # each term: lexemes that must all occur
    min_match: int
    dropped: int  # distinct useful terms beyond the cap

    @property
    def status(self) -> str:
        return "ok" if self.terms else "no_terms"

    def term_queries(self) -> list[str]:
        return [" & ".join(quote(lexeme) for lexeme in term) for term in self.terms]

    def any_query(self) -> str:
        return " | ".join(f"({q})" for q in self.term_queries())

    def summary(self) -> dict:
        return {"status": self.status, "terms": len(self.terms), "min_match": self.min_match,
                "dropped_terms": self.dropped}


def _useful(lexeme: str, policy: SearchPolicy) -> bool:
    return (lexeme not in STOPWORDS
            and policy.keyword_min_term_chars <= len(lexeme) <= policy.keyword_max_term_chars)


def min_match(terms: int, policy: SearchPolicy) -> int:
    if not terms:
        return 0
    share = math.ceil(policy.keyword_min_match_fraction * terms)
    return min(terms, max(policy.keyword_min_match_terms, share))


def terms_from_tokens(tokens: list[tuple[str, list[str] | None]], policy: SearchPolicy) -> KeywordQuery:
    """Pure part of ``build``: ``tokens`` are the ``(alias, lexemes)`` rows of ``ts_debug``."""
    terms: list[tuple[str, ...]] = []
    parts: list[str] | None = None  # useful parts of the open hyphenated compound
    whole: list[str] = []

    def close() -> None:
        nonlocal parts
        if parts is not None:
            # A compound whose parts are all function words keeps the whole lexeme if useful.
            term = tuple(dict.fromkeys(parts)) or tuple(whole)
            if term:
                terms.append(term)
        parts = None

    for alias, lexemes in tokens:
        if parts is not None and alias in PART:
            parts.extend(lexeme for lexeme in lexemes or () if _useful(lexeme, policy))
            continue
        if parts is not None and not lexemes:
            continue  # the hyphen between two parts
        close()
        useful = [lexeme for lexeme in lexemes or () if _useful(lexeme, policy)]
        if alias in COMPOUND:
            parts, whole = [], useful
        elif useful:
            terms.append(tuple(useful))
    close()
    distinct = list(dict.fromkeys(terms))
    kept = tuple(distinct[:policy.keyword_max_terms])
    return KeywordQuery(kept, min_match(len(kept), policy), len(distinct) - len(kept))


def build(db: Session, query: str, policy: SearchPolicy) -> KeywordQuery:
    rows = db.execute(text("SELECT alias, lexemes FROM ts_debug(CAST(:config AS regconfig), :q)"),
                      {"config": TS_CONFIG, "q": query}).all()
    return terms_from_tokens([(row.alias, row.lexemes) for row in rows], policy)
