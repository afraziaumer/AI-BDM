"""Diagnostic: rank EVERY stored chunk against a query and show the top matches
with their similarity score and full text. No LLM call — pure retrieval, so you
can see exactly what the system considers "closest in meaning" before any
answer is generated.

HYBRID SCORING: the final score is semantic similarity PLUS an IDF-weighted
keyword-overlap bonus. Pure embedding similarity can bury a chunk that
literally contains the answer (e.g. "it has no technical app") if that phrase
is a small fraction of a chunk otherwise full of unrelated text (address,
boilerplate) — the model encodes the whole chunk's average meaning, so a short
buried signal gets diluted. The keyword bonus is a safety net: if a chunk
literally contains a meaningful word from the query, it gets a bonus,
regardless of how the pure semantic score alone ranked it.

IDF WEIGHTING (the "improve it more" refinement): not every matched word is
equally meaningful. If every business here is "in New York," then a chunk
matching "new"/"york" tells you nothing distinguishing — nearly every chunk
matches. A chunk matching "app" (rare — only one business's text mentions it)
IS distinguishing. So each matched word's bonus is scaled by how RARE it is
across the current candidate set: words that appear in almost every chunk get
almost no bonus; words that appear in only one or two chunks get close to the
full bonus. This is the standard IDF (inverse document frequency) idea from
search engines, computed fresh per query — nothing hardcoded.

FUZZY (embedding-based) WORD MATCHING: literal matching alone misses different
wording for the same concept — a chunk saying "application" should still count
as answering a query about "app", even though neither is a literal substring
of the other. Rather than a hand-written synonym list (which only covers
pairs someone thought to add), each significant word is embedded individually
with the SAME model already used for chunks/queries, and a query word is
considered present in a chunk if either (a) it's a literal whole-word match, or
(b) some word actually in the chunk has embedding similarity to it at or above
MIN_FUZZY_SIMILARITY. This generalizes to any related word pair the model
understands as similar, not just ones anticipated in advance.

NEGATION AWARENESS: matching a word is not the same as matching its MEANING —
"we just launched our app!" and "we have no app" both literally contain "app",
but say the opposite thing. Neither pure semantic similarity nor keyword
matching alone distinguishes this (embedding models are famously weak on
negation). A small, generic set of negation cues ("no", "not", "without",
"never", "n't", "lacking", ...) is checked in the short window immediately
BEFORE a matched word, in BOTH the query and the chunk. If the query's
polarity for that word ("no app" -> negated) doesn't match the chunk's
polarity for its matching word ("just launched our app" -> not negated), the
match is heavily discounted — still visible, but no longer treated as
confidently answering the query. This is a proximity heuristic, not true
parsing: it correctly catches the dominant real-world pattern this project's
own data actually uses ("no X", "it has no technical X", "not have an X",
"without a X"). Separately, longer "soft negation" phrases that follow the
word ("X in progress", "X coming soon", "X under development") are ALSO
recognized — these mean "doesn't exist yet," functionally the same absence a
"no X" query wants, even with no explicit no/not/without present. These are
checked in the opposite direction (after, not before) from the single-word
cues; see _negation_flag's docstring for why that's safe here but isn't for
the single-word case.

The "meaningful words" are extracted generically from whatever query is
passed in (common stopwords removed) — nothing here is hardcoded to any
specific business/industry/query.

PHRASE-ADJACENCY GATING: two query words that sit next to each other ("new
york") carry a different meaning together than either word alone. "new" on
its own is one of the most generic adjectives in English and fires constantly
in unrelated marketing copy ("brand-new steering wheel", "new construction"),
diluting results with noise unrelated to the actual query. For each such
adjacent pair, whichever word is LESS rare across the current candidate set
(lower IDF weight -- a proxy for "probably being used in its ordinary sense
here, not the specific phrase") only counts when its rarer partner is ALSO
present somewhere in the same chunk. The rarer partner (e.g. "york") is never
gated, so it keeps matching completely independently, including via fuzzy
matching (e.g. "NYC").

To let the keyword bonus actually rescue a chunk that scored poorly on pure
semantic similarity, EVERY matching chunk is fetched from the store (not just
the top-k) before scoring — otherwise a weak-semantic-score chunk would never
even be a candidate for the bonus to lift back up.

By default the query is NOT re-typed — it's read automatically from
last_run.json (the same query the scraper just used), consistent with the
rest of the rag/ automatic flow. Pass --query to override with something else.

Run (from the project root):
    python -m rag.top_matches
    python -m rag.top_matches --k 20
    python -m rag.top_matches --query "does this have a mobile app?"
    python -m rag.top_matches --business sachardental.com
    python -m rag.top_matches --keyword-boost 0        # pure semantic, no hybrid
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Union

from . import config

_spellchecker = None  # lazy singleton -- offline dictionary, loaded once


def _get_spellchecker():
    global _spellchecker
    if _spellchecker is None:
        from spellchecker import SpellChecker
        _spellchecker = SpellChecker()
    return _spellchecker


def _normalize_for_embedding(word: str) -> str:
    """Correct obvious typos before a chunk word is embedded for fuzzy
    matching. Rare/misspelled words (e.g. "appoinment" for "appointment") can
    get spuriously HIGH embedding similarity to an unrelated query word,
    purely as a tokenization artifact of the OOV misspelling -- the correctly
    spelled word usually does NOT have this problem (e.g. "app"~"appointment"
    = 0.30, safely low, vs "app"~"appoinment" = 0.63, a false positive). Only
    touches unknown/misspelled words with a confident correction; real words
    (including brand names/jargon the checker doesn't recognize but also
    can't confidently "fix") pass through unchanged.
    """
    checker = _get_spellchecker()
    if word in checker:
        return word
    correction = checker.correction(word)
    return correction if correction else word


# Generic English stopwords — filtered out when extracting "meaningful words"
# from a query. Not query- or domain-specific; just common function words that
# carry no distinguishing signal for keyword matching.
_STOPWORDS: Set[str] = {
    "a", "an", "the", "and", "or", "but", "if", "of", "at", "by", "for", "with",
    "without", "about", "against", "between", "into", "through", "during",
    "before", "after", "above", "below", "to", "from", "up", "down", "in", "out",
    "on", "off", "over", "under", "again", "further", "then", "once", "here",
    "there", "all", "any", "both", "each", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "can", "will", "just", "should", "now", "is", "are", "was",
    "were", "be", "been", "being", "have", "has", "had", "having", "do", "does",
    "did", "doing", "would", "could", "you", "your", "yours", "me", "my", "i",
    "we", "us", "our", "it", "its", "this", "that", "these", "those", "give",
    "get", "find", "want", "need", "please", "with",
}
# Whole-word matches only, so "app" can't fire on "happy"/"appetizer"/etc.
_WORD_RE = re.compile(r"[a-z0-9]+")

DEFAULT_KEYWORD_BOOST = 0.5   # MAX score bonus a chunk can get from keyword matches
                              # (reached at combined rarity weight == 1.0; see
                              # _combined_keyword_weight's diminishing-returns combination).
                              # Raising this used to backfire (tried 0.7) by amplifying
                              # single-word ambiguity -- "new" (from "New York") matching ANY
                              # unrelated "new" in marketing copy ("brand-new steering wheel").
                              # That's now structurally impossible: _query_bigrams +
                              # phrase_gate (see top_matches()) make a generic word like "new"
                              # contribute ZERO unless its rarer partner ("york") is ALSO in
                              # the same chunk, regardless of this boost's size. With that
                              # noise source eliminated, raising the boost is safe again and
                              # lets a genuine, specific match (e.g. "app") outrank a chunk
                              # that's only vaguely on-topic (mentions "marina" but nothing
                              # about the actual thing asked about).

MIN_FUZZY_SIMILARITY = 0.60   # cosine similarity threshold for treating two
                              # DIFFERENT words as the same concept (e.g.
                              # "app" ~ "application") when neither is a
                              # literal substring of the other. Was 0.55, but
                              # that caught "phone"/"smartphone"/"iphone"
                              # (~0.55-0.565) as false-positive matches for
                              # "app" -- since nearly every business's contact
                              # info mentions "phone", this systematically
                              # polluted the confirmed_present/confirmed_absent
                              # evidence summary. 0.60 clears that cluster
                              # while keeping genuine matches like
                              # "application" (0.73) and "android" (0.675).

# Generic English negation cues — same category as _STOPWORDS, not specific to
# any business/industry/query. "n't" is checked separately since the word
# tokenizer (_WORD_RE) splits on apostrophes, so "doesn't"/"isn't"/"won't"
# etc. wouldn't otherwise be recognized as a single negation token.
# Checked in the window BEFORE a matched word only (see NEGATION_WINDOW_CHARS
# and _negation_flag's docstring for why).
_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|never|none|nor|neither|lacks?|lacking)\b|n't\b"
)

# "Soft" negation phrases — a feature that's "coming soon" or "in progress"
# doesn't EXIST yet, which is functionally the same absence a "no X" query is
# looking for, even though no explicit no/not/without appears. These are
# generic English phrases (not specific to apps or any topic) and, unlike the
# single-word cues above, are checked in the window AFTER a matched word --
# that's the natural English word order for this pattern ("application ...
# in progress", "checkout feature coming soon"). Checking after is safe here
# specifically because these are long, distinctive multi-word phrases: a
# short query is extremely unlikely to accidentally contain one near an
# unrelated word, unlike the single common word "no" (see _negation_flag).
#
# "coming"/"launching" require an actual time-reference after them (not just
# any word — "events coming up this weekend" isn't the same "not yet built"
# signal as "app coming next season") so "coming soon/later/next X/this X/in
# <year>/<a season name>" are all recognized, without turning into a blanket
# match on every unrelated use of "coming".
_TIME_REF = r"(?:soon|later|next \w+|this \w+|in \d{4}|spring|summer|fall|autumn|winter)"
_SOFT_NEGATION_RE = re.compile(
    rf"in progress|(?:coming|launching) {_TIME_REF}|"
    r"under (?:development|construction)|"
    r"not yet available|not available yet|to be announced|in development"
)

NEGATION_WINDOW_CHARS = 40     # how far around a matched word to look for a
                              # negation cue (before for _NEGATION_RE, after
                              # for _SOFT_NEGATION_RE).
NEGATION_MISMATCH_PENALTY = 0.4    # multiply a match's weight by this when
                                   # the query and chunk disagree on polarity.
                                   # A chunk that definitively confirms the
                                   # OPPOSITE of what was asked ("download our
                                   # app" for a "no app" query) is still real,
                                   # useful information -- more informative
                                   # than a chunk that says nothing about the
                                   # topic at all -- so it should rank below
                                   # a genuine matching-polarity chunk, but
                                   # still clearly above uninformative filler.
                                   # (Previously 0.15, which crushed it below
                                   # generic no-signal content entirely.)

PHRASE_SOLO_WEIGHT = 0.5      # multiply a phrase-gated (generic-side) word's
                              # weight by this when its rarer partner ISN'T
                              # nearby, instead of dropping it to zero. A
                              # generic partner standing alone ("office"
                              # without "physical" nearby) is still often
                              # meaningful on its own (an "office" mention
                              # already implies a physical location) -- unlike
                              # a hard gate, which would silently discard that
                              # real signal just because the specific query
                              # phrasing happened to pair it with another word.

# --- Focus-semantic (context understanding) -------------------------------
# The full-query semantic score embeds the WHOLE query ("...marinas in new
# york with no app"), so it's dominated by the topical/location words and the
# actual ask ("app") gets drowned. FOCUS-SEMANTIC additionally scores each
# chunk against the embedding of the query's single most DISCRIMINATING term
# (highest IDF -- here "app"). Because embeddings understand meaning, this
# lifts chunks that are ABOUT that concept even when they use a different word
# ("application", "platform", "digital portal", "booking software") -- pure
# context, no synonym list, no literal/fuzzy overlap required.
FOCUS_SEMANTIC_WEIGHT = 0.55

# --- Saturation suppression -----------------------------------------------
# A query word that matches MOST candidate chunks carries no ranking signal:
# if every business in the store is a marina, matching "marina" tells you
# nothing, so it must not out-score the real ask. A word's keyword weight is
# tapered from full (at SATURATION_START fraction of chunks) down to zero (at
# SATURATION_FULL). Generic, corpus-relative -- nothing hardcoded.
SATURATION_START = 0.35
SATURATION_FULL = 0.80


def _significant_words(text: str) -> Set[str]:
    """Lowercase words (3+ chars), stopwords removed — the query's real signal."""
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _query_bigrams(query: str) -> List[tuple[str, str]]:
    """Adjacent pairs of significant words in the query -- e.g. ("new", "york")
    in "...in new york with...". Two words that formed a PHRASE in the query
    carry a different meaning together than either does alone: "new" in "New
    York" means something specific, but "new" on its own is one of the most
    generic adjectives in English and matches constantly in unrelated
    marketing copy ("brand-new steering wheel", "new construction"). Detected
    generically from word ADJACENCY in the query text -- nothing hardcoded to
    "new"/"york" or any specific pair.
    """
    tokens = _WORD_RE.findall(query.lower())

    def _sig(w: str) -> bool:
        return len(w) >= 3 and w not in _STOPWORDS

    return [(a, b) for a, b in zip(tokens, tokens[1:]) if _sig(a) and _sig(b)]


def _phrase_proximity_match(text_lower: str, word_a: str, word_b: str,
                            window_chars: int = NEGATION_WINDOW_CHARS) -> bool:
    """True if `word_a` and `word_b` (the actual matched words in a chunk --
    literal or fuzzy) occur within `window_chars` of each other ANYWHERE in
    the text. A phrase-gate check that only asks "are both words present
    somewhere in this chunk" is not enough: two words can each genuinely
    appear in a short chunk while describing two UNRELATED things (e.g. a
    chunk mentioning both "a physical certificate" and "our office address"
    is not evidence of a "physical office"). Requiring them to sit near each
    other is a much better proxy for "these words are actually being used
    together as one concept," the same proximity-heuristic spirit as
    negation detection elsewhere in this file.
    """
    if not word_a or not word_b:
        return False
    try:
        positions_a = [m.start() for m in re.finditer(rf"\b{re.escape(word_a)}\b", text_lower)]
        positions_b = [m.start() for m in re.finditer(rf"\b{re.escape(word_b)}\b", text_lower)]
    except re.error:
        return False
    return any(abs(a - b) <= window_chars for a in positions_a for b in positions_b)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Embeddings are pre-normalized (config.NORMALIZE_EMBEDDINGS=True), so
    cosine similarity is just the dot product."""
    return sum(x * y for x, y in zip(a, b))


def _negation_flag(text_lower: str, word: str) -> Optional[bool]:
    """Is `word`'s (first) occurrence in `text_lower` negated?

    Two checks, in two directions, for two different reasons:

    1. Single-word cues ("no", "not", "without", "n't", "lacking", ...) are
       checked in the window immediately BEFORE the match only — covering
       the dominant real-world phrasing this project's own data actually
       uses ("no app", "it has no technical app", "without a CRM").
       Deliberately BEFORE-only: checking after the match too would also
       catch "X is not available" phrasing, but on short texts (like a whole
       query) a wide-enough window to reach that pattern also reaches across
       unrelated words earlier in the same short text — e.g. in "marina with
       no app", a bidirectional check would wrongly flag "marina" itself as
       negated by the LATER "no" that's really meant for "app". Before-only
       avoids that: nothing precedes the first word of a query, so it can't
       be falsely contaminated by a cue meant for a later word.

    2. Longer "soft negation" PHRASES ("in progress", "coming soon", "under
       development", ...) are checked in the window AFTER the match instead
       — that's the natural word order for this pattern ("application ... in
       progress"). Checking after is safe for these specifically because
       they're long, distinctive multi-word strings: a short query is far
       too unlikely to accidentally contain one near an unrelated word,
       unlike the single common word "no" in case 1.

    Returns None if `word` doesn't appear in the text at all (nothing to
    judge polarity of).

    A proximity heuristic, not real parsing — it can misfire on unusual
    phrasing, but catches the common patterns cheaply using only the regex
    tools already used elsewhere in this file.
    """
    m = re.search(rf"\b{re.escape(word)}\b", text_lower)
    if not m:
        return None
    before_start = max(0, m.start() - NEGATION_WINDOW_CHARS)
    if _NEGATION_RE.search(text_lower[before_start:m.start()]):
        return True
    after_end = min(len(text_lower), m.end() + NEGATION_WINDOW_CHARS)
    return bool(_SOFT_NEGATION_RE.search(text_lower[m.end():after_end]))


def _match_strengths(query_words: Set[str], docs: List[str], embedder,
                     query_negation: Dict[str, bool]
                     ) -> Dict[str, List[tuple[float, str, bool]]]:
    """For each significant query word, and for EACH candidate chunk (by
    index into `docs`), how strongly is that word's CONCEPT present in the
    chunk, which actual word in the chunk is responsible, and does the
    chunk's polarity (negated or not) disagree with the query's?

      (1.0, q, mismatch)      -- the word appears literally (whole-word, ci)
      (0<x<1, w, mismatch)    -- no literal match, but chunk word `w` is
                                 embedding-similar to it at/above
                                 MIN_FUZZY_SIMILARITY (e.g. chunk has
                                 "application", query word is "app")
      (0.0, "", False)        -- neither

    `mismatch` is True when the query negates this word (e.g. "no app") but
    the chunk's matching word is NOT negated there (e.g. "launched our app"),
    or vice versa — i.e. the words match but the meaning is opposite. The
    raw strength returned here is NOT yet penalized for this (that happens in
    _keyword_bonus_for_chunk, after IDF weights are computed) — document
    frequency should reflect whether the CONCEPT appears at all, regardless
    of polarity.

    Embeds every distinct significant word across the whole candidate set
    ONCE (not per query-word-per-chunk), reusing the same model instance
    already warmed up for the main query embedding.
    """
    if not query_words:
        return {}

    chunk_words_per_doc = [_significant_words(d) for d in docs]
    vocab = set().union(*chunk_words_per_doc) if chunk_words_per_doc else set()
    # Typo-normalize chunk vocabulary for EMBEDDING purposes only -- the raw
    # (possibly misspelled) word is still what gets reported as matched_word
    # and searched for in the text (via _negation_flag), since that's what's
    # actually there. Only the vector used for similarity comparison changes.
    norm_by_word = {w: _normalize_for_embedding(w) for w in vocab}
    words_to_embed = sorted(query_words | set(norm_by_word.values()))
    vectors = embedder.embed(words_to_embed) if words_to_embed else []
    vec_by_word = dict(zip(words_to_embed, vectors))

    strengths: Dict[str, List[tuple[float, str, bool]]] = {w: [] for w in query_words}
    for text, words in zip(docs, chunk_words_per_doc):
        text_lower = text.lower()
        for q in query_words:
            if re.search(rf"\b{re.escape(q)}\b", text_lower):
                s, matched_word = 1.0, q
            else:
                best, best_word = 0.0, ""
                q_vec = vec_by_word.get(q)
                if q_vec is not None:
                    for w in words:
                        if w == q:
                            continue
                        w_vec = vec_by_word.get(norm_by_word.get(w, w))
                        if w_vec is None:
                            continue
                        sim = _cosine(q_vec, w_vec)
                        if sim > best:
                            best, best_word = sim, w
                s, matched_word = (best, best_word) if best >= MIN_FUZZY_SIMILARITY else (0.0, "")

            mismatch = False
            if s > 0:
                chunk_negated = _negation_flag(text_lower, matched_word)
                if chunk_negated is not None and chunk_negated != query_negation.get(q, False):
                    mismatch = True
            strengths[q].append((s, matched_word, mismatch))
    return strengths


def _document_frequencies(strengths: Dict[str, List[tuple[float, str, bool]]]) -> Dict[str, int]:
    """For each query word, count how many candidate chunks it's present in
    (literal OR fuzzy match, i.e. strength > 0) — REGARDLESS of polarity
    mismatch, since IDF should reflect how common the CONCEPT is across
    chunks, not whether each mention agrees with the query's polarity.
    Computed fresh per query/candidate-set, never hardcoded to any word."""
    return {w: sum(1 for s, _, _ in vals if s > 0) for w, vals in strengths.items()}


def _idf_weights(doc_freq: Dict[str, int], total_docs: int) -> Dict[str, float]:
    """Turn each word's document frequency into a 0..1 rarity weight.

    1.0 = appears in (about) only one chunk -> maximally distinctive.
    0.0 = appears in every chunk -> no discriminating power at all.

    Uses smoothed IDF (log((N+1)/(df+1)) + 1), then min-max normalizes against
    the best possible score (df=1) and worst possible score (df=N) for this
    candidate set, so the weight is always a clean 0..1 regardless of corpus
    size.
    """
    import math
    if total_docs <= 1:
        return {w: 1.0 for w in doc_freq}
    best = math.log((total_docs + 1) / 2) + 1     # df = 1 (rarest possible)
    worst = math.log((total_docs + 1) / (total_docs + 1)) + 1  # df = N (in every doc)
    span = best - worst
    weights = {}
    for w, df in doc_freq.items():
        if df <= 0:
            weights[w] = 1.0  # never actually seen in this set; shouldn't happen for matched words
            continue
        idf = math.log((total_docs + 1) / (df + 1)) + 1
        weights[w] = ((idf - worst) / span) if span > 0 else 1.0
    return weights


def _saturation_factor(doc_freq: int, total_docs: int) -> float:
    """1.0 for a word matching up to SATURATION_START of chunks, tapering
    linearly to 0.0 at SATURATION_FULL. A word present in nearly every chunk
    (e.g. "marina" when every business is a marina) is already-satisfied
    context and should contribute ~no keyword signal -- otherwise it out-scores
    the query's actual discriminating ask. Multiplies the IDF weight; both are
    corpus-relative, nothing hardcoded."""
    if total_docs <= 0:
        return 1.0
    frac = doc_freq / total_docs
    if frac <= SATURATION_START:
        return 1.0
    if frac >= SATURATION_FULL:
        return 0.0
    return 1.0 - (frac - SATURATION_START) / (SATURATION_FULL - SATURATION_START)


def _combined_keyword_weight(weights: List[float]) -> float:
    """Combine several matched keywords' rarity weights with DIMINISHING
    RETURNS (a "noisy-OR": treat each weight as the probability that word
    alone is a meaningful signal, then combine as independent evidence).

    Without this, summing each word's weight lets several only-somewhat-rare
    words (e.g. "new"=0.40 + "york"=0.87 -> 1.27) out-total one truly rare,
    maximally-distinctive word (e.g. "app"=1.00) just by piling up matches --
    exactly the bug this fixes. Noisy-OR instead caps the combined weight at
    1.0 (the same ceiling as a single perfectly-rare word): stacking matches
    still helps, but each additional match contributes less than the last,
    so no pile of "somewhat common" words can ever be treated as a stronger
    signal than one genuinely distinctive word.
    """
    combined_miss = 1.0
    for w in weights:
        combined_miss *= (1.0 - max(0.0, min(1.0, w)))
    return 1.0 - combined_miss


def _keyword_bonus_for_chunk(doc_idx: int, query_words: Set[str],
                             strengths: Dict[str, List[tuple[float, str, bool]]],
                             idf_weights: Dict[str, float],
                             boost_per_word: float,
                             phrase_gate: Optional[Dict[str, str]] = None,
                             text_lower: str = "",
                             ) -> tuple[float, List[str], Dict[str, float]]:
    """IDF-weighted bonus with diminishing returns across multiple matches
    (literal or fuzzy), and a heavy penalty when the query and chunk disagree
    on polarity for a matched word (e.g. query says "no app", chunk says
    "launched our app"). `boost_per_word` is the MAXIMUM possible bonus for a
    chunk (reached only by a perfectly-rare literal match, or several matches
    whose combined noisy-OR weight approaches 1.0).

    `phrase_gate` maps a generic word to the rarer partner it formed a phrase
    with in the query (e.g. {"new": "york"}). A gated word's match is dropped
    entirely unless its partner ALSO matches NEAR it in this same chunk (see
    `_phrase_proximity_match`) -- "new" alone in unrelated marketing copy
    shouldn't score, but "New York" (or "new" alongside a genuine "york"/"NYC"
    mention nearby) should. Requiring PROXIMITY, not just co-presence anywhere
    in the chunk, matters: a chunk can genuinely contain both "physical" and
    "office" while describing two unrelated things ("a physical certificate"
    ... "our office address") -- co-presence alone would wrongly credit that
    as a "physical office" match. The rarer partner ("york") is never gated
    itself, so it still matches (literally or fuzzily, e.g. "NYC") completely
    independently.

    Returns (bonus, display_labels, per_label_effective_weight).
    display_labels look like "app" for a literal match, "app→application" for
    a fuzzy one, and get a " [opposite meaning?]" suffix when the polarity
    mismatch penalty was applied, so it's clear in the output WHY a chunk that
    contains the word still scored low.
    """
    if not query_words or boost_per_word <= 0:
        return 0.0, [], {}
    phrase_gate = phrase_gate or {}
    contributions: Dict[str, float] = {}
    for w in query_words:
        vals = strengths.get(w, [])
        s, matched_word, mismatch = vals[doc_idx] if doc_idx < len(vals) else (0.0, "", False)
        if s <= 0:
            continue
        partner = phrase_gate.get(w)
        phrase_solo = False
        if partner is not None:
            partner_vals = strengths.get(partner, [])
            partner_s, partner_matched_word, _ = (
                partner_vals[doc_idx] if doc_idx < len(partner_vals) else (0.0, "", False)
            )
            if partner_s <= 0 or not _phrase_proximity_match(
                text_lower, matched_word, partner_matched_word
            ):
                # No NEARBY sign of its phrase partner -- weaker evidence
                # (dampened, not dropped: the generic word can still be
                # meaningful standing alone, e.g. "office").
                phrase_solo = True
        weight = idf_weights.get(w, 1.0) * s
        if phrase_solo:
            weight *= PHRASE_SOLO_WEIGHT
        label = w if matched_word == w else f"{w}→{matched_word}"
        if phrase_solo:
            label += " [without phrase partner nearby]"
        if mismatch:
            weight *= NEGATION_MISMATCH_PENALTY
            label += " [opposite meaning?]"
        contributions[label] = weight
    if not contributions:
        return 0.0, [], {}
    matched = list(contributions.keys())
    combined = _combined_keyword_weight(list(contributions.values()))
    bonus = boost_per_word * combined
    return bonus, matched, contributions


def _query_from_last_run(path: str = "last_run.json") -> str:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"No {path} found and no --query given — run the scraper first "
            f'(e.g. python main.py --query "...") or pass --query "your question" directly.'
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    query = (data.get("query") or "").strip()
    if not query:
        raise SystemExit(f"{path} has no 'query' field.")
    return query


def top_matches(query: str, k: int = 10,
                business: Optional[Union[str, Sequence[str]]] = None,
                keyword_boost: float = DEFAULT_KEYWORD_BOOST,
                chroma_dir: Optional[str] = None,
                collection_name: Optional[str] = None,
                ) -> List[Dict[str, Any]]:
    """Return the top-k chunks closest to `query`, ranked together in ONE
    combined list — never split into per-business sections, so a strong match
    from a business with few chunks (e.g. 1) isn't hidden behind a business
    with many chunks (e.g. 35) filling up its own separate top-10.

    `business` restricts the candidate pool: a single domain string (one
    business only), a list of domains (rank only across those businesses
    combined — e.g. "just this run's committed businesses"), or None (every
    chunk currently in the store).

    `chroma_dir`/`collection_name` default to the production store
    (config.CHROMA_DIR/CHROMA_COLLECTION). Overriding them points this
    function at a different, isolated store instead — used by rag/eval.py so
    its fixed regression-test corpus never touches real scraped data.

    RANKING IS TIERED, not purely additive: chunks that have ANY real signal
    about the query's focus concept (e.g. "app" — literal match, fuzzy match,
    or negation-mismatched/affirmed) always rank above chunks with NO signal
    about it at all, regardless of how generically on-topic those other
    chunks otherwise look. Without this, a dense, single-topic corpus (every
    chunk scoring 0.5-0.7 on pure semantic similarity to "sports streaming
    Pakistan") can bury the one chunk that actually answers the question
    ("Download App") beneath dozens of chunks that say nothing about apps at
    all, just because their raw semantic score is a bit higher. Within each
    tier, the existing hybrid score (semantic + keyword + focus-semantic,
    negation-aware) still decides order — a genuine matching-polarity chunk
    still outranks an affirmed/opposite one within the "has signal" tier.

    Each result: {score, semantic_score, keyword_bonus, focus_lift,
    has_focus_signal, matched_keywords, text, url, title, domain, chunk_no}.
    semantic_score is cosine similarity (1 - Chroma's cosine distance, since
    the store is configured for cosine space and embeddings are normalized).
    Set keyword_boost=0 for pure semantic ranking (the old behavior) — this
    also disables the tiering, since it relies on the same match data.
    """
    # Load torch (via the embedder) BEFORE importing chromadb — on Windows,
    # chromadb pulls in onnxruntime, and if that initializes before torch the
    # two libraries' conflicting OpenMP runtimes segfault the process (see
    # embedder.py / pipeline.py for the same ordering requirement).
    from .embedder import Embedder
    embedder = Embedder()
    embedder.warmup()
    query_vector = embedder.embed_one(query)

    import chromadb
    client = chromadb.PersistentClient(path=chroma_dir or config.CHROMA_DIR)
    col = client.get_or_create_collection(
        name=collection_name or config.CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
    total = col.count()
    if total == 0:
        return []

    where = None
    if isinstance(business, str):
        where = {"domain": business.lower().removeprefix("www.")}
    elif business:  # non-empty list/sequence of domains
        domains = [d.lower().removeprefix("www.") for d in business]
        where = {"domain": {"$in": domains}} if len(domains) > 1 else {"domain": domains[0]}
    # Fetch EVERY matching chunk (not just top-k) so a chunk with a weak
    # semantic score but a strong literal keyword match can still be found and
    # boosted — restricting to top-k here would exclude it before it ever gets
    # a chance at the bonus.
    res = col.query(
        query_embeddings=[query_vector],
        n_results=total,
        where=where,
        include=["documents", "metadatas", "distances", "embeddings"],
    )

    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    embs = (res.get("embeddings") or [[]])[0]

    query_words = _significant_words(query)
    query_lower = query.lower()
    # Does the QUERY negate each significant word (e.g. "no app" -> True)?
    # Computed once — compared per-chunk in _match_strengths against how that
    # word/its fuzzy match is phrased in each candidate chunk.
    query_negation = {w: bool(_negation_flag(query_lower, w)) for w in query_words}
    # First pass over every candidate chunk: for each query word, is it
    # present literally or via a fuzzy (different-wording-same-concept) match,
    # how rare is that word across THIS candidate set, and does the chunk's
    # polarity for it agree with the query's? All needed before per-chunk
    # scoring.
    strengths = _match_strengths(query_words, docs, embedder, query_negation)
    doc_freq = _document_frequencies(strengths)
    idf_weights = _idf_weights(doc_freq, len(docs))
    # Taper the weight of "saturated" words (matching most chunks -- e.g.
    # "marina" when every business is a marina) to ~0, so they stop out-scoring
    # the query's actual discriminating ask. Applied on top of IDF.
    n_docs = len(docs)
    idf_weights = {w: iw * _saturation_factor(doc_freq.get(w, 0), n_docs)
                   for w, iw in idf_weights.items()}

    # Words that are part of a query PHRASE ("new york") describe location/
    # topic -- the FILTER, which every candidate already satisfies -- not the
    # information need. Exclude them from being chosen as the focus term (a
    # rare location word like "york" would otherwise beat the real ask "app"
    # on IDF alone and make focus-semantic score chunks against the wrong
    # concept).
    bigrams = _query_bigrams(query)
    phrase_words = {w for pair in bigrams for w in pair}

    # For each adjacent word-pair the query used as a phrase (e.g. "new
    # york"), the word with the LOWER idf_weight (more common across this
    # candidate set -> more likely being used in its ordinary, generic sense
    # rather than as part of the specific phrase) gets gated behind requiring
    # its rarer partner to also match NEARBY in the same chunk (computed
    # BEFORE focus-word selection below, since the fallback there needs it).
    # The rarer partner is never gated, so a standalone match on it (e.g.
    # "york" fuzzy-matching "NYC") is untouched.
    phrase_gate: Dict[str, str] = {}
    for w1, w2 in bigrams:
        if w1 not in idf_weights or w2 not in idf_weights or w1 == w2:
            continue
        generic, rare = (w1, w2) if idf_weights[w1] < idf_weights[w2] else (w2, w1)
        phrase_gate[generic] = rare

    # FOCUS-SEMANTIC: the query's single most discriminating term (highest
    # post-saturation weight among NON-phrase words -- here "app", not the
    # saturated "marina" nor the location word "york"). Each chunk is scored by
    # MEANING-similarity to it, lifting chunks that are about that concept even
    # in different words ("application", "platform", "digital portal").
    # Requires stored chunk embeddings; degrades gracefully to no lift if the
    # store didn't return them.
    # A query-NEGATED word ("no user login system" -> "user"/"login"/"system"
    # each sit right after "no") is a direct, explicit signal for "this is
    # the feature being asked about" -- a much stronger signal than mere
    # IDF-rarity. Prefer these first: without this, a query whose entire real
    # ask is a multi-word chain ("user login system" -> TWO overlapping
    # bigrams: "user"+"login", "login"+"system") gets ALL of its real-ask
    # words excluded as "phrase words" by the block below, leaving only an
    # unrelated non-chained word (e.g. "miami", the location FILTER) to win
    # by elimination -- exactly backwards from what the query intended.
    negated_non_phrase = {w: iw for w, iw in idf_weights.items()
                          if w not in phrase_words and query_negation.get(w, False)}
    negated_any = {w: iw for w, iw in idf_weights.items() if query_negation.get(w, False)}
    focus_candidates = {w: iw for w, iw in idf_weights.items() if w not in phrase_words}
    if negated_non_phrase:
        focus_candidates = negated_non_phrase
    elif negated_any:
        # Every negated word got swept into a phrase pair too -- still prefer
        # one of THEM over a non-negated, unrelated word.
        focus_candidates = negated_any
    elif not focus_candidates:
        # No negated word at all, AND every significant query word got swept
        # into a phrase pair (e.g. "no physical office" chains with "new
        # york" and "driving schools" -- short natural-language queries are
        # often ALL adjacent pairs). There is no word outside a phrase left
        # to pick. Falling back to the full idf_weights (old behavior) would
        # silently re-admit each phrase's GENERIC member too (e.g. "office",
        # "new") -- exactly what phrase exclusion exists to prevent. Instead
        # fall back to only the RARE half of each phrase pair -- the
        # never-gated partner that already carries real discriminating
        # signal on its own.
        focus_candidates = {w: idf_weights[w] for w in phrase_gate.values() if w in idf_weights}
    focus_pool = focus_candidates or idf_weights  # absolute last resort
    focus_word = max(focus_pool, key=focus_pool.get) if focus_pool else None
    focus_vec = embedder.embed_one(focus_word) if focus_word else None
    # If the chosen focus word is itself the rare half of a phrase pair (e.g.
    # "physical" in "physical office"), a bare match on it alone isn't
    # reliable evidence for the full two-word concept -- its partner must
    # also match NEAR it in the same chunk (see _phrase_proximity_match),
    # checked per-chunk below.
    _rare_to_generic = {rare: generic for generic, rare in phrase_gate.items()}
    focus_partner = _rare_to_generic.get(focus_word)

    results = []
    for doc_idx, (text, meta, dist) in enumerate(zip(docs, metas, dists)):
        meta = meta or {}
        text_lower = text.lower()
        semantic_score = 1.0 - dist
        bonus, matched, weights = _keyword_bonus_for_chunk(
            doc_idx, query_words, strengths, idf_weights, keyword_boost, phrase_gate,
            text_lower,
        )

        # Focus-semantic lift: how close is this chunk's MEANING to the
        # discriminating term. If this chunk affirms the focus concept while
        # the query negates it (or vice versa) -- a polarity mismatch on the
        # focus word -- the lift is suppressed the same way the keyword bonus
        # is, so "just launched our app" doesn't get lifted for a "no app"
        # query despite being semantically app-related.
        focus_lift = 0.0
        has_focus_signal = False
        focus_evidence = "no_evidence"   # default: focus concept never mentioned
        if focus_vec is not None and focus_word is not None \
                and doc_idx < len(embs) and embs[doc_idx] is not None:
            focus_sim = max(0.0, _cosine(embs[doc_idx], focus_vec))
            focus_lift = FOCUS_SEMANTIC_WEIGHT * focus_sim
            fvals = strengths.get(focus_word, [])
            fs, focus_matched_word, mismatch = fvals[doc_idx] if doc_idx < len(fvals) else (0.0, "", False)
            # A chunk "has signal" if it literally/fuzzily matches the focus
            # word AT ALL -- affirmed or negated. That's what guarantees it
            # gets tiered above no-signal filler; polarity only affects HOW
            # much it's lifted, via the existing mismatch penalty below.
            has_focus_signal = fs > 0
            if has_focus_signal and focus_partner:
                # focus_word is the rare half of a two-word phrase ask (e.g.
                # "physical" in "physical office") -- require its partner to
                # ALSO match NEAR it in this chunk, or this is likely two
                # unrelated occurrences sharing a chunk by coincidence (e.g.
                # "a physical certificate" ... "our office address"). Solo
                # occurrences still nudge ranking a little (dampened lift),
                # but do NOT count as EVIDENCE (has_focus_signal stays False,
                # so the per-business summary won't claim confirmed_present/
                # absent off a coincidental, unrelated word match).
                pvals = strengths.get(focus_partner, [])
                ps, partner_matched_word, _ = pvals[doc_idx] if doc_idx < len(pvals) else (0.0, "", False)
                if ps <= 0 or not _phrase_proximity_match(
                    text_lower, focus_matched_word, partner_matched_word
                ):
                    has_focus_signal = False
                    focus_lift *= PHRASE_SOLO_WEIGHT
            if has_focus_signal:
                if mismatch:
                    focus_lift *= NEGATION_MISMATCH_PENALTY
                # `mismatch` is relative to the QUERY's own polarity (e.g. for
                # a "no app" query, mismatch=True means the chunk affirms
                # having an app). To label evidence in absolute terms --
                # "confirmed_present" (chunk affirms the focus word) vs.
                # "confirmed_absent" (chunk negates it) -- regardless of how
                # the query itself was phrased, recover the chunk's own
                # polarity: mismatch is defined as
                # `chunk_negated != query_negation[focus_word]`, so
                # chunk_negated = mismatch XOR query_negation[focus_word].
                query_word_negated = query_negation.get(focus_word, False)
                chunk_negated = mismatch != query_word_negated
                focus_evidence = "confirmed_absent" if chunk_negated else "confirmed_present"

        # The phrase partner's OWN standalone evidence (e.g. "office" without
        # "physical" nearby), independent of proximity to focus_word. A single
        # occurrence isn't trusted alone (that's the original false-positive
        # risk) -- but the caller (ingest_and_answer.py's per-business
        # summary) can require this to repeat across multiple DISTINCT chunks
        # before treating it as real evidence, since a business consistently
        # using a word the same way across several of its own pages is a much
        # stronger signal than one coincidental mention.
        #
        # Requires a LITERAL match only (matched_word == focus_partner), not
        # fuzzy -- fuzzy word-similarity is inherently less certain, and a
        # fuzzy false positive (e.g. "system"~"service" = 0.62, just over the
        # fuzzy threshold) can repeat across many pages NATURALLY, simply
        # because the confused word ("service") is itself common generic
        # business copy. Repetition alone doesn't distinguish a genuine
        # pattern from a systematic embedding error -- only a literal match
        # is unambiguous enough to trust as standalone, corroborated evidence.
        partner_has_signal = False
        partner_evidence = "no_evidence"
        if focus_partner:
            pvals = strengths.get(focus_partner, [])
            ps, pmw, p_mismatch = pvals[doc_idx] if doc_idx < len(pvals) else (0.0, "", False)
            partner_has_signal = ps > 0 and pmw == focus_partner
            if partner_has_signal:
                p_negated = p_mismatch != query_negation.get(focus_partner, False)
                partner_evidence = "confirmed_absent" if p_negated else "confirmed_present"

        results.append({
            "score": semantic_score + bonus + focus_lift,
            "semantic_score": semantic_score,
            "keyword_bonus": bonus,
            "focus_lift": focus_lift,
            "focus_word": focus_word or "",
            "has_focus_signal": has_focus_signal,
            "focus_evidence": focus_evidence,
            "focus_partner": focus_partner or "",
            "partner_has_signal": partner_has_signal,
            "partner_evidence": partner_evidence,
            "matched_keywords": matched,
            "matched_keyword_weights": {label: round(weight, 3)
                                        for label, weight in weights.items()},
            "text": text,
            "url": meta.get("url", ""),
            "title": meta.get("title", ""),
            "domain": meta.get("domain", ""),
            "chunk_no": meta.get("chunk_no", ""),
        })
    # TIERED sort: "has real signal about the focus concept" is the PRIMARY
    # key (True sorts before False), the existing hybrid score is the
    # secondary key WITHIN each tier. This is what guarantees any chunk
    # actually addressing the query's real question -- positive or negative --
    # outranks chunks that don't address it at all, regardless of how high
    # their raw semantic score happens to be in a dense, single-topic corpus.
    results.sort(key=lambda r: (r["has_focus_signal"], r["score"]), reverse=True)
    return results[:k]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Rank every stored chunk against a query; show top matches "
                    "with score + full text. No LLM call."
    )
    parser.add_argument("--query", default=None,
                        help="Override query (default: reused from last_run.json).")
    parser.add_argument("--last-run", default="last_run.json",
                        help="Path to last_run.json (used when --query is omitted).")
    parser.add_argument("--k", type=int, default=10,
                        help="How many top matches to show (default 10).")
    parser.add_argument("--business", default=None,
                        help="Restrict to one business's domain.")
    parser.add_argument("--keyword-boost", type=float, default=DEFAULT_KEYWORD_BOOST,
                        help=f"Score bonus per matched keyword (default "
                             f"{DEFAULT_KEYWORD_BOOST}; use 0 for pure semantic ranking).")
    args = parser.parse_args()

    query = args.query or _query_from_last_run(args.last_run)
    matches = top_matches(query, k=args.k, business=args.business,
                         keyword_boost=args.keyword_boost)

    print("\n" + "=" * 70)
    print("Query:", query)
    if args.business:
        print("Business filter:", args.business)
    print(f"Top {len(matches)} chunks — hybrid score (semantic + keyword bonus, "
          f"boost={args.keyword_boost})")
    print("=" * 70)

    if not matches:
        print("No chunks stored yet — run `python -m rag.ingest_and_answer` "
              "(or `python -m rag.ingest_from_storage`) first.")
        return

    for rank, m in enumerate(matches, 1):
        print(f"\n[{rank}] score={m['score']:.4f}  "
              f"(semantic={m['semantic_score']:.4f} + keyword={m['keyword_bonus']:.4f} "
              f"+ focus[{m.get('focus_word','')}]={m.get('focus_lift', 0.0):.4f})  "
              f"evidence={m.get('focus_evidence', 'no_evidence')}  "
              f"domain={m['domain']}  chunk={m['chunk_no']}")
        if m["matched_keywords"]:
            weights = m["matched_keyword_weights"]
            pretty = ", ".join(f"{w}({weights.get(w, 1.0):.2f})" for w in m["matched_keywords"])
            print("  matched keywords (word[→fuzzy match], effective weight):", pretty)
        print("URL:", m["url"])
        print("-" * 70)
        print(m["text"])
        print("-" * 70)

    print("=" * 70)


if __name__ == "__main__":
    main()
