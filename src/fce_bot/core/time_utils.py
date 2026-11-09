"""Time and timezone utility helpers for CircumOrari Bot."""

import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROME_TZ = ZoneInfo("Europe/Rome")

WORD_TO_MINUTES = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "due": 2,
    "tre": 3,
    "quattro": 4,
    "cinque": 5,
    "sei": 6,
    "sette": 7,
    "otto": 8,
    "nove": 9,
    "dieci": 10,
    "quindici": 15,
    "venti": 20,
    "venticinque": 25,
    "trenta": 30,
    "quarantacinque": 45,
    "cinquanta": 50,
}

WORD_TO_HOURS = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "due": 2,
    "tre": 3,
    "quattro": 4,
}

# Regex pattern for relative/asap/time expressions to be recognized as delimiters
TIME_DELIMITERS_PATTERN = (
    r"(?:il\s+prima\s+possibile|al\s+pi[uù]\s+presto|il\s+pi[uù]\s+presto|subito|adesso|ora|"
    r"in\s+questo\s+momento|immediatamente|a\s+quest[\x27\s]?ora|d[\x27\s]?ora|ad\s+ora|"
    r"(?:tra|fra|in)\s+(?:\d+|[a-z]+)\s+minut[io]|"
    r"(?:tra|fra|in)\s+(?:mezz[\x27\s]?ora|mezza\s+ora)|"
    r"(?:tra|fra|in)\s+(?:un\s+quarto\s+d[\x27\s]?ora|15\s+minuti)|"
    r"(?:tra|fra|in)\s+(?:tre\s+quarti\s+d[\x27\s]?ora|45\s+minuti)|"
    r"(?:tra|fra|in)\s+(?:1|un[\x27\s]?ora)(?:\s+e\s+mezz[ao])?|"
    r"(?:tra|fra|in)\s+(?:\d+|due|tre|quattro)\s+ore|"
    r"alle|verso\s+le|verso|entro\s+le|entro|per\s+le|prima\s+delle|dopo\s+le|dopo|intorno\s+alle|ore|"
    r"domani|oggi|stasera|stamattina|stanotte|"
    r"in\s+treno|in\s+metro|in\s+metropolitana|in\s+bus|con\s+il\s+treno|con\s+la\s+metro|con\s+il\s+bus|che)"
)


def get_local_now() -> datetime:
    """Return the current local datetime in Europe/Rome timezone."""
    try:
        return datetime.now(ROME_TZ)
    except Exception:
        return datetime.now()


def get_current_time_str(now: Optional[datetime] = None) -> str:
    """Return current local time as 'HH:MM' string."""
    n = now or get_local_now()
    return n.strftime("%H:%M")


def extract_time_from_text(text: str, now: Optional[datetime] = None) -> Tuple[Optional[str], bool]:
    """Parse relative, asap, or explicit time expressions from natural language text.

    Returns:
        (extracted_time_str, has_explicit_time)
        where extracted_time_str is 'HH:MM' if any time expression was detected, or None.
        has_explicit_time is True for explicit clock times (e.g. '14:30', 'alle 9'),
        and False for relative/asap expressions (e.g. 'tra 5 minuti', 'il prima possibile').
    """
    if not text:
        return None, False

    ref_now = now or get_local_now()
    t_low = text.lower()

    # 1. Relative offset expressions (tra / fra / in ...)
    if re.search(r"\b(?:tra|fra|in)\s+(?:mezz[\x27\s]?ora|mezza\s+ora)\b", t_low):
        return (ref_now + timedelta(minutes=30)).strftime("%H:%M"), False

    if re.search(r"\b(?:tra|fra|in)\s+un\s+quarto\s+d[\x27\s]?ora\b", t_low):
        return (ref_now + timedelta(minutes=15)).strftime("%H:%M"), False

    if re.search(r"\b(?:tra|fra|in)\s+tre\s+quarti\s+d[\x27\s]?ora\b", t_low):
        return (ref_now + timedelta(minutes=45)).strftime("%H:%M"), False

    if re.search(r"\b(?:tra|fra|in)\s+(?:1|un[\x27\s]?ora)\s+e\s+mezz[ao]\b", t_low):
        return (ref_now + timedelta(minutes=90)).strftime("%H:%M"), False

    if re.search(r"\b(?:tra|fra|in)\s+(?:1|un[\x27\s]?ora)\b", t_low):
        return (ref_now + timedelta(minutes=60)).strftime("%H:%M"), False

    m_hours = re.search(r"\b(?:tra|fra|in)\s+(\d+|due|tre|quattro)\s+ore\b", t_low)
    if m_hours:
        h_str = m_hours.group(1)
        h_val = int(h_str) if h_str.isdigit() else WORD_TO_HOURS.get(h_str, 1)
        return (ref_now + timedelta(hours=h_val)).strftime("%H:%M"), False

    m_min = re.search(r"\b(?:tra|fra|in)\s+(\d+|[a-z]+)\s+minut[io]\b", t_low)
    if m_min:
        val_str = m_min.group(1)
        mins = int(val_str) if val_str.isdigit() else WORD_TO_MINUTES.get(val_str, 0)
        if mins > 0:
            return (ref_now + timedelta(minutes=mins)).strftime("%H:%M"), False

    # 2. Immediate / ASAP expressions
    if re.search(
        r"\b(?:il\s+prima\s+possibile|al\s+pi[uù]\s+presto|il\s+pi[uù]\s+presto|subito|adesso|ora|"
        r"in\s+questo\s+momento|immediatamente|a\s+quest[\x27\s]?ora|d[\x27\s]?ora|ad\s+ora)\b",
        t_low,
    ):
        return ref_now.strftime("%H:%M"), False

    # 3. Explicit HH:MM or HH.MM
    time_match = re.search(
        r"\b(?:alle\s+|verso\s+le\s+|entro\s+le\s+|per\s+le\s+|prima\s+delle\s+|dopo\s+le\s+|intorno\s+alle\s+|ore\s+)?([0-1]?[0-9]|2[0-3])[:.]([0-5][0-9])\b",
        text,
        re.IGNORECASE,
    )
    if time_match:
        h, m = time_match.group(1), time_match.group(2)
        return f"{int(h):02d}:{int(m):02d}", True

    # 4. Explicit hour only (e.g. alle 9, per le 15, entro le 8)
    hour_match = re.search(
        r"\b(?:alle\s+|verso\s+le\s+|entro\s+le\s+|per\s+le\s+|prima\s+delle\s+|dopo\s+le\s+|intorno\s+alle\s+|ore\s+)([0-1]?[0-9]|2[0-3])\b",
        text,
        re.IGNORECASE,
    )
    if hour_match:
        h = hour_match.group(1)
        return f"{int(h):02d}:00", True

    # 5. Mezzogiorno / Mezzanotte
    if re.search(r"\b(?:a\s+)?mezzogiorno\b", t_low):
        return "12:00", True
    if re.search(r"\b(?:a\s+)?mezzanotte\b", t_low):
        return "00:00", True

    return None, False


def resolve_query_time(text: str, now: Optional[datetime] = None) -> Tuple[str, bool]:
    """Resolve target time for a transit query.

    If any explicit or relative time is found in text, returns that time.
    Otherwise, defaults to the current 'now' local time string ('HH:MM').
    Returns:
        (time_str, has_explicit_time)
    """
    ref_now = now or get_local_now()
    extracted, is_explicit = extract_time_from_text(text, ref_now)
    if extracted:
        return extracted, is_explicit
    return ref_now.strftime("%H:%M"), False
