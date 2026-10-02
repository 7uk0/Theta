"""Tests for multiplayer player-empire selection.

A Stellaris multiplayer save's gamestate has a `player` block with one entry
per human player, each ``{ name = "<player>", country = <country_id> }``. The
old logic always took the first entry (usually the host), so multiplayer saves
were analyzed as the host's empire. These tests cover the selection logic that
picks the right empire from an explicit override, the local player name, or a
logged first-entry fallback.
"""

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from augur.extractor.base import SaveExtractorBase
from augur.extractor.player import (
    PlayerMixin,
    _normalize_player_entries,
    _select_player_country_id,
)
from augur.parser import rust_bridge

# Minimal multi-entry player block, mirroring the real MP autosave:
#   country 0 = "Will-O-Matic", country 1 = "Flame"
MP_PLAYER_BLOCK = [
    {"country": "0", "name": "Will-O-Matic"},
    {"country": "1", "name": "Flame"},
]

SP_PLAYER_BLOCK = [{"country": "0", "name": "Will-O-Matic"}]


def test_normalize_coerces_country_ids_to_int_and_keeps_names():
    entries = _normalize_player_entries(MP_PLAYER_BLOCK)
    assert entries == [
        {"country": 0, "name": "Will-O-Matic"},
        {"country": 1, "name": "Flame"},
    ]


def test_normalize_handles_dict_shaped_player_block():
    # Some parses can surface the block as a dict keyed by index.
    block = {"0": {"country": "0", "name": "Host"}, "1": {"country": "5", "name": "Guest"}}
    entries = _normalize_player_entries(block)
    assert {"country": 0, "name": "Host"} in entries
    assert {"country": 5, "name": "Guest"} in entries


def test_normalize_skips_malformed_entries():
    block = [{"name": "no country"}, "junk", {"country": "2", "name": "ok"}]
    assert _normalize_player_entries(block) == [{"country": 2, "name": "ok"}]


def test_single_player_returns_first_without_fallback_warning():
    entries = _normalize_player_entries(SP_PLAYER_BLOCK)
    country_id, info = _select_player_country_id(entries)
    assert country_id == 0
    assert info["method"] == "single"


def test_name_override_selects_matching_empire():
    entries = _normalize_player_entries(MP_PLAYER_BLOCK)
    country_id, info = _select_player_country_id(entries, override_name="Will-O-Matic")
    assert country_id == 0
    assert info["method"] == "name_override"

    country_id, info = _select_player_country_id(entries, override_name="Flame")
    assert country_id == 1
    assert info["method"] == "name_override"


def test_name_override_is_case_and_whitespace_insensitive():
    entries = _normalize_player_entries(MP_PLAYER_BLOCK)
    country_id, info = _select_player_country_id(entries, override_name="  will-o-matic ")
    assert country_id == 0
    assert info["method"] == "name_override"


def test_country_id_override_takes_priority_over_name():
    entries = _normalize_player_entries(MP_PLAYER_BLOCK)
    country_id, info = _select_player_country_id(
        entries, override_country_id=1, override_name="Will-O-Matic"
    )
    assert country_id == 1
    assert info["method"] == "country_id_override"


def test_name_override_no_match_falls_back_to_first_entry():
    entries = _normalize_player_entries(MP_PLAYER_BLOCK)
    country_id, info = _select_player_country_id(entries, override_name="Nonexistent")
    assert country_id == 0
    assert info["method"] == "first_fallback"


def test_multiplayer_no_override_falls_back_to_first_with_candidates_listed():
    entries = _normalize_player_entries(MP_PLAYER_BLOCK)
    country_id, info = _select_player_country_id(entries)
    assert country_id == 0
    assert info["method"] == "first_fallback"
    # Fallback must expose every candidate so a warning can list them.
    assert info["candidates"] == entries


def test_empty_player_block_returns_zero():
    country_id, info = _select_player_country_id([])
    assert country_id == 0


# ---------------------------------------------------------------------------
# Integration: exercise the real get_player_empire_id / get_player_empire_name
# methods (env override + name threading) against an injected fake session, so
# the wiring is regression-protected without needing a real multiplayer save.
# ---------------------------------------------------------------------------

# Custom empire names are stored as literal name blocks in the save.
COUNTRY_ENTRIES = {
    "0": {"name": {"key": "Great Coffee Nation", "literal": "yes"}},
    "1": {"name": {"key": "Omnivorous Cevantian Swarm", "literal": "yes"}},
}


class _FakeSession:
    def __init__(self, players, countries):
        self._players = players
        self._countries = countries
        self.extract_calls = 0

    def extract_sections(self, sections):
        if "player" in sections:
            self.extract_calls += 1
            return {"player": self._players}
        return {}

    def get_entry(self, section, key):
        if section == "country":
            return self._countries.get(str(key))
        return None


class _FakeExtractor(PlayerMixin, SaveExtractorBase):
    """Minimal extractor that skips file I/O but keeps real selection logic."""

    def __init__(self, meta_name="mp_host_save"):
        self._meta_name = meta_name
        self._player_entries_cache = None
        self._player_empire_id_cache = None
        self._player_country_entry_cache = None

    def get_metadata(self):  # override: avoid reading a real save's meta zip
        return {"name": self._meta_name}


@contextlib.contextmanager
def _injected_session(session):
    prev = getattr(rust_bridge._tls, "session", None)
    rust_bridge._tls.session = session
    try:
        yield
    finally:
        rust_bridge._tls.session = prev


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv("STELLARIS_PLAYER_NAME", raising=False)
    monkeypatch.delenv("STELLARIS_PLAYER_COUNTRY_ID", raising=False)


def test_name_override_threads_selected_empire_name(monkeypatch):
    monkeypatch.setenv("STELLARIS_PLAYER_NAME", "Will-O-Matic")
    session = _FakeSession(
        [{"country": "0", "name": "Host"}, {"country": "1", "name": "Will-O-Matic"}],
        COUNTRY_ENTRIES,
    )
    with _injected_session(session):
        ext = _FakeExtractor()
        assert ext.get_player_empire_id() == 1
        name = ext.get_player_empire_name()
        # Name comes from the selected country's block, not the meta header.
        assert name == ext._resolve_country_empire_name(1)
        assert name != ext.get_metadata()["name"]


def test_country_id_override_threads_selected_empire(monkeypatch):
    monkeypatch.setenv("STELLARIS_PLAYER_COUNTRY_ID", "1")
    session = _FakeSession(
        [{"country": "0", "name": "Host"}, {"country": "1", "name": "Guest"}],
        COUNTRY_ENTRIES,
    )
    with _injected_session(session):
        ext = _FakeExtractor()
        assert ext.get_player_empire_id() == 1


def test_multiplayer_fallback_still_returns_first_and_caches(monkeypatch):
    session = _FakeSession(
        [{"country": "0", "name": "Host"}, {"country": "1", "name": "Guest"}],
        COUNTRY_ENTRIES,
    )
    with _injected_session(session):
        ext = _FakeExtractor()
        assert ext.get_player_empire_id() == 0
        # Repeated calls must not re-query the session.
        ext.get_player_empire_id()
        ext.get_player_empire_id()
        assert session.extract_calls == 1


def test_single_player_name_falls_back_to_meta_name():
    session = _FakeSession([{"country": "0", "name": "SoloPlayer"}], COUNTRY_ENTRIES)
    with _injected_session(session):
        ext = _FakeExtractor(meta_name="United Nations of Earth")
        assert ext.is_multiplayer_save() is False
        # Single-player keeps the existing meta-derived name (unchanged behavior).
        assert ext.get_player_empire_name() == "United Nations of Earth"
