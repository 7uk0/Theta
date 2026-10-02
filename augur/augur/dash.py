"""The orrery: a terminal dashboard over the campaign archive.

This is what the cut Electron app was for — seeing campaign state change
without asking a question — rebuilt as a curses TUI with no new dependencies.
Run it in one pane and Claude Code in another: the dashboard shows you *that*
something changed, the conversation tells you what to do about it.

It optionally runs the vigil itself, so one process watches the save folder,
ingests each autosave, and redraws. Nothing here reaches past
``CampaignContext`` and the snapshot table, so it stays honest about the same
data a briefing would give you.

Layout is built from pure functions (``build_state``, ``sparkline``,
``collect_alerts``) with curses confined to ``_draw``, so the interesting parts
are unit-testable without a terminal.
"""

from __future__ import annotations

import curses
import logging
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from augur.context import CampaignContext, ContextError
from augur.core.database import GameDatabase

REFRESH_SECONDS = 1.0
TREND_POINTS = 12
MAX_LOG_LINES = 50
MAX_EVENTS = 60

# Below this the masthead, alert strip and column headings leave no room for an
# actual entry, so the dashboard says what it needs instead of drawing a husk.
MIN_ROWS = 11
# The two-column block needs this much before the resource values start getting
# clipped. 80 is the universal default terminal width, so this costs nothing in
# practice; a narrower pane gets told rather than shown a mangled frame.
MIN_COLS = 72

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
SPARK_ASCII = "_.-~=+*#"

# Resources worth a row of their own, in the order a player triages them.
# Keys that appear alongside the "<name>_net" fields but are totals held, not
# income. `minor_artifacts` is the one the extractor actually publishes this way
# (see augur/extractor/economy.py), and `resources_in_deficit` is a count.
NON_NET_KEYS = frozenset({"minor_artifacts", "resources_in_deficit"})

# The accent is drawn from the empire's own ethos, the way the advisor's voice
# is. A militarist dominion should not read like a materialist research state,
# and a machine intelligence should feel colder than either.
ETHOS_ACCENT = {
    "militarist": 203,
    "pacifist": 114,
    "xenophile": 44,
    "xenophobe": 208,
    "materialist": 45,
    "spiritualist": 141,
    "egalitarian": 75,
    "authoritarian": 178,
}
GESTALT_ACCENT = {"machine": 51, "hive": 107}
DEFAULT_ACCENT = 45  # Stellaris interface cyan

PAIR_ACCENT, PAIR_WARN, PAIR_ALERT, PAIR_GOOD, PAIR_DIM = 1, 2, 3, 4, 5
BAR = "▌"
ALERT_MARK = "▲"
STATUS_MARK = "◆"


# Rows the situation log needs at minimum: its own heading plus one entry.
LOG_MIN_ROWS = 2


def budget_columns(*, available: int, longest_column: int) -> int:
    """How many rows the EMPIRE / RESOURCE LEDGER columns may use.

    `available` is the space between the column headings and the footer. The log
    is always left room, so on a short terminal the fact list is truncated rather
    than crowding out what changed.
    """
    if available <= 0 or longest_column <= 0:
        return 0
    # +1 for the blank row between the columns and whatever pane follows them.
    return max(1, min(longest_column, available - LOG_MIN_ROWS - 1))


def budget_panes(*, available: int, trend_count: int, event_count: int) -> int:
    """How many trend rows may be drawn, given the height left for both panes.

    The situation log is the more informative pane, so it is served first and
    trends are trimmed to fit. Returning 0 means the trajectory pane is dropped
    entirely rather than pushing the log off the bottom of the screen.

    `available` counts the rows between the two-column block and the footer.
    Each pane costs one row for its own heading.
    """
    if available <= 0 or trend_count <= 0:
        return 0
    log_need = min(event_count + 1, 5) if event_count else LOG_MIN_ROWS
    # -2: the trajectory pane costs a heading row and the blank row that
    # separates it from the log. Charging only for the heading let the spacer
    # eat into the log's reservation, leaving it a heading and no entries.
    room = available - log_need - 2
    if room < 1:
        return 0
    return min(trend_count, room)




