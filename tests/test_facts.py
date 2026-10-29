"""Format-tolerant fact matching: the same fact in another format is supported; an invented one is not."""

import pytest

from app.facts import FactSource

DAY_PLAN_REQUEST = "give me a doc of my day plan 3 am walk 8am to 12 pm metting then from 7 game"
PROJECT_REQUEST = (
    "Draft a project plan for our CRM migration. Priya owns the data migration, due March 31, 2027. "
    "Budget is $40,000. We have 3 phases."
)


@pytest.mark.parametrize(
    "value", ["8am-12pm", "8am to 12pm", "8am to 12 pm", "8:00 a.m. - noon", "8 - 12pm", "3am", "7 pm"]
)
def test_day_plan_times_in_another_format_are_supported(value: str) -> None:
    assert FactSource.from_text(DAY_PLAN_REQUEST).supports_value(value)


@pytest.mark.parametrize("value", ["10am", "4 am", "8am to 1pm", "7:30 pm"])
def test_invented_times_are_not_supported(value: str) -> None:
    assert not FactSource.from_text(DAY_PLAN_REQUEST).supports_value(value)


@pytest.mark.parametrize(
    "value", ["31 March", "31st of March", "Mar. 31", "03/31", "31/03/27", "2027-03-31", "March 31st, 2027"]
)
def test_dates_in_another_format_are_supported(value: str) -> None:
    assert FactSource.from_text(PROJECT_REQUEST).supports_value(value)


@pytest.mark.parametrize("value", ["April 15", "30 March", "2026-03-31", "31/03/2026"])
def test_invented_dates_and_years_are_not_supported(value: str) -> None:
    assert not FactSource.from_text(PROJECT_REQUEST).supports_value(value)


@pytest.mark.parametrize("value", ["$40,000", "40000", "40k", "40,000"])
def test_figures_in_another_format_are_supported(value: str) -> None:
    assert FactSource.from_text(PROJECT_REQUEST).supports_value(value)


def test_invented_figure_and_surname_are_not_supported() -> None:
    source = FactSource.from_text(PROJECT_REQUEST)

    assert not source.supports_value("$45,000")
    assert not source.supports_value("Priya Sharma")
    assert source.supports_value("priya")


def test_a_plain_count_is_not_read_as_a_time() -> None:
    # "3 phases" is a count; only a bare hour after "from", "at", "by"... or starting a range can be a time.
    assert not FactSource.from_text(PROJECT_REQUEST).supports_value("3 pm")


def test_quotes_must_be_one_run_of_the_request() -> None:
    source = FactSource.from_text(PROJECT_REQUEST)

    assert source.supports_quote("Priya owns the data-migration")
    assert source.supports_quote("due 31 March")
    assert source.supports_quote("Budget is 40k")
    assert not source.supports_quote("Priya owns the budget")  # every word is in the request, but not together
