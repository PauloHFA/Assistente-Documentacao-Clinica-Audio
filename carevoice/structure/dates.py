"""Deterministic pt-BR relative and explicit session date resolution (RF10)."""

import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_EXPLICIT_DMY = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_WEEKDAYS_PT: dict[str, int] = {
    "segunda": 0,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "domingo": 6,
}


def resolve_session_date(
    text: str,
    *,
    reference_now: datetime | None = None,
    timezone_name: str = "America/Fortaleza",
) -> date:
    """Resolve pt-BR date hints ('ontem', 'segunda', '12/11/2026') against local today."""
    zone = ZoneInfo(timezone_name)
    now_local = reference_now.astimezone(zone) if reference_now is not None else datetime.now(zone)
    today = now_local.date()

    folded = _fold(text)
    if "anteontem" in folded:
        return today - timedelta(days=2)
    if re.search(r"\bontem\b", folded):
        return today - timedelta(days=1)

    explicit = _EXPLICIT_DMY.search(folded)
    if explicit:
        day = int(explicit.group(1))
        month = int(explicit.group(2))
        year_raw = explicit.group(3)
        year = today.year if year_raw is None else _expand_year(int(year_raw))
        try:
            candidate = date(year, month, day)
        except ValueError:
            return today
        if year_raw is None and candidate > today:
            try:
                candidate = date(year - 1, month, day)
            except ValueError:
                return today
        return candidate

    for name, target_weekday in _WEEKDAYS_PT.items():
        if re.search(rf"\b{name}(?:-feira)?\b", folded):
            delta = (today.weekday() - target_weekday) % 7
            if delta == 0 and "passad" in folded:
                delta = 7
            return today - timedelta(days=delta)

    return today


def _expand_year(year: int) -> int:
    return 2000 + year if year < 100 else year


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