def accent_for(identity: dict[str, Any]) -> int:
    """Pick the 256-colour accent an empire's ethos earns."""
    if not isinstance(identity, dict):
        return DEFAULT_ACCENT
    if identity.get("is_machine"):
        return GESTALT_ACCENT["machine"]
    if identity.get("is_hive_mind"):
        return GESTALT_ACCENT["hive"]
    ethics = identity.get("ethics") if isinstance(identity.get("ethics"), list) else []
    names = [str(e).lower().replace("ethic_", "") for e in ethics]
    # A fanatic ethic defines the empire more than its secondary does.
    for name in sorted(names, key=lambda n: 0 if n.startswith("fanatic") else 1):
        for key, colour in ETHOS_ACCENT.items():
            if key in name:
                return colour
    return DEFAULT_ACCENT

# Resources worth a row of their own, in the order a player triages them.
TRACKED_RESOURCES = (
    "energy",
    "minerals",
    "food",
    "alloys",
    "consumer_goods",
    "influence",
    "unity",
    "research_total",
)


@dataclass
class Trend:
    label: str
    values: list[float]
    current: float | None = None

    @property
    def delta(self) -> float | None:
        if len(self.values) < 2:
            return None
        return self.values[-1] - self.values[0]

    @property
    def pct(self) -> float | None:
        if len(self.values) < 2 or not self.values[0]:
            return None
        return (self.values[-1] - self.values[0]) / abs(self.values[0])


@dataclass
class DashState:
    loaded: bool = False
    message: str | None = None
    empire: str = "—"
    game_date: str = "—"
    version: str = "—"
    freshness: str = ""
    snapshot_count: int = 0
    first_date: str | None = None
    facts: list[tuple[str, str]] = field(default_factory=list)
    stockpiles: list[tuple[str, str, str]] = field(default_factory=list)
    trends: list[Trend] = field(default_factory=list)
    events: list[tuple[str, str, str]] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    subtitle: str = ""
    accent: int = DEFAULT_ACCENT


# ------------------------------------------------------------------ formatting


def fmt_number(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number) and abs(number) < 1e15:
        return f"{int(number):,}"
    return f"{number:,.1f}"


