"""End-to-end tests for the classic crawler URL pipeline.

The scenario is a local fixture page (``tests/fixtures/search_form.html``) whose
search form is submitted with ``GET``. It is modelled on a crawler that, for the
same page request:

1. merges a pagination condition into the URL
   (:func:`w3lib.url.add_or_replace_parameters`),
2. cleans tracking parameters with a whitelist or a blacklist
   (:func:`w3lib.url.url_query_cleaner`), and
3. computes the address used for crawl de-duplication
   (:func:`w3lib.url.canonicalize_url`).

The fixture defines the following local-server contract, which the tests treat
as the source of truth for "which page a request points at":

* ``;`` is the query parameter separator.
* A ``GET`` submission replaces the action query string with the control data,
  and keeps the action fragment (``#results``).
* ``page`` may be repeated and may be blank. The first non-blank value selects
  the page; with only blank values page 1 is served.
* Repeated ``tag`` values are ANDed *in the order given*, so ``tag=x;tag=y`` and
  ``tag=y;tag=x`` are different result pages.

The tests are grouped by the question they answer, and each case asserts its own
literal expected value instead of comparing runs with each other.
"""

from __future__ import annotations

import dataclasses
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urljoin, urlsplit

import pytest

from w3lib.url import (
    add_or_replace_parameters,
    canonicalize_url,
    safe_url_string,
    url_query_cleaner,
    url_query_parameter,
)

SITE = "http://127.0.0.1:8765"
SEPARATOR = ";"
ALLOWED = ("q", "tag", "page", "sort")
FILTERS = ("q", "tag", "sort")

# What the local server serves for the fixture form once the crawler applies the
# submitted query string and the action fragment. See tests/fixtures/search_form.html.
SUBMITTED = (
    f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page=;page=1;sort=;utm_source=crawl#results"
)

FIXTURE = Path(__file__).parent / "fixtures" / "search_form.html"


