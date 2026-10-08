"""Shared types and recipient-matching rules for messaging integrations."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from actions.web_driver import BrowserClosed, DriverError  # noqa: F401 - re-exported for integrations


class NotSignedIn(Exception):
    """The service is open but the user is not logged in. Never automate a login."""


class IntegrationError(Exception):
    """The page did not have the expected structure (UI changed, element missing)."""


_USERNAME = re.compile(r"^@?[a-z0-9._]{2,30}$")
_LABEL = re.compile(r"^(?P<name>.*?)\s*\(@(?P<user>[a-z0-9._]{2,30})\)\s*$", re.I)


def parse_recipient(text: str) -> tuple[str, str]:
    """("Yuliyan Ivanov (@yuliyan.ivanov_)" | "@yuliyan.ivanov_" | "Yuliyan Ivanov") → (name, username)."""
    text = " ".join(str(text).split())
    match = _LABEL.match(text)
    if match:
        return match["name"].strip(), match["user"].lower()
    if text.startswith("@") or (_USERNAME.match(text) and any(c in text for c in "._0123456789")):
        return "", text.lstrip("@").lower()
    return text, ""


@dataclass(frozen=True)
class Candidate:
    """One search-result row: its visible texts (display name, username, …)."""
    texts: tuple[str, ...]

    @property
    def username(self) -> str:
        users = [t.lstrip("@") for t in self.texts if _USERNAME.match(t)]
        if len(self.texts) < 2 or not users:
            return ""
        # Prefer the handle-looking text (dots, underscores, digits); else the second text.
        return next((u for u in users if any(c in u for c in "._0123456789")), users[-1]).lower()

    @property
    def name(self) -> str:
        user = self.username
        return next((t for t in self.texts if t.lstrip("@").lower() != user), self.texts[0] if self.texts else "")

    @property
    def label(self) -> str:
        """What the user hears and what send_message receives: "Name (@username)"."""
        return f"{self.name} (@{self.username})" if self.username else self.name


@dataclass
class ContactLookup:
    platform: str
    query: str
    exact: list[Candidate] = field(default_factory=list)
    partial: list[Candidate] = field(default_factory=list)

    @property
    def unique(self) -> Candidate | None:
        """The one contact that can be messaged without asking, or None."""
        # A single partial match is not enough: the user must agree it's the right person.
        return self.exact[0] if len(self.exact) == 1 else None

    @staticmethod
    def _labels(candidates: list[Candidate], limit: int = 8) -> str:
        shown = "; ".join(c.label for c in candidates[:limit])
        return shown + (f"; and {len(candidates) - limit} more" if len(candidates) > limit else "")

    def describe(self) -> str:
        if len(self.exact) == 1:
            return f"Found exactly one {self.platform} account matching: {self.exact[0].label}."
        if len(self.exact) > 1:
            return (f"{len(self.exact)} different {self.platform} accounts match '{self.query}': "
                    f"{self._labels(self.exact)}. Ask the user which one (read the usernames); do not guess.")
        if self.partial:
            return (f"No exact '{self.query}' on {self.platform}. Similar: {self._labels(self.partial)}. "
                    "Ask the user which one they mean.")
        return f"No {self.platform} account matching '{self.query}' was found."

    def as_dict(self) -> dict:
        return {"platform": self.platform, "query": self.query,
                "exact_matches": [c.label for c in self.exact[:8]],
                "similar_matches": [c.label for c in self.partial[:8]],
                "can_send_to": self.unique.label if self.unique else None, "summary": self.describe()}


def classify_rows(query: str, rows: list[list[str]]) -> tuple[list[Candidate], list[Candidate]]:
    """Exact and similar result rows for ``query`` (a name, a username, or a "Name (@username)" label)."""
    name, user = parse_recipient(query)
    exact, partial, seen = [], [], set()
    for texts in rows:
        candidate = Candidate(tuple(" ".join(str(t).split()) for t in texts if str(t).strip()))
        if not candidate.texts:
            continue
        if candidate.username:   # usernames are unique: the same account listed twice is one person
            if candidate.username in seen:
                continue
            seen.add(candidate.username)
        if user:
            hit = candidate.username == user or any(t.lstrip("@").lower() == user for t in candidate.texts)
            similar = bool(classify_names(user, list(candidate.texts))[1]) or (
                bool(name) and bool(sum(classify_names(name, list(candidate.texts)), [])))
        else:
            hit = bool(classify_names(name, list(candidate.texts))[0])
            similar = bool(classify_names(name, list(candidate.texts))[1])
        if hit:
            exact.append(candidate)
        elif similar:
            partial.append(candidate)
    return exact, partial


_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a", "ы": "y", "ь": "y",
    "э": "e", "ю": "yu", "я": "ya", "є": "ye", "і": "i", "ї": "yi", "ґ": "g",
}
# Spelling variants that are the same name ("Yuliyan"/"Yulian"/"Юлиян", "Hristo"/"Khristo").
_LOOSE = (("sht", "st"), ("iy", "i"), ("y", "i"), ("j", "i"), ("kh", "h"), ("ts", "c"), ("w", "v"))


def _fold(text: str) -> str:
    """Casefold, drop accents and transliterate Cyrillic to Latin."""
    text = unicodedata.normalize("NFKD", " ".join(str(text).split()).casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(_CYRILLIC.get(c, c) for c in text)


def _loose(text: str) -> str:
    text = re.sub(r"[\W_]+", "", _fold(text))   # also joins usernames: yuliyan.ivanov → yuliyanivanov
    for old, new in _LOOSE:
        text = text.replace(old, new)
    return text


def classify_names(query: str, names: list[str], limit: int = 8) -> tuple[list[str], list[str]]:
    """Split visible contact names (one per result row) into exact and similar matches.

    Exact means the same text ignoring case and spacing. Transliterations ("Yuliyan
    Ivanov" for "Юлиян Иванов"), usernames and spelling variants are only *similar*,
    so the user is asked before anything is sent. Identical names are deliberately
    kept: two rows both called "John" are two people.
    """
    wanted = " ".join(query.casefold().split())
    wanted_fold, wanted_loose = _fold(query), _loose(query)
    exact, partial = [], []
    for raw in names:
        name = " ".join(str(raw).split())
        if not name or not wanted:
            continue
        if name.casefold() == wanted:
            exact.append(name)
            continue
        folded, loose = _fold(name), _loose(name)
        if (loose == wanted_loose or wanted_fold in folded
                or all(part in folded for part in wanted_fold.split())
                or (len(wanted_loose) >= 4 and wanted_loose in loose)):
            partial.append(name)
    return exact[:limit], partial[:limit]


@dataclass
class SendResult:
    sent: bool
    verified: bool
    message: str

    def __str__(self) -> str:
        return self.message