def fmt_signed(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{'+' if number >= 0 else ''}{fmt_number(number)}"


def label_of(key: str) -> str:
    return str(key).replace("_", " ").title()


def sparkline(values: list[float], *, unicode_ok: bool = True) -> str:
    """A one-line shape of a series. Flat series render flat, not as noise."""
    numbers = [v for v in values if isinstance(v, (int, float))]
    if len(numbers) < 2:
        return ""
    ramp = SPARK_BLOCKS if unicode_ok else SPARK_ASCII
    low, high = min(numbers), max(numbers)
    if high == low:
        return ramp[len(ramp) // 2] * len(numbers)
    span = high - low
    return "".join(ramp[min(len(ramp) - 1, int((v - low) / span * len(ramp)))] for v in numbers)


def arrow(trend: Trend, *, unicode_ok: bool = True) -> str:
    delta = trend.delta
    if delta is None or delta == 0:
        return "→" if unicode_ok else "="
    if delta > 0:
        return "↑" if unicode_ok else "^"
    return "↓" if unicode_ok else "v"


# ----------------------------------------------------------------- state build


def unwrap_sections(briefing: dict[str, Any]) -> dict[str, Any]:
    """Return the section map whether it is nested or flat.

    ``get_empire_briefing`` wraps its payload as ``{campaign, detail, sections}``
    while ``get_strategy_context`` returns the sections at the top level. Accept
    either so the dashboard does not care which call it was handed.
    """
    if not isinstance(briefing, dict):
        return {}
    sections = briefing.get("sections")
    if isinstance(sections, dict):
        return sections
    return briefing


def extract_nets(sections: dict[str, Any]) -> dict[str, float]:
    """Monthly net income per resource, from whichever place it is published.

    Three shapes exist in the wild: ``economy.net_monthly`` (keys bare),
    ``economy.resources.summary`` (keys suffixed ``_net``), and
    ``situation.economy`` (also suffixed). Merge them, preferring the first
    that carries a given resource.
    """
    economy = sections.get("economy") if isinstance(sections.get("economy"), dict) else {}
    situation = sections.get("situation") if isinstance(sections.get("situation"), dict) else {}
    resources = economy.get("resources") if isinstance(economy.get("resources"), dict) else {}

    candidates: list[dict[str, Any]] = []
    if isinstance(economy.get("net_monthly"), dict):
        candidates.append(economy["net_monthly"])
    if isinstance(resources.get("summary"), dict):
        candidates.append(resources["summary"])
    if isinstance(situation.get("economy"), dict):
        candidates.append(situation["economy"])

    nets: dict[str, float] = {}
    for source in candidates:
        for key, value in source.items():
            if key.startswith("_") or not isinstance(value, (int, float)):
                continue
            if key in NON_NET_KEYS:
                # resources.summary mixes a few stockpile totals in among the
                # "<name>_net" fields; counting those as income reads as a huge
                # monthly surplus that is really just the amount held.
                continue
            name = key[:-4] if key.endswith("_net") else key
            nets.setdefault(name, float(value))
    return nets


def collect_alerts(briefing: dict[str, Any], trends: list[Trend]) -> list[str]:
    """The handful of things a player wants shouted, not buried in a briefing."""
    alerts: list[str] = []
    sections = unwrap_sections(briefing)
    situation = sections.get("situation") if isinstance(sections.get("situation"), dict) else {}

    if situation.get("at_war"):
        count = situation.get("war_count")
        alerts.append(f"AT WAR ({fmt_number(count)})" if count else "AT WAR")
    if situation.get("crisis_active"):
        alerts.append("CRISIS ACTIVE")

    deficits = [
        label_of(name)
        for name, value in extract_nets(sections).items()
        if value < 0 and name not in {"resources_in_deficit"}
    ]
    if deficits:
        alerts.append("DEFICIT: " + ", ".join(sorted(deficits)[:4]))

    # A military drop between snapshots usually means a fleet died.
    for trend in trends:
        if trend.label == "Military power":
            pct = trend.pct
            if pct is not None and pct <= -0.1:
                alerts.append(f"MILITARY DOWN {pct * 100:.0f}%")
    return alerts


def build_state(*, context: CampaignContext, db: GameDatabase) -> DashState:
    """Assemble everything the dashboard draws. Never raises on empty data."""
    try:
        campaign = context.get_active_campaign()
    except ContextError as exc:
        return DashState(loaded=False, message=str(exc))

    if not campaign.get("save_loaded"):
        return DashState(
            loaded=False,
            message=campaign.get("message") or "No campaign ingested yet. Run: augur ingest",
        )

    state = DashState(
        loaded=True,
        empire=str(campaign.get("empire_name") or "—"),
        game_date=str(campaign.get("game_date") or "—"),
        version=str(campaign.get("version") or "—"),
        snapshot_count=int(campaign.get("snapshot_count") or 0),
        first_date=campaign.get("first_game_date"),
    )
    freshness = campaign.get("freshness")
    if isinstance(freshness, dict):
        state.freshness = str(freshness.get("state") or "")

    try:
        briefing = context.get_empire_briefing(max_detail="compact")
    except ContextError:
        briefing = {}
    if not isinstance(briefing, dict):
        briefing = {}

    sections = unwrap_sections(briefing)

    def section(name: str) -> dict[str, Any]:
        value = sections.get(name)
        return value if isinstance(value, dict) else {}

    situation = section("situation")
    military = section("military")
    economy = section("economy")
    territory = section("territory")
    technology = section("technology")
    colonies = territory.get("colonies") if isinstance(territory.get("colonies"), dict) else {}
    research = technology.get("research") if isinstance(technology.get("research"), dict) else {}

    state.facts = [
        ("Phase", label_of(str(situation.get("game_phase") or "—"))),
        ("At war", fmt_number(situation.get("at_war"))),
        ("Colonies", fmt_number(colonies.get("total_count"))),
        ("Pops", fmt_number(colonies.get("total_population"))),
        ("Fleet power", fmt_number(military.get("military_power"))),
        ("Fleets", fmt_number(military.get("military_fleets") or military.get("fleet_count"))),
        (
            "Techs",
            fmt_number(technology.get("tech_count") or technology.get("researched_count")),
        ),
        (
            "Research",
            fmt_number(
                sum(v for v in research.values() if isinstance(v, (int, float))) or None
            ),
        ),
        ("Contacts", fmt_number(situation.get("contact_count"))),
    ]

    resources = economy.get("resources") if isinstance(economy.get("resources"), dict) else {}
    stock = resources.get("stockpiles") if isinstance(resources.get("stockpiles"), dict) else {}
    nets = extract_nets(sections)
    for name in TRACKED_RESOURCES:
        held = stock.get(name)
        net = nets.get(name)
        # A resource with nothing stockpiled and a flat net tells you nothing.
        if held is None and not net:
            continue
        state.stockpiles.append((label_of(name), fmt_number(held), fmt_signed(net)))
    # Anything the save tracks that is not in the curated list still matters if
    # it is being held; show it after the staples rather than hiding it.
    for name, held in stock.items():
        if name not in TRACKED_RESOURCES and isinstance(held, (int, float)) and held:
            state.stockpiles.append((label_of(name), fmt_number(held), fmt_signed(nets.get(name))))

    identity = section("identity") or _identity_from_archive(db)
    state.accent = accent_for(identity)
    state.subtitle = build_subtitle(identity)

    state.trends = build_trends(db=db, session_id=_current_session_id(context))
    state.alerts = collect_alerts(briefing, state.trends)

    try:
        events = context.get_recent_events(limit=MAX_EVENTS).get("events") or []
    except ContextError:
        events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        state.events.append(
            (
                str(event.get("game_date") or "?"),
                str(event.get("event_type") or "event"),
                str(event.get("summary") or ""),
            )
        )
    return state


def build_subtitle(identity: dict[str, Any]) -> str:
    """AUTHORITY · ETHIC · ETHIC — the empire's character, in its own terms."""
    if not isinstance(identity, dict):
        return ""
    parts: list[str] = []
    if identity.get("is_machine"):
        parts.append("MACHINE INTELLIGENCE")
    elif identity.get("is_hive_mind"):
        parts.append("HIVE MIND")
    elif identity.get("authority"):
        parts.append(str(identity["authority"]).replace("auth_", "").replace("_", " ").upper())
    ethics = identity.get("ethics") if isinstance(identity.get("ethics"), list) else []
    for ethic in ethics[:3]:
        parts.append(str(ethic).replace("ethic_", "").replace("_", " ").upper())
    return " · ".join(parts)


def _identity_from_archive(db: GameDatabase) -> dict[str, Any]:
    """Identity is not in a compact briefing; read it from the stored one."""
    import json

    try:
        row = db.execute("SELECT latest_briefing_json FROM sessions LIMIT 1;").fetchone()
    except Exception:  # noqa: BLE001
        return {}
    if not row or not row[0]:
        return {}
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    identity = payload.get("identity") if isinstance(payload, dict) else None
    return identity if isinstance(identity, dict) else {}


def _current_session_id(context: CampaignContext) -> str | None:
    """The session the rest of the dashboard is describing.

    `db.get_sessions(limit=1)` orders by `started_at` and does not filter trashed
    playthroughs, so it can name a different campaign than
    `CampaignContext._get_current_session`. Using it for the trends pane rendered
    an abandoned campaign's history under the active campaign's header — and fed
    the wrong series to the MILITARY DOWN alert. Ask the context instead.
    """
    try:
        return context.current_session_id()
    except Exception:  # noqa: BLE001 - a dashboard must not die on a read
        return None


def build_trends(*, db: GameDatabase, session_id: str | None) -> list[Trend]:
    """Metric series across snapshots, oldest first."""
    if not session_id:
        return []
    try:
        points = db.get_recent_snapshot_points(session_id=session_id, limit=TREND_POINTS)
    except Exception:  # noqa: BLE001
        return []
    points = list(reversed(points))  # the query returns newest first
    if not points:
        return []

    specs = (
        ("Military power", "military_power"),
        ("Colonies", "colony_count"),
        ("Energy net", "energy_net"),
        ("Alloys net", "alloys_net"),
        ("Wars", "wars_count"),
    )
    trends: list[Trend] = []
    for label, column in specs:
        values = [
            float(p[column])
            for p in points
            if isinstance(p.get(column), (int, float))
        ]
        if not values:
            continue
        trends.append(Trend(label=label, values=values, current=values[-1]))
    return trends


# --------------------------------------------------------------------- curses


class _SinkHandler(logging.Handler):
    """Routes log records into the dashboard's own log pane."""

    def __init__(self, sink) -> None:
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(record.getMessage())
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


class Dashboard:
    def __init__(
        self,
        *,
        db: GameDatabase,
        language: str = "en",
        watch: bool = True,
        watch_paths: list[Path] | None = None,
    ) -> None:
        self._db = db
        self._language = language
        self._watch = watch
        self._watch_paths = watch_paths

        self._state = DashState()
        self._log: list[str] = []
        self._lock = threading.Lock()
        self._dirty = threading.Event()
        self._event_scroll = 0
        self._unicode_ok = True
        self._vigil = None
        self._vigil_thread: threading.Thread | None = None
        self._ingest_threads: list[threading.Thread] = []
        self._status = "starting"

    # -- plumbing

    def log(self, message: str) -> None:
        with self._lock:
            for line in str(message).split("\n"):
                self._log.append(f"{time.strftime('%H:%M:%S')} {line}")
            del self._log[:-MAX_LOG_LINES]
        self._dirty.set()

    def refresh_state(self) -> None:
        context = CampaignContext(db=self._db, language=self._language)
        try:
            state = build_state(context=context, db=self._db)
        except Exception as exc:  # noqa: BLE001 - never let a read kill the UI
            state = DashState(loaded=False, message=f"Could not read the archive: {exc}")
        finally:
            context.close()
        with self._lock:
            self._state = state
        self._dirty.set()

    def _start_vigil(self) -> None:
        from augur.watch import Vigil

        self._vigil = Vigil(
            db=self._db,
            watch_paths=self._watch_paths,
            on_ingest=lambda _r: self.refresh_state(),
            log=self.log,
        )
        self._vigil_thread = threading.Thread(
            target=self._vigil.run, kwargs={"ingest_existing": True}, daemon=True
        )
        self._vigil_thread.start()
        self._status = "vigil starting"

    def _ingest_now(self) -> None:
        """Manual 'r' — ingest in a thread so the UI keeps drawing."""
        # Drop threads that have finished, so holding 'r' cannot grow the list
        # without bound.
        self._ingest_threads = [t for t in self._ingest_threads if t.is_alive()]
        if self._ingest_threads:
            self.log("a re-read is already running")
            return

        def work() -> None:
            from augur.ingest import IngestError, ingest_save

            self.log("re-reading newest save…")
            try:
                # Honour the watched directory: ingest_save(None) searches only
                # the platform defaults, which may hold a different campaign.
                result = ingest_save(db=self._db, save_path=self._explicit_save_dir())
            except IngestError as exc:
                self.log(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self.log(f"ingest failed: {exc}")
                return
            self.log(
                f"{result.game_date or '?'} "
                f"({'new snapshot' if result.inserted else 'refreshed'})"
            )
            self.refresh_state()

        thread = threading.Thread(target=work, daemon=True, name="augur-reread")
        self._ingest_threads.append(thread)
        thread.start()

    def _explicit_save_dir(self) -> Path | None:
        """The directory the user told us to watch, if they named one."""
        if self._watch_paths:
            return self._watch_paths[0]
        return None

    # -- entry point

    def run(self) -> int:
        self.refresh_state()
        if self._watch:
            self._start_vigil()
        else:
            self._status = "watch off"
        with self._captured_logging():
            try:
                curses.wrapper(self._loop)
            finally:
                self.shutdown()
        return 0

    @contextmanager
    def _captured_logging(self):
        """Keep stderr clear while curses owns the terminal.

        Anything that reaches the root logger is written to stderr by the
        lastResort handler, which lands a traceback on top of the display and
        leaves the screen unreadable. Attach our own handler for the duration and
        put the records in the footer instead.
        """
        root = logging.getLogger()
        handler = _SinkHandler(self.log)
        previous_handlers = list(root.handlers)
        previous_level = root.level
        root.handlers = [handler]
        root.setLevel(logging.WARNING)
        try:
            yield
        finally:
            root.handlers = previous_handlers
            root.setLevel(previous_level)

    def shutdown(self, *, timeout: float = 30.0) -> None:
        """Stop writers and wait for them, before the database is closed.

        Setting the stop flag is not enough: an ingest already inside
        `record_snapshot_from_briefing` keeps using the connection, and closing
        it underneath raises `ProgrammingError` deep in the write — losing the
        autosave, or leaving a snapshot row with no events. So join every writer,
        then drain whatever shutdown raced.
        """
        deadline = time.time() + timeout
        if self._vigil is not None:
            self._vigil.stop()
        if self._vigil_thread is not None and self._vigil_thread.is_alive():
            self._vigil_thread.join(timeout=max(0.0, deadline - time.time()))
        for thread in self._ingest_threads:
            if thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.time()))
        self._ingest_threads = []
        if self._vigil is not None:
            # A save queued when the stop arrived is still unrecorded; the
            # database is open for a moment longer, so record it now.
            try:
                self._vigil.drain_pending()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                # curses is already torn down here, so stderr is the terminal again.
                print(f"augur: could not record the queued save: {exc}", file=sys.stderr)

    def _loop(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        with self._lock:
            accent = self._state.accent
        self._init_colors(accent)
        self._themed_accent = accent
        self._unicode_ok = self._probe_unicode(stdscr)

        last_poll = 0.0
        last_fingerprint = self._db_fingerprint()

        while True:
            now = time.time()
            if now - last_poll >= REFRESH_SECONDS:
                last_poll = now
                # Another process (a standalone vigil, or an ingest) may have
                # written to the archive; pick that up without being told.
                fingerprint = self._db_fingerprint()
                if fingerprint != last_fingerprint:
                    last_fingerprint = fingerprint
                    self.refresh_state()
                self._dirty.set()

            if self._dirty.is_set():
                self._dirty.clear()
                with self._lock:
                    accent = self._state.accent
                if accent != getattr(self, "_themed_accent", None):
                    self._init_colors(accent)
                    self._themed_accent = accent
                self._draw(stdscr)

            try:
                key = stdscr.getch()
            except curses.error:
                key = -1

            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("r"), ord("R")):
                self._ingest_now()
            elif key in (curses.KEY_DOWN, ord("j")):
                self._event_scroll += 1
                self._dirty.set()
            elif key in (curses.KEY_UP, ord("k")):
                self._event_scroll = max(0, self._event_scroll - 1)
                self._dirty.set()
            elif key == curses.KEY_RESIZE:
                self._dirty.set()
            elif key == -1:
                time.sleep(0.05)

    def _init_colors(self, accent: int = DEFAULT_ACCENT) -> None:
        if not curses.has_colors():
            return
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            return
        # Fall back to the 8 base colours where 256 are not available.
        rich = getattr(curses, "COLORS", 8) >= 256
        palette = {
            PAIR_ACCENT: accent if rich else curses.COLOR_CYAN,
            PAIR_WARN: 214 if rich else curses.COLOR_YELLOW,
            PAIR_ALERT: 203 if rich else curses.COLOR_RED,
            PAIR_GOOD: 114 if rich else curses.COLOR_GREEN,
            PAIR_DIM: 244 if rich else curses.COLOR_WHITE,
        }
        for pair, colour in palette.items():
            try:
                curses.init_pair(pair, colour, -1)
            except curses.error:
                pass
        self._accent = accent

    @staticmethod
    def _probe_unicode(stdscr) -> bool:
        """Write a block glyph off-screen; if the terminal chokes, use ASCII."""
        try:
            stdscr.addstr(0, 0, SPARK_BLOCKS[0])
            stdscr.erase()
            return True
        except Exception:  # noqa: BLE001
            stdscr.erase()
            return False

    def _db_fingerprint(self) -> tuple:
        """A cheap change signal that survives WAL mode.

        In WAL journal mode a concurrent writer appends to `<db>-wal` and leaves
        the main file's mtime untouched until a checkpoint, so watching the main
        file alone makes `--no-watch` display a frozen frame while a separate
        vigil fills the archive. Fingerprint the whole set.
        """
        base = Path(self._db.path)
        parts: list[tuple] = []
        for path in (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")):
            try:
                stat = path.stat()
                parts.append((path.name, stat.st_mtime_ns, stat.st_size))
            except OSError:
                parts.append((path.name, 0, 0))
        return tuple(parts)

    # -- drawing

    def _draw(self, stdscr) -> None:
        with self._lock:
            state = self._state
            log_lines = list(self._log)

        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < MIN_ROWS or width < MIN_COLS:
            self._put(
                stdscr, 0, 0,
                f"augur needs {MIN_COLS}x{MIN_ROWS}; this terminal is {width}x{height}.",
                width,
            )
            if height > 1:
                # Even here, show the one thing worth shouting.
                with self._lock:
                    alerts = list(self._state.alerts)
                self._put(stdscr, 1, 0, "  ".join(alerts) if alerts else "[q] quit", width)
            stdscr.refresh()
            return

        accent = self._pair(PAIR_ACCENT)
        warn = self._pair(PAIR_WARN)
        alert = self._pair(PAIR_ALERT)
        good = self._pair(PAIR_GOOD)
        dim = self._pair(PAIR_DIM)
        heavy = "═" if self._unicode_ok else "="
        bar = BAR if self._unicode_ok else "|"

        # --- masthead
        self._put(stdscr, 0, 0, self._band(" AUGUR ", width, heavy), width, accent | curses.A_BOLD)
        right = state.game_date
        if state.version != "—":
            right += f"  {state.version}"
        self._put(
            stdscr, 1, 1, state.empire.upper(), max(0, width - len(right) - 4), accent | curses.A_BOLD
        )
        self._put(stdscr, 1, max(1, width - len(right) - 2), right, width, dim)
        row = 2
        if state.subtitle:
            self._put(stdscr, row, 1, state.subtitle, width - 2, dim)
            row += 1
        self._put(stdscr, row, 0, heavy * (width - 1), width, accent)
        row += 1

        if not state.loaded:
            self._put(stdscr, row + 1, 2, state.message or "No campaign loaded.", width - 4, warn)
            self._put(stdscr, row + 3, 2, "[r] read newest save    [q] quit", width - 4, accent)
            stdscr.refresh()
            return

        # --- alert strip, reverse video so it reads as a HUD warning
        if state.alerts:
            text = f" {ALERT_MARK if self._unicode_ok else '!'} " + "   ".join(state.alerts)
            self._put(
                stdscr, row, 0, text.ljust(width - 1), width,
                alert | curses.A_BOLD | curses.A_REVERSE,
            )
            row += 1

        # --- two columns
        split = max(26, min(width // 2, 42))
        self._put(stdscr, row, 1, f"{bar}EMPIRE", split - 1, accent | curses.A_BOLD)
        self._put(
            stdscr, row, split + 1, f"{bar}RESOURCE LEDGER", width - split, accent | curses.A_BOLD
        )
        col_top = row + 1

        # Track what was actually painted: both loops break early on a short
        # terminal, so advancing by the full list length pushes the panes below
        # off the bottom of the screen.
        column_rows = budget_columns(
            available=max(0, height - 1 - col_top),
            longest_column=max(len(state.facts), len(state.stockpiles)),
        )
        last_drawn = col_top - 1
        for index, (key, value) in enumerate(state.facts[:column_rows]):
            line = col_top + index
            if line >= height - 2:
                break
            self._put(stdscr, line, 3, f"{key:<13}", 14, dim)
            self._put(stdscr, line, 17, value, max(0, split - 18))
            last_drawn = max(last_drawn, line)

        for index, (name, held, net) in enumerate(state.stockpiles[:column_rows]):
            line = col_top + index
            if line >= height - 2:
                break
            self._put(stdscr, line, split + 3, f"{name:<15}", 16, dim)
            self._put(stdscr, line, split + 19, f"{held:>10}", 11)
            if net:
                room = max(0, width - split - 32)
                text = f"{net:>8} /mo" if room >= 12 else f"{net:>8}"
                self._put(
                    stdscr, line, split + 31, text, room,
                    warn if net.startswith("-") else good,
                )
            last_drawn = max(last_drawn, line)

        trimmed = max(len(state.facts), len(state.stockpiles)) - column_rows
        if trimmed > 0 and last_drawn >= col_top:
            marker = f"+{trimmed} more"
            # Anchor to the last row drawn, not the heading row, so it cannot
            # land on top of the RESOURCE LEDGER title.
            self._put(stdscr, last_drawn, max(1, width - len(marker) - 2), marker, 13, dim)

        row = last_drawn + 2

        # --- budget the two lower panes. The log is the pane the dashboard
        # exists for, so it is served first and trends trim to fit.
        allowed = budget_panes(
            available=max(0, height - 1 - row),
            trend_count=len(state.trends),
            event_count=len(state.events),
        )
        visible_trends = state.trends[:allowed]

        if visible_trends:
            self._section(stdscr, row, "TRAJECTORY", width)
            row += 1
            # Fixed columns; the value field ends at 30 and the arrow sits at 32,
            # so the sparkline cannot start before 34.
            spark_at = 34
            for trend in visible_trends:
                if row >= height - 2:
                    break
                pct = trend.pct
                colour = 0
                if pct is not None and abs(pct) >= 0.01:
                    colour = good if pct > 0 else warn
                self._put(stdscr, row, 3, f"{trend.label:<16}", 17, dim)
                self._put(stdscr, row, 20, f"{fmt_number(trend.current):>10}", 11)
                self._put(stdscr, row, 32, arrow(trend, unicode_ok=self._unicode_ok), 2, colour)
                self._put(
                    stdscr, row, spark_at,
                    sparkline(trend.values, unicode_ok=self._unicode_ok),
                    max(0, width - spark_at - 9), accent,
                )
                if pct is not None:
                    at = min(spark_at + TREND_POINTS + 2, max(spark_at, width - 8))
                    self._put(stdscr, row, at, f"{pct * 100:+.0f}%", 7, colour)
                row += 1
            hidden = len(state.trends) - len(visible_trends)
            if hidden > 0:
                self._put(stdscr, row - 1, max(1, width - 14), f"+{hidden} more", 13, dim)
            row += 1

        # --- situation log. Needs its heading plus at least one entry row;
        # a bare heading tells the reader nothing.
        if height - 1 - row >= LOG_MIN_ROWS:
            self._section(stdscr, row, "SITUATION LOG", width)
            row += 1
            space = max(1, height - 1 - row)
            events = state.events
            # When the list scrolls, the position indicator needs a row of its
            # own or it overwrites the last visible event.
            if len(events) > space:
                space = max(1, space - 1)
            max_scroll = max(0, len(events) - space)
            self._event_scroll = min(self._event_scroll, max_scroll)
            visible = events[self._event_scroll : self._event_scroll + space]
            if not visible:
                self._put(
                    stdscr, row, 3, "No events yet — detection needs two snapshots.", width - 4, dim
                )
            for date, kind, summary in visible:
                if row >= height - 1:
                    break
                self._put(stdscr, row, 3, date, 11, dim)
                self._put(stdscr, row, 15, kind[:22], 23, accent)
                self._put(stdscr, row, 39, summary, max(0, width - 40))
                row += 1
            if max_scroll and row < height - 1:
                self._put(
                    stdscr, row, max(1, width - 18),
                    f"{self._event_scroll + 1}-{self._event_scroll + len(visible)}/{len(events)}",
                    17, dim,
                )

        # --- footer
        self._put(stdscr, height - 1, 0, heavy * (width - 1), width, accent)
        status = f" {STATUS_MARK if self._unicode_ok else '*'} {self._status.upper()} "
        if log_lines:
            status += f"{'─' if self._unicode_ok else '-'} {log_lines[-1]} "
        keys = " [r] re-read   [j/k] scroll   [q] quit "
        room = width - len(keys) - 3
        if room > 4:
            self._put(stdscr, height - 1, 1, status[:room], width, accent | curses.A_BOLD)
        self._put(stdscr, height - 1, max(1, width - len(keys) - 1), keys, width, dim)
        stdscr.refresh()

    def _section(self, stdscr, row: int, title: str, width: int) -> None:
        """▌TITLE ──────── : the rule starts after the title, whatever its length."""
        bar = BAR if self._unicode_ok else "|"
        light = "─" if self._unicode_ok else "-"
        label = f"{bar}{title} "
        self._put(stdscr, row, 1, label, width - 2, self._pair(PAIR_ACCENT) | curses.A_BOLD)
        start = 1 + len(label)
        self._put(stdscr, row, start, light * max(0, width - start - 2), width, self._pair(PAIR_DIM))

    def _band(self, label: str, width: int, fill: str) -> str:
        """A masthead rule with the label inset, HUD style."""
        return (fill * 2 + label + fill * max(0, width - len(label) - 4))[: max(0, width - 1)]

    @staticmethod
    def _pair(index: int) -> int:
        try:
            return curses.color_pair(index) if curses.has_colors() else 0
        except curses.error:
            return 0

    def _rule(self, label: str, width: int) -> str:
        fill = "─" if self._unicode_ok else "-"
        return (label + fill * max(0, width - len(label) - 1))[: max(0, width - 1)]

    @staticmethod
    def _put(stdscr, y: int, x: int, text: str, limit: int, attr: int = 0) -> None:
        """Bounded write. curses raises on the last cell and off-screen writes."""
        height, width = stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        room = min(limit, width - x - 1)
        if room <= 0:
            return
        try:
            stdscr.addnstr(y, x, text, room, attr)
        except curses.error:
            pass


def run_dashboard(
    *,
    db_path: str | Path,
    language: str = "en",
    watch: bool = True,
    watch_paths: list[Path] | None = None,
) -> int:
    db = GameDatabase(db_path=db_path)
    try:
        return Dashboard(
            db=db, language=language, watch=watch, watch_paths=watch_paths
        ).run()
    finally:
        db.close()
