from pathlib import Path

import pytest

from sieve.core.junit import (
    JUnitParseError,
    ParsedTestResult,
    Status,
    make_test_id,
    parse_junit,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[ParsedTestResult]:
    return parse_junit((FIXTURES / name).read_bytes())


def by_id(results: list[ParsedTestResult]) -> dict[str, list[ParsedTestResult]]:
    grouped: dict[str, list[ParsedTestResult]] = {}
    for result in results:
        grouped.setdefault(result.test_id, []).append(result)
    return grouped


def only(results: list[ParsedTestResult], test_id: str) -> ParsedTestResult:
    [result] = by_id(results)[test_id]
    return result


# --- pytest -------------------------------------------------------------------------------


def test_pytest_fixture() -> None:
    results = load("pytest.xml")

    assert len(results) == 7
    assert [r.status for r in results].count(Status.PASSED) == 3
    assert all(r.attempt == 1 for r in results)
    # Default xunit2 output has no file attribute.
    assert all(r.file_path is None for r in results)

    add = only(results, "tests.test_math::test_add")
    assert add.status is Status.PASSED
    assert add.classname == "tests.test_math"
    assert add.name == "test_add"
    assert add.duration_ms == 1

    assert "tests.test_math::test_add_params[1-2-3]" in by_id(results)
    assert "tests.test_math::test_add_params[-1-1-0]" in by_id(results)

    failed = only(results, "tests.test_math.TestDivide::test_divide_by_zero")
    assert failed.status is Status.FAILED
    assert failed.message == "assert 1 == 2"
    assert failed.duration_ms == 123

    skipped = only(results, "tests.test_io::test_needs_network")
    assert skipped.status is Status.SKIPPED
    assert skipped.message == "needs network"

    xfail = only(results, "tests.test_io::test_known_bug")
    assert xfail.status is Status.SKIPPED

    error = only(results, "tests.test_io::test_fixture_error")
    assert error.status is Status.ERROR
    assert error.message == 'failed on setup with "RuntimeError: db unavailable"'


# --- Jest (jest-junit) --------------------------------------------------------------------


def test_jest_fixture() -> None:
    results = load("jest.xml")

    assert len(results) == 5
    assert [r.status for r in results] == [
        Status.PASSED,
        Status.FAILED,
        Status.SKIPPED,
        Status.PASSED,
        Status.PASSED,
    ]

    added = only(results, "cart adds an item::cart adds an item")
    assert added.duration_ms == 4

    # jest-junit joins nested describe() titles with U+203A.
    title = "cart totals › applies discount"  # noqa: RUF001
    failed = only(results, f"{title}::{title}")
    assert failed.status is Status.FAILED
    # jest-junit puts the failure in the element body, not a message attribute.
    assert failed.message is not None
    assert failed.message.startswith("Error: expect(received).toBe(expected)")

    skipped = only(results, "cart persists to storage::cart persists to storage")
    assert skipped.message is None
    assert skipped.duration_ms == 0

    # Runs of whitespace are collapsed so the id is stable across reporter versions.
    assert "formatPrice formats EUR::formatPrice formats EUR" in by_id(results)


# --- Go (go-junit-report) -----------------------------------------------------------------


def test_go_fixture() -> None:
    results = load("go.xml")

    assert len(results) == 5

    passed = only(results, "github.com/acme/shop/cart::TestAddItem")
    assert passed.status is Status.PASSED
    assert passed.duration_ms == 0

    parent = only(results, "github.com/acme/shop/cart::TestTotal")
    subtest = only(results, "github.com/acme/shop/cart::TestTotal/with_discount")
    assert parent.status is Status.FAILED
    assert subtest.status is Status.FAILED
    assert subtest.message == "Failed"

    charge = only(results, "github.com/acme/shop/payment::TestCharge")
    assert charge.duration_ms == 1500

    skipped = only(results, "github.com/acme/shop/payment::TestRefund")
    assert skipped.status is Status.SKIPPED
    assert skipped.message == "refund_test.go:10: flaky upstream sandbox"


# --- Maven Surefire -----------------------------------------------------------------------

CART = "com.acme.shop.CartServiceTest"


def test_maven_fixture_basic_outcomes() -> None:
    results = by_id(load("maven-surefire.xml"))

    [added] = results[f"{CART}::addsItem"]
    assert (added.status, added.attempt, added.duration_ms) == (Status.PASSED, 1, 12)

    [error] = results[f"{CART}::loadsPrices"]
    assert error.status is Status.ERROR
    assert error.message == "Connection refused"
    # Locale-formatted time from older Surefire versions.
    assert error.duration_ms == 1_200_001

    [skipped] = results[f"{CART}::appliesCoupon"]
    assert skipped.status is Status.SKIPPED
    assert skipped.message == "disabled until COUPON-12"


def test_maven_rerun_failures_become_extra_failed_attempts() -> None:
    attempts = by_id(load("maven-surefire.xml"))[f"{CART}::computesTotal"]

    assert [(a.attempt, a.status) for a in attempts] == [
        (1, Status.FAILED),
        (2, Status.FAILED),
        (3, Status.FAILED),
    ]
    assert [a.duration_ms for a in attempts] == [34, None, None]
    assert attempts[0].message == "expected: <30> but was: <25>"
    assert attempts[1].message == "expected: <30> but was: <26>"


def test_maven_flaky_attempts_precede_final_pass() -> None:
    results = by_id(load("maven-surefire.xml"))

    stock = results[f"{CART}::checksStock"]
    assert [(a.attempt, a.status) for a in stock] == [(1, Status.FAILED), (2, Status.PASSED)]
    assert stock[0].message == "Timed out after 50ms"
    assert [a.duration_ms for a in stock] == [None, 51]

    reserve = results[f"{CART}::reservesStock"]
    assert [(a.attempt, a.status) for a in reserve] == [
        (1, Status.ERROR),
        (2, Status.FAILED),
        (3, Status.PASSED),
    ]


# --- Go, package attribute (ipfs/kubo format) ---------------------------------------------

CLI = "github.com/ipfs/kubo/test/cli"
COREUNIX = "github.com/ipfs/kubo/core/coreunix"


def test_go_package_attribute_fixture() -> None:
    results = load("go-package-attr.xml")

    assert [(r.test_id, r.status, r.attempt) for r in results] == [
        (f"{CLI}::TestAdd", Status.PASSED, 1),
        (f"{CLI}::TestAdd/produced_cid_version:_implicit_default_(CIDv0)", Status.PASSED, 1),
        (f"{CLI}::TestAdd/ipfs_add_--to-files", Status.SKIPPED, 1),
        (f"{COREUNIX}::TestAdd", Status.FAILED, 1),
        (f"{COREUNIX}::TestAdd/ipfs_add_--to-files", Status.PASSED, 1),
    ]
    assert all(r.classname in (CLI, COREUNIX) for r in results)

    failed = only(results, f"{COREUNIX}::TestAdd")
    assert failed.message == "Failed"
    assert failed.duration_ms == 380
    assert only(results, f"{CLI}::TestAdd/ipfs_add_--to-files").message == "SKIP"


def test_same_named_tests_in_different_packages_get_different_ids() -> None:
    results = load("go-package-attr.xml")

    # Both packages define TestAdd and TestAdd/ipfs_add_--to-files. Keyed by suite name they
    # would share an ID, and the second would be miscounted as a retry (attempt 2).
    for name in ("TestAdd", "TestAdd/ipfs_add_--to-files"):
        ids = {r.test_id for r in results if r.name == name}
        assert ids == {f"{CLI}::{name}", f"{COREUNIX}::{name}"}
    assert len({r.test_id for r in results}) == len(results)
    assert all(r.attempt == 1 for r in results)


def test_classname_package_and_suite_name_precedence() -> None:
    xml = """
    <testsuites>
      <testsuite name="Outer" package="example.com/outer">
        <testcase classname="explicit.Class" name="has_classname"/>
        <testcase name="uses_package"/>
        <testsuite name="Inner">
          <testcase name="inherits_outer_package"/>
        </testsuite>
        <testsuite name="Other" package="example.com/other">
          <testcase name="uses_own_package"/>
        </testsuite>
      </testsuite>
      <testsuite name="NoPackage">
        <testcase name="uses_suite_name"/>
      </testsuite>
    </testsuites>
    """
    assert [r.test_id for r in parse_junit(xml)] == [
        "explicit.Class::has_classname",
        "example.com/outer::uses_package",
        "example.com/outer::inherits_outer_package",
        "example.com/other::uses_own_package",
        "NoPackage::uses_suite_name",
    ]


@pytest.mark.parametrize(
    ("root_attrs", "expected_id"),
    [
        ('name="com.acme.FooTest"', "com.acme.FooTest::bar"),
        ('name="TestFoo" package="example.com/pkg"', "example.com/pkg::bar"),
    ],
    ids=["suite-name", "package"],
)
def test_bare_testsuite_root_attributes_apply_to_its_testcases(
    root_attrs: str, expected_id: str
) -> None:
    # Regression: the root <testsuite>'s own attributes were ignored, giving "::bar".
    [result] = parse_junit(
        f'<testsuite {root_attrs} file="src/foo.go"><testcase name="bar"/></testsuite>'
    )
    assert result.test_id == expected_id
    assert result.file_path == "src/foo.go"


# --- dialect edge cases -------------------------------------------------------------------


def test_repeated_testcase_elements_count_as_attempts() -> None:
    # Gradle test-retry and similar plugins repeat the whole <testcase>.
    xml = """
    <testsuite name="com.acme.FooTest">
      <testcase classname="com.acme.FooTest" name="bar" time="0.1"><failure/></testcase>
      <testcase classname="com.acme.FooTest" name="baz" time="0.1"/>
      <testcase classname="com.acme.FooTest" name="bar" time="0.2"/>
    </testsuite>
    """
    attempts = by_id(parse_junit(xml))["com.acme.FooTest::bar"]
    assert [(a.attempt, a.status, a.duration_ms) for a in attempts] == [
        (1, Status.FAILED, 100),
        (2, Status.PASSED, 200),
    ]


def test_file_path_from_testcase_or_suite_is_normalized() -> None:
    xml = r"""
    <testsuites>
      <testsuite name="outer" file=".\src\outer.test.js">
        <testcase classname="a" name="inherits suite file"/>
        <testcase classname="a" name="own file" file="./tests/test_own.py"/>
        <testsuite name="inner">
          <testcase name="no classname"/>
        </testsuite>
      </testsuite>
    </testsuites>
    """
    results = parse_junit(xml)
    assert [(r.test_id, r.file_path) for r in results] == [
        ("a::inherits suite file", "src/outer.test.js"),
        ("a::own file", "tests/test_own.py"),
        # Missing classname falls back to the nearest suite name; file is inherited.
        ("inner::no classname", "src/outer.test.js"),
    ]


def test_error_takes_precedence_over_failure_and_skipped() -> None:
    xml = """
    <testsuite name="s">
      <testcase classname="c" name="n">
        <skipped/><failure message="f"/><error message="e"/>
      </testcase>
    </testsuite>
    """
    [result] = parse_junit(xml)
    assert (result.status, result.message) == (Status.ERROR, "e")


def test_namespaced_elements_are_recognized() -> None:
    xml = """
    <testsuites xmlns="urn:example:junit">
      <testsuite name="s"><testcase classname="c" name="n"><failure/></testcase></testsuite>
    </testsuites>
    """
    [result] = parse_junit(xml)
    assert result.status is Status.FAILED


@pytest.mark.parametrize("raw", [None, "", "abc", "-1"])
def test_unusable_time_gives_no_duration(raw: str | None) -> None:
    time_attr = "" if raw is None else f' time="{raw}"'
    [result] = parse_junit(f'<testsuite><testcase classname="c" name="n"{time_attr}/></testsuite>')
    assert result.duration_ms is None


def test_empty_report_yields_no_results() -> None:
    assert parse_junit(b"<testsuites/>") == []


def test_make_test_id_collapses_whitespace() -> None:
    assert make_test_id("  pkg.Mod ", "test  x\n") == "pkg.Mod::test x"


# --- invalid input ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("xml", "match"),
    [
        ("<testsuite><testcase", "invalid JUnit XML"),
        ("<html><body/></html>", "unexpected root element <html>"),
        ('<testsuite><testcase classname="c"/></testsuite>', "without a name"),
    ],
)
def test_invalid_input_raises(xml: str, match: str) -> None:
    with pytest.raises(JUnitParseError, match=match):
        parse_junit(xml)


def test_entity_expansion_is_rejected() -> None:
    xml = """<?xml version="1.0"?>
    <!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
    <testsuite><testcase classname="c" name="&lol2;"/></testsuite>
    """
    with pytest.raises(JUnitParseError):
        parse_junit(xml)
