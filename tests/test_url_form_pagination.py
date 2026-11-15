"""Integration tests for paginated ``GET`` forms replayed through w3lib.

Each test builds the request a crawler would send from a local fixture form,
then runs it through the transformations a crawl uses:

1. :func:`~w3lib.url.url_query_cleaner` to drop parameters no longer wanted,
2. :func:`~w3lib.url.add_or_replace_parameter` to move to another page,
3. :func:`~w3lib.url.canonicalize_url` (or the local ``_dedup_key``) to
   deduplicate.

The fixtures under ``tests/fixtures/forms/`` model search pages whose form
submits repeated parameters (``tag=a&tag=b``, ``size=s&size=m``) and empty
values (``q=``, ``page=``, meaning "no filter" and "first page"), and that use
``;`` as the query separator on one page so that percent-encoded separators in
values interact with the parser.

The form reader below is intentionally local and small: it reads only what the
fixtures use, from a form submitted without activating a submit button.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

import pytest

from w3lib.url import (
    add_or_replace_parameter,
    add_or_replace_parameters,
    canonicalize_url,
    safe_url_string,
    url_query_cleaner,
    url_query_parameter,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "forms"

_SUBMIT_TYPES = frozenset({"submit", "button", "image", "reset", "file"})


# --------------------------------------------------------------------------
# Local form fixtures
# --------------------------------------------------------------------------


@dataclass
class _Select:
    name: str
    multiple: bool
    index: int
    options: list[tuple[str, bool]] = field(default_factory=list)


class _GetFormReader(HTMLParser):
    """Read a single ``GET`` form, with controls in document order."""

    def __init__(self, form_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.action = ""
        self.separator = "&"
        self.controls: list[tuple[str, str]] = []
        self._in_form = False
        self._select: _Select | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        disabled = "disabled" in attributes
        if tag == "form":
            if not self._in_form and attributes.get("id") == self.form_id:
                self._in_form = True
                self.action = attributes.get("action") or ""
                self.separator = attributes.get("data-query-separator") or "&"
            return
        if not self._in_form:
            return
        name = attributes.get("name")
        if tag == "input":
            if not name or disabled:
                return
            kind = (attributes.get("type") or "text").lower()
            if kind in _SUBMIT_TYPES:
                # A submit button is only submitted when it is the submitter.
                return
            if kind in {"checkbox", "radio"} and "checked" not in attributes:
                return
            value = attributes.get("value")
            if value is None:
                value = "on" if kind in {"checkbox", "radio"} else ""
            self.controls.append((name, value))
        elif tag == "select":
            if not name or disabled:
                self._select = None
                return
            self._select = _Select(name, "multiple" in attributes, len(self.controls))
            # Keep a slot so selected options land where the select is.
            self.controls.append((name, ""))
        elif tag == "option" and self._select is not None:
            # Fixture options always carry an explicit value.
            self._select.options.append(
                (attributes.get("value", ""), "selected" in attributes)
            )

    def handle_endtag(self, tag):
        if tag == "select" and self._select is not None:
            select = self._select
            chosen = [value for value, selected in select.options if selected]
            if select.multiple:
                self.controls[select.index : select.index + 1] = [
                    (select.name, value) for value in chosen
                ]
            else:
                self.controls[select.index] = (
                    select.name,
                    chosen[0] if chosen else "",
                )
            self._select = None
        elif tag == "form":
            self._in_form = False


@dataclass(frozen=True)
class _Form:
    action: str
    separator: str
    fields: tuple[tuple[str, str], ...]

    def submit(self, page: str | None = None) -> str:
        """Return the URL a browser would request for the form.

        ``page`` replaces the value of the ``page`` control, which selects
        another page of results.
        """
        pairs = [
            (name, page if name == "page" and page is not None else value)
            for name, value in self.fields
        ]
        query = self.separator.join(
            f"{quote_plus(name, safe='')}={quote_plus(value, safe='')}"
            for name, value in pairs
        )
        base, _, fragment = self.action.partition("#")
        url = f"{base}?{query}"
        return f"{url}#{fragment}" if fragment else url


@cache
def load_form(fixture: str, form_id: str) -> _Form:
    reader = _GetFormReader(form_id)
    reader.feed((FIXTURE_DIR / fixture).read_text(encoding="utf-8"))
    return _Form(reader.action, reader.separator, tuple(reader.controls))


# --------------------------------------------------------------------------
# Crawl helpers
# --------------------------------------------------------------------------


def _pairs(url: str, separator: str = "&") -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True, separator=separator)


def _page(url: str, scenario: _Scenario) -> str | None:
    return url_query_parameter(
        url, "page", keep_blank_values=True, separator=scenario.separator
    )


def _clean(
    url: str,
    scenario: _Scenario,
    names: tuple[str, ...] | None = None,
    *,
    remove: bool = False,
    unique: bool = False,
) -> str:
    return url_query_cleaner(
        url,
        scenario.allowed if names is None else names,
        sep=scenario.separator,
        remove=remove,
        unique=unique,
        keep_fragments=True,
    )


def _paged(url: str, scenario: _Scenario, page: str) -> str:
    return add_or_replace_parameter(url, "page", page, separator=scenario.separator)


def _canonical(url: str, scenario: _Scenario, *, keep_fragments: bool = False) -> str:
    return canonicalize_url(
        url, keep_fragments=keep_fragments, query_separator=scenario.separator
    )


def _dedup_key(
    url: str, separator: str = "&"
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """A crawler dedup address that does not sort query pairs.

    ``canonicalize_url`` sorts query pairs, which is lossy for servers that
    treat ``tag=a&tag=b`` and ``tag=b&tag=a`` as different pages. This key
    canonicalizes the path but keeps the pairs in request order.
    """
    parts = urlsplit(safe_url_string(url))
    base = canonicalize_url(
        urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    )
    return base, tuple(
        parse_qsl(parts.query, keep_blank_values=True, separator=separator)
    )


# --------------------------------------------------------------------------
# Scenarios with independent expected results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scenario:  # pylint: disable=too-many-instance-attributes
    fixture: str
    form_id: str
    separator: str
    fragment: str
    fields: tuple[tuple[str, str], ...]
    allowed: tuple[str, ...]
    allowed_without_page: tuple[str, ...]
    first_page: str
    second_page: str
    third_page: str
    cleaned_first: str
    cleaned_first_without_fragment: str
    cleaned_second: str
    whitelisted_without_page: str
    blacklisted_page: str
    canonical_first: str
    canonical_second: str
    canonical_second_fragment: str
    canonical_third: str
    canonical_without_page: str
    canonical_raw_second: str
    canonical_raw_second_fragment: str


CATALOG = _Scenario(
    fixture="catalog-get.html",
    form_id="catalog-search",
    separator="&",
    fragment="results",
    fields=(
        ("view", "grid"),
        ("sort", "price asc"),
        ("session", "abc123"),
        ("tag", "a"),
        ("tag", "b"),
        ("size", "s"),
        ("size", "m"),
        ("q", ""),
        ("page", ""),
    ),
    allowed=("view", "sort", "tag", "size", "q", "page"),
    allowed_without_page=("view", "sort", "tag", "size", "q"),
    first_page="https://shop.test/catalog?view=grid&sort=price+asc&session=abc123&tag=a&tag=b&size=s&size=m&q=&page=#results",
    second_page="https://shop.test/catalog?view=grid&sort=price+asc&session=abc123&tag=a&tag=b&size=s&size=m&q=&page=2#results",
    third_page="https://shop.test/catalog?view=grid&sort=price+asc&session=abc123&tag=a&tag=b&size=s&size=m&q=&page=3#results",
    cleaned_first="https://shop.test/catalog?view=grid&sort=price+asc&tag=a&tag=b&size=s&size=m&q=&page=#results",
    cleaned_first_without_fragment="https://shop.test/catalog?view=grid&sort=price+asc&tag=a&tag=b&size=s&size=m&q=&page=",
    cleaned_second="https://shop.test/catalog?view=grid&sort=price+asc&tag=a&tag=b&size=s&size=m&q=&page=2#results",
    whitelisted_without_page="https://shop.test/catalog?view=grid&sort=price+asc&tag=a&tag=b&size=s&size=m&q=#results",
    blacklisted_page="https://shop.test/catalog?view=grid&sort=price+asc&tag=a&tag=b&size=s&size=m&q=#results",
    canonical_first="https://shop.test/catalog?page=&q=&size=m&size=s&sort=price+asc&tag=a&tag=b&view=grid",
    canonical_second="https://shop.test/catalog?page=2&q=&size=m&size=s&sort=price+asc&tag=a&tag=b&view=grid",
    canonical_second_fragment="https://shop.test/catalog?page=2&q=&size=m&size=s&sort=price+asc&tag=a&tag=b&view=grid#results",
    canonical_third="https://shop.test/catalog?page=3&q=&size=m&size=s&sort=price+asc&tag=a&tag=b&view=grid",
    canonical_without_page="https://shop.test/catalog?q=&size=m&size=s&sort=price+asc&tag=a&tag=b&view=grid",
    canonical_raw_second="https://shop.test/catalog?page=2&q=&session=abc123&size=m&size=s&sort=price+asc&tag=a&tag=b&view=grid",
    canonical_raw_second_fragment="https://shop.test/catalog?page=2&q=&session=abc123&size=m&size=s&sort=price+asc&tag=a&tag=b&view=grid#results",
)

LEGACY = _Scenario(
    fixture="legacy-catalog-get.html",
    form_id="legacy-search",
    separator=";",
    fragment="top",
    fields=(
        ("view", "grid"),
        ("q", "a;b"),
        ("session", "abc123"),
        ("tag", "a"),
        ("tag", "b"),
        ("sort", "price asc"),
        ("page", ""),
    ),
    allowed=("view", "q", "tag", "sort", "page"),
    allowed_without_page=("view", "q", "tag", "sort"),
    first_page="https://shop.test/legacy?view=grid;q=a%3Bb;session=abc123;tag=a;tag=b;sort=price+asc;page=#top",
    second_page="https://shop.test/legacy?view=grid;q=a%3Bb;session=abc123;tag=a;tag=b;sort=price+asc;page=2#top",
    third_page="https://shop.test/legacy?view=grid;q=a%3Bb;session=abc123;tag=a;tag=b;sort=price+asc;page=3#top",
    cleaned_first="https://shop.test/legacy?view=grid;q=a%3Bb;tag=a;tag=b;sort=price+asc;page=#top",
    cleaned_first_without_fragment="https://shop.test/legacy?view=grid;q=a%3Bb;tag=a;tag=b;sort=price+asc;page=",
    cleaned_second="https://shop.test/legacy?view=grid;q=a%3Bb;tag=a;tag=b;sort=price+asc;page=2#top",
    whitelisted_without_page="https://shop.test/legacy?view=grid;q=a%3Bb;tag=a;tag=b;sort=price+asc#top",
    blacklisted_page="https://shop.test/legacy?view=grid;q=a%3Bb;tag=a;tag=b;sort=price+asc#top",
    canonical_first="https://shop.test/legacy?page=;q=a%3Bb;sort=price+asc;tag=a;tag=b;view=grid",
    canonical_second="https://shop.test/legacy?page=2;q=a%3Bb;sort=price+asc;tag=a;tag=b;view=grid",
    canonical_second_fragment="https://shop.test/legacy?page=2;q=a%3Bb;sort=price+asc;tag=a;tag=b;view=grid#top",
    canonical_third="https://shop.test/legacy?page=3;q=a%3Bb;sort=price+asc;tag=a;tag=b;view=grid",
    canonical_without_page="https://shop.test/legacy?q=a%3Bb;sort=price+asc;tag=a;tag=b;view=grid",
    canonical_raw_second="https://shop.test/legacy?page=2;q=a%3Bb;session=abc123;sort=price+asc;tag=a;tag=b;view=grid",
    canonical_raw_second_fragment="https://shop.test/legacy?page=2;q=a%3Bb;session=abc123;sort=price+asc;tag=a;tag=b;view=grid#top",
)

SCENARIOS = [
    pytest.param(CATALOG, id="ampersand-separator"),
    pytest.param(LEGACY, id="semicolon-separator"),
]


# --------------------------------------------------------------------------
# Submitted request semantics
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_submitted_request_matches_form_controls(scenario):
    form = load_form(scenario.fixture, scenario.form_id)
    first = form.submit()

    assert first == scenario.first_page
    assert _pairs(first, scenario.separator) == list(scenario.fields)

    # Every submitted name is readable, and reading a name returns its leftmost
    # value: repeated parameters keep "a" (not "b"), and empty values are
    # visible only with keep_blank_values.
    leftmost: dict[str, str] = {}
    for name, value in scenario.fields:
        leftmost.setdefault(name, value)
    for name, value in leftmost.items():
        read = url_query_parameter(
            first, name, keep_blank_values=True, separator=scenario.separator
        )
        assert read == value
        default_read = url_query_parameter(first, name, separator=scenario.separator)
        assert default_read == (value or None)

    # "page" is submitted empty on the first page, and a repeated filter keeps
    # both values in form order.
    assert leftmost["page"] == ""
    assert _page(first, scenario) == ""
    assert [
        value for name, value in _pairs(first, scenario.separator) if name == "tag"
    ] == ["a", "b"]

    # The form action fragment is preserved by the request, but a crawler
    # that drops fragments still sees the same page.
    assert urlsplit(first).fragment == scenario.fragment
    assert safe_url_string(first) == first

    # Disabled controls and submit buttons are not submitted.
    assert "go" not in [name for name, _ in form.fields]


def test_checked_and_disabled_controls_are_filtered():
    form = load_form(CATALOG.fixture, CATALOG.form_id)

    assert ("tag", "c") not in form.fields  # disabled, though checked
    assert ("size", "l") not in form.fields  # unchecked
    assert "go" not in [name for name, _ in form.fields]  # submit button


# --------------------------------------------------------------------------
# Pagination: clean, merge, and deduplicate in sequence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_pagination_merge_keeps_pointing_at_the_submitted_page(scenario):
    form = load_form(scenario.fixture, scenario.form_id)
    first = form.submit()
    second = form.submit(page="2")

    # Merging the next page onto the raw submission gives exactly the request
    # the form produces for that page.
    assert _paged(first, scenario, "2") == second

    cleaned_first = _clean(first, scenario)
    cleaned_second = _clean(second, scenario)
    assert cleaned_first == scenario.cleaned_first
    assert cleaned_second == scenario.cleaned_second

    # Cleaning drops the session parameter but keeps repeated and empty ones,
    # and merging still lands on the submitted page.
    assert _paged(cleaned_first, scenario, "2") == scenario.cleaned_second
    assert _page(cleaned_first, scenario) == ""
    assert _page(cleaned_second, scenario) == "2"

    assert _canonical(cleaned_first, scenario) == scenario.canonical_first
    assert _canonical(cleaned_second, scenario) == scenario.canonical_second
    assert _canonical(_paged(cleaned_first, scenario, "3"), scenario) == (
        scenario.canonical_third
    )

    # Consecutive pages never share a dedup address.
    assert (
        len(
            {
                _canonical(cleaned_first, scenario),
                _canonical(cleaned_second, scenario),
                _canonical(_paged(cleaned_first, scenario, "3"), scenario),
            }
        )
        == 3
    )


def test_merge_replaces_every_occurrence_of_a_repeated_parameter():
    form = load_form(CATALOG.fixture, CATALOG.form_id)
    first = form.submit()

    merged = add_or_replace_parameter(first, "size", "l")
    assert (
        merged
        == "https://shop.test/catalog?view=grid&sort=price+asc&session=abc123&tag=a&tag=b&size=l&q=&page=#results"
    )
    assert [pair for pair in _pairs(merged) if pair[0] == "size"] == [("size", "l")]

    combined = add_or_replace_parameters(first, {"page": "2", "sort": "price asc"})
    # Values are re-encoded on merge ("price asc" -> "price+asc"), and the
    # form's field order is kept.
    assert combined == CATALOG.second_page
    assert [name for name, _ in _pairs(combined)] == [
        name for name, _ in CATALOG.fields
    ]


def test_merge_on_a_semicolon_query_keeps_encoded_separators():
    form = load_form(LEGACY.fixture, LEGACY.form_id)

    merged = add_or_replace_parameter(
        form.submit(page="2"), "page", "3", separator=LEGACY.separator
    )
    assert merged == LEGACY.third_page
    # The value that contains a literal ";", submitted as %3B, is untouched.
    assert _pairs(merged, LEGACY.separator) == [
        (name, "3" if name == "page" else value) for name, value in LEGACY.fields
    ]


# --------------------------------------------------------------------------
# Same page with different encoding vs. different pages with the same pairs
# --------------------------------------------------------------------------


def test_encoding_only_variants_share_one_dedup_address():
    plus = "https://shop.test/catalog?sort=price+asc&tag=a&page=2#results"
    percent = "https://shop.test/catalog?sort=price%20asc&tag=%61&page=2#results"
    mixed = "https://shop.test/catalog?sort=price%20asc&tag=a&page=2#results"
    bare = "https://shop.test/catalog?sort=price%20asc&tag=a&page=2"

    expected = "https://shop.test/catalog?page=2&sort=price+asc&tag=a"
    for url in (plus, percent, mixed, bare):
        assert canonicalize_url(url) == expected

    # Fragments are dropped by default, and kept only when asked for.
    for url in (plus, percent, mixed):
        assert canonicalize_url(url, keep_fragments=True) == f"{expected}#results"
    assert canonicalize_url(bare, keep_fragments=True) == expected

    # The requests differ byte-for-byte, but all four reach the same page.
    assert len({safe_url_string(url) for url in (plus, percent, mixed, bare)}) == 4
    assert _pairs(plus) == _pairs(percent) == _pairs(mixed) == _pairs(bare)
    assert len({_dedup_key(url) for url in (plus, percent, mixed, bare)}) == 1

    # A different page stays distinct under both forms of the address.
    first_page = "https://shop.test/catalog?sort=price+asc&tag=a&page=1"
    assert canonicalize_url(first_page) != expected
    assert _dedup_key(first_page) != _dedup_key(plus)


def test_duplicate_parameter_order_is_a_canonicalization_boundary():
    ab = "https://shop.test/catalog?view=grid&sort=price+asc&session=abc123&tag=a&tag=b&size=s&size=m&q=&page=2#results"
    ba = "https://shop.test/catalog?view=grid&sort=price+asc&session=abc123&tag=b&tag=a&size=s&size=m&q=&page=2#results"

    # Existing default behaviour: query pairs are sorted, so two pages that
    # differ only in the order of a repeated parameter collapse into one
    # dedup address. This is the documented boundary of canonicalize_url.
    assert canonicalize_url(ab) == canonicalize_url(ba)
    assert (
        canonicalize_url(ab)
        == "https://shop.test/catalog?page=2&q=&session=abc123&size=m&size=s&sort=price+asc&tag=a&tag=b&view=grid"
    )
    assert safe_url_string(ab) != safe_url_string(ba)

    # Cleaning with unique=True does not preserve the pair either: it keeps
    # only the first value, so the two pages stay different, but the second
    # value is lost from the request too.
    cleaned_ab = _clean(ab, CATALOG, unique=True)
    cleaned_ba = _clean(ba, CATALOG, unique=True)
    assert (
        cleaned_ab
        == "https://shop.test/catalog?view=grid&sort=price+asc&tag=a&size=s&q=&page=2#results"
    )
    assert (
        cleaned_ba
        == "https://shop.test/catalog?view=grid&sort=price+asc&tag=b&size=s&q=&page=2#results"
    )
    assert canonicalize_url(cleaned_ab) != canonicalize_url(cleaned_ba)

    # Preservation option 1: dedup on the ordered query pairs.
    assert _dedup_key(ab) != _dedup_key(ba)
    submitted_pairs = tuple(
        (name, "2" if name == "page" else value) for name, value in CATALOG.fields
    )
    assert _dedup_key(ab) == ("https://shop.test/catalog", submitted_pairs)

    # Preservation option 2: record the order in an extra parameter, which
    # survives canonicalization because the values differ.
    ordered_ab = add_or_replace_parameter(_clean(ab, CATALOG), "tag_order", "a,b")
    ordered_ba = add_or_replace_parameter(_clean(ba, CATALOG), "tag_order", "b,a")
    assert (
        canonicalize_url(ordered_ab)
        == "https://shop.test/catalog?page=2&q=&size=m&size=s&sort=price+asc&tag=a&tag=b&tag_order=a%2Cb&view=grid"
    )
    assert (
        canonicalize_url(ordered_ba)
        == "https://shop.test/catalog?page=2&q=&size=m&size=s&sort=price+asc&tag=a&tag=b&tag_order=b%2Ca&view=grid"
    )
    assert canonicalize_url(ordered_ab) != canonicalize_url(ordered_ba)


def test_duplicate_parameter_order_boundary_is_stable():
    ab = "https://shop.test/catalog?tag=a&tag=b&page=2#results"
    ba = "https://shop.test/catalog?tag=b&tag=a&page=2#results"

    # Repeating the lossy transformation stays stable: it keeps collapsing
    # the ordered pair the same way every time.
    assert canonicalize_url(canonicalize_url(ab)) == canonicalize_url(ab)
    assert canonicalize_url(canonicalize_url(ba)) == canonicalize_url(ba)
    assert canonicalize_url(ab) == canonicalize_url(ba)

    # The order-preserving key is stable as well, and still distinguishes.
    key_ab = _dedup_key(ab)
    key_ba = _dedup_key(ba)
    assert _dedup_key(_canonical(canonicalize_url(ab), CATALOG)) == _dedup_key(
        canonicalize_url(ab)
    )
    assert key_ab != key_ba


# --------------------------------------------------------------------------
# Separator and percent-encoding interaction
# --------------------------------------------------------------------------


def test_default_separator_collapses_separator_and_encoding():
    separated = "https://shop.test/legacy?a=1;b=2"
    encoded = "https://shop.test/legacy?a=1%3Bb%3D2"

    # With the default "&" separator the whole legacy query is one value, so
    # two different pages get the same dedup address.
    assert canonicalize_url(separated) == canonicalize_url(encoded)
    assert canonicalize_url(separated) == "https://shop.test/legacy?a=1%3Bb%3D2"
    assert _pairs(separated) == _pairs(encoded) == [("a", "1;b=2")]

    # Declaring the separator keeps the two pages apart.
    assert (
        canonicalize_url(separated, query_separator=";")
        == "https://shop.test/legacy?a=1;b=2"
    )
    assert (
        canonicalize_url(encoded, query_separator=";")
        == "https://shop.test/legacy?a=1%3Bb%3D2"
    )
    assert _pairs(separated, ";") == [("a", "1"), ("b", "2")]
    assert _pairs(encoded, ";") == [("a", "1;b=2")]
    assert _dedup_key(separated, separator=";") != _dedup_key(encoded, separator=";")
    assert _dedup_key(separated) == _dedup_key(encoded)

    # Parameter semantics agree with the pairs: "b" only exists on the page
    # where ";" is a separator.
    assert url_query_parameter(separated, "a", separator=";") == "1"
    assert url_query_parameter(separated, "b", separator=";") == "2"
    assert url_query_parameter(encoded, "b", separator=";") is None
    assert url_query_parameter(separated, "b") is None
    assert url_query_parameter(encoded, "b") is None
    assert safe_url_string(separated) != safe_url_string(encoded)


def test_separator_mismatch_changes_cleaning_scope():
    first = LEGACY.first_page

    # Under the default separator the whole legacy query is the single pair
    # "view=grid;q=a;b;...". A whitelist that names "view" therefore keeps the
    # whole raw query: cleaning becomes a no-op and the blacklisted session
    # value survives (scope widened).
    assert (
        url_query_cleaner(first, ("view",), unique=False, keep_fragments=True) == first
    )
    assert (
        url_query_cleaner(
            first, ("session",), remove=True, unique=False, keep_fragments=True
        )
        == first
    )

    # A whitelist that does not name "view" drops everything (scope narrowed
    # to the bare page), and a blacklist that names "view" does the same.
    assert (
        url_query_cleaner(
            first, ("q", "tag", "page"), unique=False, keep_fragments=True
        )
        == "https://shop.test/legacy#top"
    )
    assert (
        url_query_cleaner(
            first, ("view",), remove=True, unique=False, keep_fragments=True
        )
        == "https://shop.test/legacy#top"
    )

    # With the form's separator the allowlist and blacklist apply per pair, so
    # neither scope surprise happens.
    assert _clean(first, LEGACY) == LEGACY.cleaned_first
    assert (
        url_query_cleaner(
            first, ("q", "tag", "page"), sep=";", unique=False, keep_fragments=True
        )
        == "https://shop.test/legacy?q=a%3Bb;tag=a;tag=b;page=#top"
    )
    assert _clean(first, LEGACY, ("session",), remove=True) == LEGACY.cleaned_first


def test_separator_mismatch_mangles_merged_pagination():
    first = LEGACY.first_page

    mangled = add_or_replace_parameter(first, "page", "2")
    assert (
        mangled
        == "https://shop.test/legacy?view=grid%3Bq%3Da%3Bb%3Bsession%3Dabc123%3Btag%3Da%3Btag%3Db%3Bsort%3Dprice+asc%3Bpage%3D&page=2#top"
    )
    # The page is no longer a separate parameter; the whole old query is now
    # the value of "view", including the empty page it carried.
    assert (
        url_query_parameter(mangled, "page", keep_blank_values=True, separator=";")
        is None
    )
    assert (
        url_query_parameter(mangled, "view", keep_blank_values=True, separator=";")
        == "grid;q=a;b;session=abc123;tag=a;tag=b;sort=price asc;page=&page=2"
    )

    # Using the form separator gives exactly the next page's submission.
    assert (
        add_or_replace_parameter(first, "page", "2", separator=";")
        == LEGACY.second_page
    )


# --------------------------------------------------------------------------
# Whitelists and blacklists around pagination
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_whitelist_around_pagination(scenario):
    form = load_form(scenario.fixture, scenario.form_id)
    first = form.submit()

    # When "page" is whitelisted, cleaning and merging commute: pagination is
    # kept in both orders and lands on the same page.
    append_then_clean = _clean(_paged(first, scenario, "2"), scenario)
    clean_then_append = _paged(_clean(first, scenario), scenario, "2")
    assert append_then_clean == scenario.cleaned_second
    assert clean_then_append == scenario.cleaned_second
    assert _canonical(append_then_clean, scenario) == scenario.canonical_second
    assert _canonical(clean_then_append, scenario) == scenario.canonical_second

    # When "page" is not whitelisted, appending before cleaning silently drops
    # the page: the crawl is narrowed to a pageless page and cannot advance.
    narrowed = _clean(
        _paged(first, scenario, "2"), scenario, scenario.allowed_without_page
    )
    assert narrowed == scenario.whitelisted_without_page
    assert _page(narrowed, scenario) is None
    assert _canonical(narrowed, scenario) == scenario.canonical_without_page

    # Appending after cleaning re-adds the page the whitelist would have
    # removed, so the next page is reachable, but the two orders no longer
    # agree.
    widened = _paged(
        _clean(first, scenario, scenario.allowed_without_page), scenario, "2"
    )
    assert widened == scenario.cleaned_second
    assert _page(widened, scenario) == "2"
    assert _canonical(widened, scenario) == scenario.canonical_second
    assert _canonical(narrowed, scenario) != _canonical(widened, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_blacklist_around_pagination(scenario):
    form = load_form(scenario.fixture, scenario.form_id)
    first = form.submit()
    session_and_page = ("session", "page")

    # Removing only the session value leaves pagination untouched in both
    # orders.
    for cleaned in (
        _clean(_paged(first, scenario, "2"), scenario, ("session",), remove=True),
        _paged(_clean(first, scenario, ("session",), remove=True), scenario, "2"),
    ):
        assert cleaned == scenario.cleaned_second
        assert _canonical(cleaned, scenario) == scenario.canonical_second
        assert _page(cleaned, scenario) == "2"

    # Removing "page" before cleaning deletes the pagination condition: the
    # crawl is narrowed to the pageless page.
    narrowed = _clean(
        _paged(first, scenario, "2"),
        scenario,
        session_and_page,
        remove=True,
    )
    assert narrowed == scenario.blacklisted_page
    assert _page(narrowed, scenario) is None
    assert _canonical(narrowed, scenario) == scenario.canonical_without_page

    # Cleaning first and appending afterwards re-adds the very parameter the
    # blacklist removed: the scope is widened relative to the blacklist.
    widened = _paged(
        _clean(first, scenario, session_and_page, remove=True), scenario, "2"
    )
    assert widened == scenario.cleaned_second
    assert _page(widened, scenario) == "2"
    assert _canonical(widened, scenario) == scenario.canonical_second
    assert _canonical(narrowed, scenario) != _canonical(widened, scenario)


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fragments_are_request_only_by_default(scenario):
    form = load_form(scenario.fixture, scenario.form_id)
    first = form.submit()

    # The fragment survives the request and the pagination merge.
    assert urlsplit(_paged(first, scenario, "2")).fragment == scenario.fragment
    assert _paged(first, scenario, "2") == scenario.second_page

    # Cleaning drops it unless keep_fragments=True.
    assert (
        url_query_cleaner(first, scenario.allowed, sep=scenario.separator, unique=False)
        == scenario.cleaned_first_without_fragment
    )
    assert _clean(first, scenario) == scenario.cleaned_first

    # Dedup ignores the fragment by default, and only sees it when kept.
    second = form.submit(page="2")
    other_fragment = second.replace(f"#{scenario.fragment}", "#elsewhere")
    assert _canonical(second, scenario) == scenario.canonical_raw_second
    assert _canonical(second, scenario) == _canonical(other_fragment, scenario)
    assert _canonical(second, scenario, keep_fragments=True) == (
        scenario.canonical_raw_second_fragment
    )
    assert _canonical(second, scenario, keep_fragments=True) != _canonical(
        other_fragment, scenario, keep_fragments=True
    )


# --------------------------------------------------------------------------
# Stability of repeated transformations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_repeated_transformations_are_stable(scenario):
    form = load_form(scenario.fixture, scenario.form_id)
    second = form.submit(page="2")

    cleaned = _clean(second, scenario)
    assert _clean(cleaned, scenario) == cleaned

    unique_cleaned = _clean(second, scenario, unique=True)
    assert _clean(unique_cleaned, scenario, unique=True) == unique_cleaned

    canonical = _canonical(cleaned, scenario)
    assert _canonical(canonical, scenario) == canonical

    with_fragment = _canonical(cleaned, scenario, keep_fragments=True)
    assert with_fragment == scenario.canonical_second_fragment
    assert _canonical(with_fragment, scenario, keep_fragments=True) == (with_fragment)

    merged = _paged(cleaned, scenario, "3")
    assert _paged(merged, scenario, "3") == merged
    assert _canonical(merged, scenario) == scenario.canonical_third
    # The merged request is already clean, so cleaning it again is a no-op and
    # the dedup address does not move.
    assert _clean(merged, scenario) == merged
    assert _canonical(_clean(merged, scenario), scenario) == (scenario.canonical_third)

    raw_second = form.submit(page="2")
    assert safe_url_string(safe_url_string(raw_second)) == safe_url_string(raw_second)
    assert _canonical(raw_second, scenario) == scenario.canonical_raw_second
