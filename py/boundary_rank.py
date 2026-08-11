"""Bounded, Unicode-aware boundary quality for keyword ranking."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import functools
import math
import re
import unicodedata


_QUERY_SPLIT = re.compile(r"[\s\-_]+")
_APOSTROPHES = frozenset(("'", "\u2019", "\u02bc"))
_UNSEGMENTED = frozenset(
    ("HAN", "HIRAGANA", "KATAKANA", "HANGUL", "THAI", "LAO", "KHMER", "MYANMAR")
)
_SCRIPTS = (
    "LATIN", "GREEK", "CYRILLIC", "ARMENIAN", "HEBREW", "ARABIC", "SYRIAC",
    "THAANA", "DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA",
    "TAMIL", "TELUGU", "KANNADA", "MALAYALAM", "SINHALA", "THAI", "LAO",
    "TIBETAN", "MYANMAR", "GEORGIAN", "HANGUL", "ETHIOPIC", "CHEROKEE",
    "KHMER", "MONGOLIAN", "HIRAGANA", "KATAKANA", "CJK", "IDEOGRAPH",
)

StatValue = tuple[int, int] | Mapping[str, int]
Stats = Mapping[str, StatValue] | Callable[[str], StatValue | None] | None
Span = tuple[int, int]


@dataclass(frozen=True, slots=True)
class _Cluster:
    start: int
    end: int
    text: str

    @property
    def base(self) -> str:
        for char in self.text:
            if not _is_attachment(char) and char != "\u200d":
                return char
        return self.text[0]


@dataclass(frozen=True, slots=True)
class PreparedText:
    original: str
    folded: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]
    boundaries: frozenset[int]

    def find_spans(self, folded_term: str) -> tuple[Span, ...]:
        if not folded_term:
            return ()
        out: list[Span] = []
        seen: set[Span] = set()
        at = self.folded.find(folded_term)
        while at >= 0:
            span = (self.starts[at], self.ends[at + len(folded_term) - 1])
            if span not in seen:
                seen.add(span)
                out.append(span)
            at = self.folded.find(folded_term, at + 1)
        return tuple(out)

    def quality(self, span: Span) -> float:
        start, end = span
        if start < 0 or end < start or end > len(self.original):
            raise ValueError("boundary span outside text")
        return (float(start in self.boundaries) + float(end in self.boundaries)) / 2.0

    def folded_slice(self, span: Span) -> str:
        return normalize_token(self.original[span[0]:span[1]])


@dataclass(frozen=True, slots=True)
class _AsciiText:
    """ASCII stand-in for PreparedText: same span protocol, no cluster or boundary-set cost."""

    original: str
    folded: str

    def find_spans(self, folded_term: str) -> tuple[Span, ...]:
        if not folded_term:
            return ()
        out: list[Span] = []
        at = self.folded.find(folded_term)
        while at >= 0:
            out.append((at, at + len(folded_term)))
            at = self.folded.find(folded_term, at + 1)
        return tuple(out)

    def quality(self, span: Span) -> float:
        return _ascii_quality(self.original, span)

    def folded_slice(self, span: Span) -> str:
        return self.folded[span[0]:span[1]]


@dataclass(frozen=True, slots=True)
class BoundaryTerm:
    folded: str
    ambiguity: float


@dataclass(frozen=True, slots=True)
class BoundaryScore:
    factor: float
    qualities: tuple[float, ...]
    match_class: str
    matched: bool
    spans: tuple[Span | None, ...]


@dataclass(frozen=True, slots=True)
class PreparedQuery:
    terms: tuple[BoundaryTerm, ...]

    def evaluate(
        self,
        text: str,
        spans: Sequence[Span | None] | None = None,
        *,
        validate_spans: bool = True,
    ) -> BoundaryScore:
        if text.isascii() and all(term.folded.isascii() for term in self.terms):
            prepared: _AsciiText | PreparedText = _AsciiText(text, text.lower())
        else:
            prepared = prepare_text(text)
        if spans is not None and len(spans) != len(self.terms):
            raise ValueError("one span is required per query token")
        qualities: list[float] = []
        selected: list[Span | None] = []
        matched = True
        for index, term in enumerate(self.terms):
            if spans is None:
                candidates = prepared.find_spans(term.folded)
                span = max(candidates, key=prepared.quality) if candidates else None
            else:
                span = spans[index]
            if span is None:
                quality = 0.0
                matched = False
            else:
                start, end = span
                if start < 0 or end < start or end > len(text):
                    raise ValueError("boundary span outside text")
                if (spans is not None and validate_spans
                        and term.folded not in prepared.folded_slice(span)):
                    raise ValueError("span does not identify its query token")
                quality = prepared.quality(span)
            selected.append(span)
            qualities.append(quality)
        return _finish_score(self.terms, qualities, selected, matched)


def query_tokens(query: str) -> tuple[str, ...]:
    return tuple(token for token in _QUERY_SPLIT.split(query.strip()) if token)


def normalize_token(token: str) -> str:
    return "".join(unicodedata.normalize("NFKC", cluster.text).casefold()
                   for cluster in _clusters(token))


def cold_prior(token: str) -> float:
    clusters = _clusters(token)
    alphanumeric = [cluster for cluster in clusters
                    if any(char.isalnum() for char in cluster.text)]
    length = len(alphanumeric) or len(clusters)
    scripts = {_script(cluster.base) for cluster in alphanumeric if cluster.base.isalpha()}
    scripts.discard(None)
    if scripts and scripts <= _UNSEGMENTED:
        return 0.0
    cased = any(char.lower() != char.upper() for char in token)
    if not alphanumeric:
        return 0.0
    if cased:
        if length <= 2:
            return 0.90
        if length == 3:
            return 0.75
        if length == 4:
            return 0.40
        return 0.05
    if length <= 2:
        return 0.60
    if length <= 4:
        return 0.30
    return 0.05


def prepare_query(query: str, stats: Stats = None) -> PreparedQuery:
    resolved: dict[str, float] = {}
    terms: list[BoundaryTerm] = []
    for original in query_tokens(query):
        folded = normalize_token(original)
        if not folded:
            continue
        prior = cold_prior(original)
        if folded not in resolved:
            resolved[folded] = _ambiguity(folded, prior, stats)
        terms.append(BoundaryTerm(folded, resolved[folded]))
    return PreparedQuery(tuple(terms))


# ranking re-evaluates the same snippet once per occurrence; cache so preparation is once per text
@functools.lru_cache(maxsize=64)
def prepare_text(text: str) -> PreparedText:
    clusters = _clusters(text)
    folded: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for cluster in clusters:
        normalized = unicodedata.normalize("NFKC", cluster.text).casefold()
        folded.append(normalized)
        starts.extend((cluster.start,) * len(normalized))
        ends.extend((cluster.end,) * len(normalized))
    boundaries = {0, len(text)}
    for index in range(1, len(clusters)):
        if _boundary_between(clusters, index):
            boundaries.add(clusters[index].start)
    return PreparedText(text, "".join(folded), tuple(starts), tuple(ends),
                        frozenset(boundaries))


def _finish_score(terms: Sequence[BoundaryTerm], qualities: Sequence[float],
                  selected: Sequence[Span | None], matched: bool) -> BoundaryScore:
    penalties = [max(0.12, 1.0 - term.ambiguity * (1.0 - quality))
                 for term, quality in zip(terms, qualities)]
    factor = math.prod(penalties) ** (1.0 / len(penalties)) if penalties else 1.0
    factor = min(1.0, max(0.0, factor))
    if qualities and all(quality == 1.0 for quality in qualities):
        match_class = "aligned"
    elif qualities and all(quality == 0.0 for quality in qualities):
        match_class = "interior"
    else:
        match_class = "partial"
    return BoundaryScore(factor, tuple(qualities), match_class, matched, tuple(selected))


def _ascii_quality(text: str, span: Span) -> float:
    return (float(_ascii_boundary(text, span[0]))
            + float(_ascii_boundary(text, span[1]))) / 2.0


def _ascii_boundary(text: str, index: int) -> bool:
    if index == 0 or index == len(text):
        return True
    left, right = text[index - 1], text[index]
    if right == "'" and index + 1 < len(text):
        if left.isalpha() and text[index + 1].isalpha():
            return False
    if left == "'" and index >= 2:
        if text[index - 2].isalpha() and right.isalpha():
            return False
    if not left.isalnum() or not right.isalnum():
        return True
    if left.islower() and right.isupper():
        return True
    if left.isupper() and right.isupper() and index + 1 < len(text):
        if text[index + 1].islower():
            return True
    return ((left.isalpha() and right.isdigit())
            or (left.isdigit() and right.isalpha()))


def _ambiguity(token: str, prior: float, stats: Stats) -> float:
    value = stats(token) if callable(stats) else stats.get(token) if stats is not None else None
    if value is None:
        return prior
    if isinstance(value, Mapping):
        n, s = int(value.get("n", 0)), int(value.get("s", 0))
    else:
        n, s = int(value[0]), int(value[1])
    if n <= 0:
        return prior
    s = min(n, max(0, s))
    return min(1.0, max(0.0, (n - s + 32.0 * prior) / (n + 32.0)))


def _clusters(text: str) -> tuple[_Cluster, ...]:
    out: list[_Cluster] = []
    index = 0
    while index < len(text):
        start = index
        hangul = _hangul_kind(text[index])
        index += 1
        while index < len(text):
            char = text[index]
            if _is_attachment(char):
                index += 1
                continue
            next_hangul = _hangul_kind(char)
            if hangul == "L" and next_hangul == "V":
                hangul = "LV"
                index += 1
                continue
            if hangul == "LV" and next_hangul == "T":
                hangul = "LVT"
                index += 1
                continue
            if char == "\u200d" and index + 1 < len(text):
                index += 2
                continue
            break
        out.append(_Cluster(start, index, text[start:index]))
    return tuple(out)


def _is_attachment(char: str) -> bool:
    code = ord(char)
    return (unicodedata.category(char).startswith("M")
            or 0xFE00 <= code <= 0xFE0F
            or 0xE0100 <= code <= 0xE01EF
            or 0x1F3FB <= code <= 0x1F3FF)


def _hangul_kind(char: str) -> str | None:
    code = ord(char)
    if 0x1100 <= code <= 0x115F or 0xA960 <= code <= 0xA97C:
        return "L"
    if 0x1160 <= code <= 0x11A7 or 0xD7B0 <= code <= 0xD7C6:
        return "V"
    if 0x11A8 <= code <= 0x11FF or 0xD7CB <= code <= 0xD7FB:
        return "T"
    return None


def _boundary_between(clusters: tuple[_Cluster, ...], index: int) -> bool:
    left, right = clusters[index - 1], clusters[index]
    if _apostrophe_joins(clusters, index):
        return False
    if _separator(left.base) or _separator(right.base):
        return True
    before, after = left.base, right.base
    if before.islower() and after.isupper():
        return True
    if before.isupper() and after.isupper() and index + 1 < len(clusters):
        if clusters[index + 1].base.islower():
            return True
    if (before.isalpha() and after.isdigit()) or (before.isdigit() and after.isalpha()):
        return True
    before_script, after_script = _script(before), _script(after)
    return bool(before.isalpha() and after.isalpha() and before_script and after_script
                and before_script != after_script)


def _apostrophe_joins(clusters: tuple[_Cluster, ...], index: int) -> bool:
    left, right = clusters[index - 1], clusters[index]
    if right.text in _APOSTROPHES and index + 1 < len(clusters):
        return left.base.isalpha() and clusters[index + 1].base.isalpha()
    if left.text in _APOSTROPHES and index >= 2:
        return clusters[index - 2].base.isalpha() and right.base.isalpha()
    return False


def _separator(char: str) -> bool:
    category = unicodedata.category(char)
    return char.isspace() or category[0] in ("P", "S")


def _script(char: str) -> str | None:
    if not char.isalpha():
        return None
    name = unicodedata.name(char, "")
    for script in _SCRIPTS:
        if script in name:
            return "HAN" if script in ("CJK", "IDEOGRAPH") else script
    return None