class _FormParser(HTMLParser):
    """Collect the ``form`` action, its successful controls, and the next link."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action: str | None = None
        self.method = "get"
        self.controls: list[tuple[str, list[str], str]] = []
        self.next_href: str | None = None
        self._select: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.action = attributes.get("action")
            self.method = (attributes.get("method") or "get").lower()
            return
        if self.action is None:
            return
        if tag == "a" and "next" in (attributes.get("class") or "").split():
            self.next_href = attributes.get("href")
            return
        name = attributes.get("name")
        if tag == "input" and name:
            self.controls.append(
                (
                    name,
                    [attributes.get("value") or ""],
                    attributes.get("type") or "text",
                )
            )
        elif tag == "select":
            self._select = {
                "name": name,
                "multiple": "multiple" in attributes,
                "options": [],
            }
        elif tag == "option" and self._select is not None:
            options = self._select["options"]
            assert isinstance(options, list)
            options.append((attributes.get("value") or "", "selected" in attributes))

    def handle_endtag(self, tag: str) -> None:
        if tag != "select" or self._select is None:
            return
        select = self._select
        self._select = None
        name = select["name"]
        if not name:
            return
        options = select["options"]
        assert isinstance(options, list)
        selected = [value for value, is_selected in options if is_selected]
        if not selected and not select["multiple"] and options:
            selected = [options[0][0]]
        self.controls.append((str(name), selected, "select"))


@dataclasses.dataclass(frozen=True)
class Form:
    action: str
    method: str
    fields: tuple[tuple[str, tuple[str, ...]], ...]
    next_href: str | None

    @classmethod
    def load(cls) -> Form:
        parser = _FormParser()
        parser.feed(FIXTURE.read_text(encoding="utf-8"))
        assert parser.action is not None
        return cls(
            action=parser.action,
            method=parser.method,
            fields=tuple(
                (name, tuple(values)) for name, values, _type in parser.controls
            ),
            next_href=parser.next_href,
        )

    def submit(self, site: str = SITE, separator: str = SEPARATOR) -> str:
        """Return the URL a browser requests for a GET submission of the form."""
        action = urljoin(site + "/", self.action)
        target, _, fragment = action.partition("#")
        target = target.partition("?")[0]
        query = separator.join(
            f"{quote_plus(name, safe='')}={quote_plus(value, safe='')}"
            for name, values in self.fields
            for value in values
        )
        url = f"{target}?{query}"
        return f"{url}#{fragment}" if fragment else url


def query_pairs(url: str, separator: str = SEPARATOR) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True, separator=separator)


def served_page(url: str) -> int:
    """Page the local server serves, per the fixture contract."""
    for name, value in query_pairs(url):
        if name == "page" and value:
            return int(value)
    return 1


def served_tags(url: str) -> list[str]:
    return [value for name, value in query_pairs(url) if name == "tag"]


def with_page(url: str, page: int | str) -> str:
    return add_or_replace_parameters(url, {"page": str(page)}, separator=SEPARATOR)


def dedup_key(url: str, *, keep_fragments: bool = False) -> str:
    return canonicalize_url(
        url, query_separator=SEPARATOR, keep_fragments=keep_fragments
    )


def whitelist(url: str, names: tuple[str, ...], *, unique: bool = False) -> str:
    return url_query_cleaner(
        url,
        list(names),
        sep=SEPARATOR,
        unique=unique,
        keep_fragments=True,
    )


def blacklist(url: str, names: tuple[str, ...], *, unique: bool = False) -> str:
    return url_query_cleaner(
        url,
        list(names),
        sep=SEPARATOR,
        unique=unique,
        remove=True,
        keep_fragments=True,
    )


@pytest.fixture(scope="module")
def form() -> Form:
    return Form.load()


def test_fixture_get_submission_drops_action_query_but_keeps_fragment(
    form: Form,
) -> None:
    assert form.method == "get"
    assert form.action == "/search?page=1;ref=home#results"

    assert form.submit() == SUBMITTED
    # The action's own query string (?page=1;ref=home) is replaced by the form
    # data, so "ref" never reaches the server...
    assert "ref=home" not in form.submit()
    # ...while the action fragment does.
    assert urlsplit(form.submit()).fragment == "results"

    # Control order is preserved, including the blank page box before the
    # hidden page=1, and the repeated tag values.
    assert query_pairs(SUBMITTED) == [
        ("q", "a;b&c"),
        ("tag", "x"),
        ("tag", "y"),
        ("page", ""),
        ("page", "1"),
        ("sort", ""),
        ("utm_source", "crawl"),
    ]


def test_fixture_next_link_only_carries_the_page_condition(form: Form) -> None:
    assert form.next_href == "/search?page=2#results"
    next_url = urljoin(SITE + "/", form.next_href)
    assert next_url == f"{SITE}/search?page=2#results"
    assert served_page(next_url) == 2
    assert served_tags(next_url) == []
    # De-duplicated on its own, the bare next link is its own address.
    assert dedup_key(next_url) == f"{SITE}/search?page=2"
    # The fixture link carries no filters, so it is a *different* result page
    # than paging the submitted form: de-duplication keeps them apart.
    carried = with_page(whitelist(with_page(SUBMITTED, 2), FILTERS), 2)
    assert carried == (f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;sort=;page=2#results")
    assert served_page(carried) == 2
    assert served_tags(carried) == ["x", "y"]
    assert served_tags(next_url) == []
    assert dedup_key(carried) == f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y"
    assert dedup_key(next_url) != dedup_key(carried)


def test_request_parameters_are_interpreted_with_the_explicit_separator() -> None:
    # With the declared ";" separator the submitted URL has seven parameters.
    assert served_page(SUBMITTED) == 1
    assert served_tags(SUBMITTED) == ["x", "y"]
    # First non-blank page wins; the blank page box does not select page 1 by
    # itself, and a blank "sort" means the site default.
    assert url_query_parameter(SUBMITTED, "page", separator=SEPARATOR) == "1"
    assert url_query_parameter(SUBMITTED, "page", separator=SEPARATOR) is not None
    assert (
        url_query_parameter(SUBMITTED, "sort", keep_blank_values=1, separator=SEPARATOR)
        == ""
    )
    assert url_query_parameter(SUBMITTED, "sort", separator=SEPARATOR) is None

    # The same wire format read with the default "&" separator is one giant
    # parameter: the whole page selection collapses into q.
    assert query_pairs(SUBMITTED, "&") == [
        ("q", "a;b&c;tag=x;tag=y;page=;page=1;sort=;utm_source=crawl")
    ]


def test_safe_url_string_keeps_the_wire_format_unchanged() -> None:
    # safe_url_string() fixes syntax only; it must not touch the separator,
    # blank values, repeated parameters or the fragment.
    assert safe_url_string(SUBMITTED) == SUBMITTED
    assert safe_url_string(safe_url_string(SUBMITTED)) == SUBMITTED


@pytest.mark.parametrize(
    ("page", "expected_request"),
    [
        (
            1,
            (
                f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page=1"
                ";sort=;utm_source=crawl#results"
            ),
        ),
        (
            2,
            (
                f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page=2"
                ";sort=;utm_source=crawl#results"
            ),
        ),
        (
            3,
            (
                f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page=3"
                ";sort=;utm_source=crawl#results"
            ),
        ),
    ],
)
def test_merging_the_page_collapses_the_repeated_blank_page(
    page: int, expected_request: str
) -> None:
    request = with_page(SUBMITTED, page)
    assert request == expected_request
    assert served_page(request) == page
    # The merge replaced *all* page occurrences, not just the first.
    assert [v for k, v in query_pairs(request) if k == "page"] == [str(page)]
    # Untouched parameters keep their values and relative order.
    assert query_pairs(request) == [
        ("q", "a;b&c"),
        ("tag", "x"),
        ("tag", "y"),
        ("page", str(page)),
        ("sort", ""),
        ("utm_source", "crawl"),
    ]


@pytest.mark.parametrize(
    ("page", "expected_key"),
    [
        (
            1,
            f"{SITE}/search?page=1;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl",
        ),
        (
            2,
            f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl",
        ),
        (
            3,
            f"{SITE}/search?page=3;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl",
        ),
    ],
)
def test_dedup_key_is_stable_per_page_and_sorted(page: int, expected_key: str) -> None:
    request = with_page(SUBMITTED, page)
    assert dedup_key(request) == expected_key
    # Sorted parameters, plus-stable values and the fragment dropped.
    assert "utm_source" in expected_key
    assert "#" not in expected_key
    # Same content, lowercase percent escape on the wire -> same key.
    reshuffled = with_page(SUBMITTED, page).replace("q=a%3Bb%26c", "q=a%3bb%26c")
    assert dedup_key(reshuffled) == expected_key


def test_fragment_dropped_by_default_and_kept_on_request() -> None:
    request = with_page(SUBMITTED, 2)
    key = f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl"
    assert dedup_key(request) == key
    assert dedup_key(request, keep_fragments=True) == f"{key}#results"
    # Two different fragments are the same page for crawling...
    assert dedup_key(request) == dedup_key(request.replace("#results", "#top"))
    # ...but are preserved as distinct addresses when fragments are kept.
    assert dedup_key(request, keep_fragments=True) != dedup_key(
        request.replace("#results", "#top"), keep_fragments=True
    )
    # url_query_cleaner follows the same default: fragments go unless asked for.
    assert whitelist(request, ALLOWED).endswith("#results")
    assert (
        url_query_cleaner(request, list(ALLOWED), sep=SEPARATOR, unique=False)
        == f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page=2;sort="
    )


ENCODING_VARIANTS = [
    (
        "uppercase percent escape",
        (
            f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page=2"
            ";sort=;utm_source=crawl#results"
        ),
    ),
    (
        "lowercase percent escape",
        (
            f"{SITE}/search?q=a%3bb%26c;tag=x;tag=y;page=2"
            ";sort=;utm_source=crawl#results"
        ),
    ),
    (
        "encoded value digit",
        (
            f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page=%32"
            ";sort=;utm_source=crawl#results"
        ),
    ),
    (
        "bare key instead of key=",
        (f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page=2;sort;utm_source=crawl#results"),
    ),
]


def test_encoding_only_variants_of_the_same_page_share_one_dedup_key() -> None:
    expected = f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl"
    for description, variant in ENCODING_VARIANTS:
        # Request semantics are identical for every spelling...
        assert served_page(variant) == 2, description
        assert served_tags(variant) == ["x", "y"], description
        # ...and so is the de-duplication address.
        assert dedup_key(variant) == expected, description
        assert dedup_key(variant, keep_fragments=True) == f"{expected}#results"
    # The page they select is genuinely different from page 3.
    assert dedup_key(with_page(SUBMITTED, 3)) != expected


REPEATED_PAGE_ORDER = [
    (
        "page=2;page=1",
        f"{SITE}/search?page=2;page=1;tag=x;tag=y#results",
        f"{SITE}/search?page=2;tag=x;tag=y",
        2,
    ),
    (
        "page=1;page=2",
        f"{SITE}/search?page=1;page=2;tag=x;tag=y#results",
        f"{SITE}/search?page=1;tag=x;tag=y",
        1,
    ),
]


@pytest.mark.parametrize(("description", "url", "folded", "page"), REPEATED_PAGE_ORDER)
def test_repeated_page_order_is_meaningful_but_default_dedup_ignores_it(
    description: str, url: str, folded: str, page: int
) -> None:
    # The two addresses differ only in the order of a repeated parameter, which
    # the fixture server treats as the page selector: they are different pages.
    assert served_page(url) == page, description
    assert urlsplit(url).query != urlsplit(folded).query

    # Default canonicalization sorts by (key, value), so it maps both orders to
    # one address. This is the documented boundary: canonicalize_url() is for
    # de-duplication and may point at a different resource.
    assert served_page(folded) == page
    assert dedup_key(url) == dedup_key(url.replace("page=2;page=1", "page=1;page=2"))
    assert dedup_key(url) == f"{SITE}/search?page=1;page=2;tag=x;tag=y"

    # Merging the effective page value replaces the repeated parameter with the
    # one value that selects it, and that single-value form de-duplicates to a
    # page-specific address, distinct from the other page's.
    folded_request = with_page(url, page)
    other_page = 1 if page == 2 else 2
    other_request = with_page(url, other_page)
    assert folded_request == f"{folded}#results"
    assert served_page(folded_request) == page
    assert dedup_key(folded_request) == folded
    assert dedup_key(folded_request) != dedup_key(other_request)
    # Merging the wrong value would change the served page; the key follows.
    assert served_page(with_page(url, other_page)) == other_page
    assert dedup_key(other_request) == f"{SITE}/search?page={other_page};tag=x;tag=y"


def test_order_preserving_dedup_key_keeps_repeated_parameter_semantics() -> None:
    """A caller-supplied key that preserves order does keep the pages apart."""

    def order_key(url: str, *, keep_fragments: bool = False) -> str:
        grouped: dict[str, list[str]] = {}
        for name, value in query_pairs(url):
            grouped.setdefault(name, []).append(value)
        folded = {name: "|".join(values) for name, values in grouped.items()}
        merged = add_or_replace_parameters(url, folded, separator=SEPARATOR)
        return canonicalize_url(
            merged, query_separator=SEPARATOR, keep_fragments=keep_fragments
        )

    page_2_first = f"{SITE}/search?page=2;page=1;tag=x;tag=y#results"
    page_1_first = f"{SITE}/search?page=1;page=2;tag=x;tag=y#results"
    assert order_key(page_2_first) == f"{SITE}/search?page=2%7C1;tag=x%7Cy"
    assert order_key(page_1_first) == f"{SITE}/search?page=1%7C2;tag=x%7Cy"
    assert order_key(page_2_first) != order_key(page_1_first)
    # The same key rule still merges nothing else, and is idempotent.
    assert order_key(order_key(page_2_first)) == order_key(page_2_first)
    assert order_key(page_2_first, keep_fragments=True).endswith("#results")


def test_repeated_filter_order_is_meaningful_and_also_flattened_by_default() -> None:
    x_then_y = f"{SITE}/search?page=2;tag=x;tag=y#results"
    y_then_x = f"{SITE}/search?page=2;tag=y;tag=x#results"
    # Same parameters, different order -> different result pages.
    assert served_tags(x_then_y) == ["x", "y"]
    assert served_tags(y_then_x) == ["y", "x"]
    assert dedup_key(x_then_y) == dedup_key(y_then_x)
    assert dedup_key(x_then_y) == f"{SITE}/search?page=2;tag=x;tag=y"
    # The compromise that keeps the order: fold repeated values into one
    # ordered parameter, then de-duplicate.
    folded_x = add_or_replace_parameters(x_then_y, {"tag": "x,y"}, separator=SEPARATOR)
    folded_y = add_or_replace_parameters(y_then_x, {"tag": "y,x"}, separator=SEPARATOR)
    assert folded_x == f"{SITE}/search?page=2;tag=x%2Cy#results"
    assert folded_y == f"{SITE}/search?page=2;tag=y%2Cx#results"
    assert dedup_key(folded_x) == f"{SITE}/search?page=2;tag=x%2Cy"
    assert dedup_key(folded_y) == f"{SITE}/search?page=2;tag=y%2Cx"
    assert dedup_key(folded_x) != dedup_key(folded_y)


def test_explicit_separator_added_after_percent_encoding_stays_inside_values() -> None:
    # A raw ";" inside a value is ambiguous once ";" is also the separator;
    # percent-encoding it keeps it as data.
    encoded = f"{SITE}/search?q=a%3Bb%26c;tag=x"
    assert query_pairs(encoded) == [("q", "a;b&c"), ("tag", "x")]
    decoded = f"{SITE}/search?q=a;b&c;tag=x"
    assert query_pairs(decoded) == [("q", "a"), ("b&c", ""), ("tag", "x")]
    # The raw form loses the q value and invents a "b&c" parameter, which is
    # exactly the expansion the encoded form avoids.
    assert len(query_pairs(decoded)) > len(query_pairs(encoded))
    assert served_tags(decoded) == served_tags(encoded) == ["x"]


@pytest.mark.parametrize("page", [1, 2, 3])
def test_whitelist_that_keeps_page_preserves_the_crawl_range(page: int) -> None:
    request = with_page(SUBMITTED, page)
    cleaned = whitelist(request, ALLOWED)
    assert cleaned == (
        f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page={page};sort=#results"
    )
    assert served_page(cleaned) == page
    assert served_tags(cleaned) == ["x", "y"]
    assert "utm_source" not in cleaned


def test_whitelist_that_keeps_page_does_not_expand_the_crawl_range() -> None:
    keys = {
        dedup_key(whitelist(with_page(SUBMITTED, page), ALLOWED)) for page in (1, 2, 3)
    }
    assert keys == {
        f"{SITE}/search?page=1;q=a%3Bb%26c;sort=;tag=x;tag=y",
        f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y",
        f"{SITE}/search?page=3;q=a%3Bb%26c;sort=;tag=x;tag=y",
    }


@pytest.mark.parametrize("page", [1, 2, 3])
def test_whitelist_without_page_erases_the_page_when_applied_after(page: int) -> None:
    request = with_page(SUBMITTED, page)
    cleaned = whitelist(request, FILTERS)
    assert cleaned == f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;sort=#results"
    # The crawl silently falls back to page 1: the range shrank.
    assert served_page(cleaned) == 1
    assert dedup_key(cleaned) == f"{SITE}/search?q=a%3Bb%26c;sort=;tag=x;tag=y"


def test_whitelist_without_page_collapses_every_page_into_one_key() -> None:
    assert {
        dedup_key(whitelist(with_page(SUBMITTED, page), FILTERS)) for page in (1, 2, 3)
    } == {f"{SITE}/search?q=a%3Bb%26c;sort=;tag=x;tag=y"}


@pytest.mark.parametrize("page", [1, 2, 3])
def test_clean_before_paging_restores_the_intended_range(page: int) -> None:
    request = with_page(SUBMITTED, page)
    # Correct order: clean the filters first, add the page condition last.
    rebuilt = with_page(whitelist(request, FILTERS), page)
    assert rebuilt == f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;sort=;page={page}#results"
    assert served_page(rebuilt) == page
    assert dedup_key(rebuilt) == (
        f"{SITE}/search?page={page};q=a%3Bb%26c;sort=;tag=x;tag=y"
    )


def test_clean_before_paging_yields_one_key_per_page() -> None:
    assert {
        dedup_key(with_page(whitelist(with_page(SUBMITTED, page), FILTERS), page))
        for page in (1, 2, 3)
    } == {
        f"{SITE}/search?page=1;q=a%3Bb%26c;sort=;tag=x;tag=y",
        f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y",
        f"{SITE}/search?page=3;q=a%3Bb%26c;sort=;tag=x;tag=y",
    }


@pytest.mark.parametrize("page", [1, 2, 3])
def test_blacklisting_the_tracking_parameter_keeps_the_range(page: int) -> None:
    request = with_page(SUBMITTED, page)
    cleaned = blacklist(request, ("utm_source",))
    assert cleaned == (
        f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;page={page};sort=#results"
    )
    assert served_page(cleaned) == page
    assert dedup_key(cleaned) == (
        f"{SITE}/search?page={page};q=a%3Bb%26c;sort=;tag=x;tag=y"
    )


def test_blacklisting_the_tracking_parameter_merges_only_duplicate_targets() -> None:
    # Three pages x two tracking values = six raw URLs; after the blacklist the
    # tracking variant stops mattering, leaving exactly three crawl targets.
    requests = [
        with_page(SUBMITTED, page).replace("utm_source=crawl", f"utm_source={source}")
        for page in (1, 2, 3)
        for source in ("crawl", "newsletter")
    ]
    assert len({dedup_key(url) for url in requests}) == 6
    assert {dedup_key(blacklist(url, ("utm_source",))) for url in requests} == {
        f"{SITE}/search?page=1;q=a%3Bb%26c;sort=;tag=x;tag=y",
        f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y",
        f"{SITE}/search?page=3;q=a%3Bb%26c;sort=;tag=x;tag=y",
    }


@pytest.mark.parametrize("page", [1, 2, 3])
def test_blacklisting_the_page_parameter_shrinks_the_range(page: int) -> None:
    request = with_page(SUBMITTED, page)
    cleaned = blacklist(request, ("page",))
    assert (
        cleaned
        == f"{SITE}/search?q=a%3Bb%26c;tag=x;tag=y;sort=;utm_source=crawl#results"
    )
    # Without an explicit page the server serves page 1, for every target.
    assert served_page(cleaned) == 1
    assert dedup_key(cleaned) == (
        f"{SITE}/search?q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl"
    )


def test_blacklisting_the_page_parameter_collapses_every_page_into_one_key() -> None:
    assert {
        dedup_key(blacklist(with_page(SUBMITTED, page), ("page",)))
        for page in (1, 2, 3)
    } == {
        f"{SITE}/search?q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl",
    }
    # Re-adding the page after cleaning restores three distinct targets.
    assert {
        dedup_key(with_page(blacklist(with_page(SUBMITTED, page), ("page",)), page))
        for page in (1, 2, 3)
    } == {
        f"{SITE}/search?page=1;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl",
        f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl",
        f"{SITE}/search?page=3;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl",
    }


def test_default_separator_on_this_page_widens_the_range_instead_of_cleaning() -> None:
    # A whitelist that keeps "tag" and "page" but splits with "&" sees only the
    # first key of a ";"-joined query, so every filter disappears, not just the
    # tracking ones: the page collapses to a bare /search.
    for page in (1, 2, 3):
        cleaned = url_query_cleaner(
            with_page(SUBMITTED, page),
            ["tag", "page"],
            sep="&",
            unique=False,
            keep_fragments=True,
        )
        assert cleaned == f"{SITE}/search#results"
        assert dedup_key(cleaned) == f"{SITE}/search"
    # A blacklist for the tracking key with the same wrong separator removes
    # nothing at all, so the tracking value keeps splitting the crawl.
    requests = [
        with_page(SUBMITTED, page).replace("utm_source=crawl", f"utm_source={source}")
        for page in (1, 2, 3)
        for source in ("crawl", "newsletter")
    ]
    assert {
        dedup_key(
            url_query_cleaner(
                url,
                ["utm_source"],
                sep="&",
                unique=False,
                remove=True,
                keep_fragments=True,
            )
        )
        for url in requests
    } == {dedup_key(url) for url in requests}
    assert len({dedup_key(url) for url in requests}) == 6


def test_merging_with_the_default_separator_does_not_change_the_served_page() -> None:
    wrong = add_or_replace_parameters(SUBMITTED, {"page": "2"})
    # The "&" merge keeps the whole old query as one percent-encoded value, so
    # the page condition submitted by the form still wins on ";"-parsing...
    assert wrong == (
        f"{SITE}/search?q=a%3Bb%26c%3Btag%3Dx%3Btag%3Dy"
        "%3Bpage%3D%3Bpage%3D1%3Bsort%3D%3Butm_source%3Dcrawl&page=2#results"
    )
    assert served_page(wrong) == 1
    assert url_query_parameter(wrong, "page", separator=SEPARATOR) is None
    assert url_query_parameter(wrong, "page", separator="&") == "2"
    # ...and the de-duplication key is a completely different string.
    assert dedup_key(wrong) != dedup_key(with_page(SUBMITTED, 2))


def test_default_dedup_cannot_sort_the_real_parameters() -> None:
    # With the default "&" separator the whole ";"-query is one value, so
    # canonicalize_url() can only re-encode it, not sort the parameters inside.
    reordered = (
        f"{SITE}/search?tag=x;tag=y;q=a%3Bb%26c;page=;page=1"
        ";sort=;utm_source=crawl#results"
    )
    assert served_page(SUBMITTED) == served_page(reordered) == 1
    assert served_tags(SUBMITTED) == served_tags(reordered) == ["x", "y"]
    # Default keys therefore differ for the same logical page...
    assert canonicalize_url(SUBMITTED) != canonicalize_url(reordered)
    assert canonicalize_url(SUBMITTED) == (
        f"{SITE}/search?q=a%3Bb%26c%3Btag%3Dx%3Btag%3Dy"
        "%3Bpage%3D%3Bpage%3D1%3Bsort%3D%3Butm_source%3Dcrawl"
    )
    # ...while passing the declared separator sorts and merges them properly.
    assert canonicalize_url(SUBMITTED, query_separator=SEPARATOR) == (
        f"{SITE}/search?page=;page=1;q=a%3Bb%26c;sort=;tag=x;tag=y;utm_source=crawl"
    )
    assert canonicalize_url(SUBMITTED, query_separator=SEPARATOR) == canonicalize_url(
        reordered, query_separator=SEPARATOR
    )


def test_blank_page_handling_depends_on_keep_blank_values_and_uniqueness() -> None:
    blank_first = f"{SITE}/search?page=;page=2#results"
    assert served_page(blank_first) == 2
    # Keep blanks (default): the blank and the real value both survive, so the
    # request still serves page 2...
    assert dedup_key(blank_first) == f"{SITE}/search?page=;page=2"
    # ...unless blanks are dropped, in which case it still serves page 2, while
    # unique=True keeps only the *first* (blank) value and changes the page.
    assert (
        canonicalize_url(
            blank_first,
            query_separator=SEPARATOR,
            keep_blank_values=False,
            keep_fragments=True,
        )
        == f"{SITE}/search?page=2#results"
    )
    collapsed = whitelist(blank_first, ("page",), unique=True)
    assert collapsed == f"{SITE}/search?page=#results"
    assert url_query_parameter(collapsed, "page", separator=SEPARATOR) is None
    # Keeping duplicates (or folding the effective page) preserves the target.
    assert whitelist(blank_first, ("page",)) == blank_first
    assert with_page(blank_first, served_page(blank_first)) == (
        f"{SITE}/search?page=2#results"
    )
    assert dedup_key(with_page(blank_first, served_page(blank_first))) == (
        f"{SITE}/search?page=2"
    )


def test_each_transform_is_idempotent_under_its_own_contract() -> None:
    request = with_page(SUBMITTED, 2)

    assert whitelist(whitelist(request, ALLOWED), ALLOWED) == whitelist(
        request, ALLOWED
    )
    assert whitelist(whitelist(request, FILTERS), FILTERS) == whitelist(
        request, FILTERS
    )
    assert blacklist(blacklist(request, ("utm_source",)), ("utm_source",)) == (
        blacklist(request, ("utm_source",))
    )
    assert with_page(request, 2) == request

    key = dedup_key(request)
    assert dedup_key(key) == key
    # The dedup key is itself a valid request address for the same page.
    assert served_page(key) == 2
    assert with_page(key, 2) == key


def test_duplicate_transformations_compose_in_the_expected_order() -> None:
    request = with_page(SUBMITTED, 2)

    cleaned = blacklist(request, ("utm_source",))
    key = dedup_key(cleaned)
    assert key == f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y"
    # Re-running clean on the key only reorders parameters in the key.
    assert dedup_key(blacklist(key, ("utm_source",))) == key
    # Cleaning again for the whitelist is stable and still points at page 2.
    assert dedup_key(whitelist(key, ALLOWED)) == (
        f"{SITE}/search?page=2;q=a%3Bb%26c;sort=;tag=x;tag=y"
    )
    assert served_page(whitelist(key, ALLOWED)) == 2
