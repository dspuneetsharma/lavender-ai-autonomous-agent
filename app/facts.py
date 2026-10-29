"""Format-tolerant fact matching for grounding: compare what a fact means, not how it is spelled.

Times, dates and numbers in both texts become canonical forms ("8am", "8 am" and "8:00 a.m." are all
08:00), so "8am-12pm" matches "8am to 12 pm" in the request while an invented "10am" still does not.
A bare hour such as "from 7" stays ambiguous and matches 07:00 or 19:00.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from app.guardrails import normalize_text

Token = frozenset[str]  # every reading of one word or fact, e.g. {"n7", "t07:00", "t19:00"}

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DAY = r"(?:[12]\d|3[01]|0?[1-9])(?:st|nd|rd|th)?"
_FACT = re.compile(
    r"\b(?:"
    r"(?P<iso>\d{4}-\d{2}-\d{2})"
    rf"|(?P<md_month>{_MONTH})\.?\s+(?P<md_day>{_DAY})"
    rf"|(?P<dm_day>{_DAY})\s+(?:of\s+)?(?P<dm_month>{_MONTH})"
    r"|(?P<slash>\d{1,2}/\d{1,2})(?:/(?P<slash_year>\d{4}|\d{2}))?"
    r"|(?P<clock>\d{1,2})(?::(?P<clock_min>\d{2}))?\s*(?P<meridiem>[ap])\.?m"
    r"|(?P<hhmm>\d{1,2}):(?P<hhmm_min>\d{2})"
    r"|(?P<named>noon|midday|midnight)"
    r"|(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<scale>[km])?"
    r")\b"
)
_WORD = re.compile(r"[^\W_]+|[-–—]")
_HYPHENATED = re.compile(r"(?<=[^\W\d_])[-–—](?=[^\W\d_])")  # "follow-up" -> "follow up"
_RANGE_WORDS = {"-": "to", "–": "to", "—": "to", "till": "to", "until": "to", "through": "to", "thru": "to"}
_HOUR_CONTEXT = frozenset({"from", "at", "by", "to", "between", "around", "before", "after"})
_CONNECTORS = frozenset({"a", "an", "and", "at", "by", "from", "in", "of", "on", "the", "to"})
_SCALES = {"k": 1_000, "m": 1_000_000}
_NAMED_TIMES = {"noon": "t12:00", "midday": "t12:00", "midnight": "t00:00"}


@dataclass(frozen=True)
class FactSource:
    """The request as canonical tokens, for checking values and quotes the model wrote."""

    tokens: tuple[Token, ...]
    readings: frozenset[str]

    @classmethod
    def from_text(cls, text: str) -> "FactSource":
        tokens = tuple(tokenize(text))
        return cls(tokens=tokens, readings=frozenset().union(*tokens))

    def supports_value(self, value: str) -> bool:
        """True if every time, date, number and word in an owner/due/value field is in the request."""
        meaningful = [token for token in tokenize(value) if not (token and token <= _CONNECTORS)]
        return bool(meaningful) and all(token & self.readings for token in meaningful)

    def supports_quote(self, quote: str) -> bool:
        """True if the quote appears in the request as one run of tokens, allowing format differences."""
        needle = tokenize(quote)
        if not needle:
            return False
        last_start = len(self.tokens) - len(needle)
        return any(
            all(wanted & found for wanted, found in zip(needle, self.tokens[start:]))
            for start in range(last_start + 1)
        )


def tokenize(text: str) -> list[Token]:
    """Split text into word tokens and canonical fact tokens (t=time, d=date, n=number)."""
    text = _HYPHENATED.sub(" ", normalize_text(text))
    tokens: list[Token] = []
    position = 0
    for match in _FACT.finditer(text):
        tokens.extend(_words(text[position : match.start()]))
        tokens.extend(_fact_tokens(match))
        position = match.end()
    tokens.extend(_words(text[position:]))
    return _with_bare_hours(tokens)


def _words(text: str) -> list[Token]:
    return [frozenset({_RANGE_WORDS.get(word, word)}) for word in _WORD.findall(text)]


def _fact_tokens(match: re.Match[str]) -> list[Token]:
    """A date with a year gives two tokens, so a year is checked like any other number."""
    groups = match.groupdict()
    if groups["iso"]:
        year, month, day = (int(part) for part in groups["iso"].split("-"))
        return [_date(month, day), _year(year)]
    if groups["slash"]:
        first, second = (int(part) for part in groups["slash"].split("/"))
        dates = _date(first, second) | _date(second, first)  # MM/DD or DD/MM
        year = groups["slash_year"]
        return [dates] if year is None else [dates, _year(int(year))]
    return [_fact_token(groups)]


def _year(year: int) -> Token:
    return frozenset({f"n{year + 2000 if year < 100 else year}"})  # "27" in 31/03/27 means 2027


def _fact_token(groups: dict[str, str | None]) -> Token:
    if groups["md_month"]:
        return _date(_month_number(groups["md_month"]), _day_number(groups["md_day"]))
    if groups["dm_month"]:
        return _date(_month_number(groups["dm_month"]), _day_number(groups["dm_day"]))
    if groups["clock"]:
        hour = int(groups["clock"]) % 12 + (12 if groups["meridiem"] == "p" else 0)
        return _time(hour, int(groups["clock_min"] or 0))
    if groups["hhmm"]:
        return _time(int(groups["hhmm"]), int(groups["hhmm_min"]))
    if groups["named"]:
        return frozenset({_NAMED_TIMES[groups["named"]]})
    amount = Decimal(groups["number"].replace(",", "")) * _SCALES.get(groups["scale"] or "", 1)
    return frozenset({f"n{format(amount.normalize(), 'f')}"})


def _with_bare_hours(tokens: list[Token]) -> list[Token]:
    """A plain 1-12 after "from"/"at"/... or starting a range ("8 to 12pm") may also be a time."""
    result = list(tokens)
    for index, token in enumerate(tokens):
        hour = _plain_hour(token)
        if hour is None:
            continue
        previous = tokens[index - 1] if index > 0 else frozenset()
        starts_range = tokens[index + 1 : index + 2] == [frozenset({"to"})] and index + 2 < len(tokens)
        if previous & _HOUR_CONTEXT or starts_range:
            result[index] = token | _time(hour % 12, 0) | _time(hour % 12 + 12, 0)
    return result


def _plain_hour(token: Token) -> int | None:
    if len(token) != 1:
        return None
    (reading,) = token
    if reading.startswith("n") and reading[1:].isdigit() and 1 <= int(reading[1:]) <= 12:
        return int(reading[1:])
    return None


def _date(month: int, day: int) -> Token:
    return frozenset({f"d{month:02d}-{day:02d}"}) if 1 <= month <= 12 and 1 <= day <= 31 else frozenset()


def _time(hour: int, minute: int) -> Token:
    return frozenset({f"t{hour:02d}:{minute:02d}"}) if hour < 24 and minute < 60 else frozenset()


def _month_number(name: str) -> int:
    return _MONTHS.index(name[:3]) + 1


def _day_number(day: str) -> int:
    return int(day.rstrip("stndrh"))
