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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from augur.context import CampaignContext, ContextError
from augur.core.database import GameDatabase

REFRESH_SECONDS = 1.0
TREND_POINTS = 12
MAX_LOG_LINES = 50
MAX_EVENTS = 60

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
SPARK_ASCII = "_.-~=+*#"

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

    state.trends = build_trends(db=db, session_id=_session_id(db))
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


def _session_id(db: GameDatabase) -> str | None:
    try:
        sessions = db.get_sessions(limit=1)
    except Exception:  # noqa: BLE001 - a dashboard must not die on a read
        return None
    if not sessions:
        return None
    return str(sessions[0].get("id") or "") or None


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
        self._status = "vigil running"

    def _ingest_now(self) -> None:
        """Manual 'r' — ingest in a thread so the UI keeps drawing."""

        def work() -> None:
            from augur.ingest import IngestError, ingest_save

            self.log("re-reading newest save…")
            try:
                result = ingest_save(db=self._db, save_path=None)
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

        threading.Thread(target=work, daemon=True).start()

    # -- entry point

    def run(self) -> int:
        self.refresh_state()
        if self._watch:
            self._start_vigil()
        else:
            self._status = "watch off"
        try:
            curses.wrapper(self._loop)
        finally:
            if self._vigil is not None:
                self._vigil.stop()
        return 0

    def _loop(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        self._init_colors()
        self._unicode_ok = self._probe_unicode(stdscr)

        last_poll = 0.0
        last_db_mtime = self._db_mtime()

        while True:
            now = time.time()
            if now - last_poll >= REFRESH_SECONDS:
                last_poll = now
                # Another process (a standalone vigil, or an ingest) may have
                # written to the archive; pick that up without being told.
                mtime = self._db_mtime()
                if mtime != last_db_mtime:
                    last_db_mtime = mtime
                    self.refresh_state()
                self._dirty.set()

            if self._dirty.is_set():
                self._dirty.clear()
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

    @staticmethod
    def _init_colors() -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        for index, fg in enumerate(
            (curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_GREEN), start=1
        ):
            try:
                curses.init_pair(index, fg, -1)
            except curses.error:
                pass

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

    def _db_mtime(self) -> float:
        try:
            return Path(self._db.path).stat().st_mtime
        except OSError:
            return 0.0

    # -- drawing

    def _draw(self, stdscr) -> None:
        with self._lock:
            state = self._state
            log_lines = list(self._log)

        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 8 or width < 40:
            self._put(stdscr, 0, 0, "Terminal too small for the orrery.", width)
            stdscr.refresh()
            return

        cyan = self._pair(1)
        yellow = self._pair(2)
        red = self._pair(3)

        title = f" augur · {state.empire} · {state.game_date} "
        if state.version != "—":
            title += f"· {state.version} "
        self._put(stdscr, 0, 0, self._rule(title, width), width, cyan | curses.A_BOLD)

        if not state.loaded:
            self._put(stdscr, 2, 2, state.message or "No campaign loaded.", width - 4, yellow)
            self._put(stdscr, 4, 2, "Press r to read your newest save, q to quit.", width - 4)
            stdscr.refresh()
            return

        row = 1
        if state.alerts:
            self._put(
                stdscr, row, 0, "  " + "   ".join(state.alerts)[: width - 2], width, red | curses.A_BOLD
            )
            row += 1

        # Two columns: facts on the left, resources on the right.
        split = max(24, min(width // 2, 40))
        col_top = row + 1
        self._put(stdscr, row, 2, "EMPIRE", width, cyan | curses.A_BOLD)
        self._put(stdscr, row, split + 2, "RESOURCES", width, cyan | curses.A_BOLD)

        for index, (key, value) in enumerate(state.facts):
            line = col_top + index
            if line >= height - 2:
                break
            self._put(stdscr, line, 2, f"{key:<13}{value}", split - 2)

        for index, (name, held, net) in enumerate(state.stockpiles):
            line = col_top + index
            if line >= height - 2:
                break
            text = f"{name:<15}{held:>12}"
            if net:
                text += f"  {net:>8}/mo"
            self._put(stdscr, line, split + 2, text, width - split - 3)

        row = col_top + max(len(state.facts), len(state.stockpiles)) + 1

        if state.trends and row < height - 4:
            self._put(stdscr, row, 0, self._rule(" TRENDS ", width), width, cyan)
            row += 1
            for trend in state.trends:
                if row >= height - 3:
                    break
                spark = sparkline(trend.values, unicode_ok=self._unicode_ok)
                mark = arrow(trend, unicode_ok=self._unicode_ok)
                text = f"  {trend.label:<15}{fmt_number(trend.current):>10}  {mark} {spark}"
                pct = trend.pct
                if pct is not None:
                    text += f"  {pct * 100:+.0f}%"
                self._put(stdscr, row, 0, text, width)
                row += 1
            row += 1

        if row < height - 3:
            self._put(stdscr, row, 0, self._rule(" EVENTS ", width), width, cyan)
            row += 1
            space = max(1, height - 2 - row)
            events = state.events
            # Clamp the scroll so it cannot run off the end of the list.
            max_scroll = max(0, len(events) - space)
            self._event_scroll = min(self._event_scroll, max_scroll)
            visible = events[self._event_scroll : self._event_scroll + space]
            if not visible:
                self._put(
                    stdscr, row, 2, "No events yet — detection needs two snapshots.", width - 2
                )
            for date, kind, summary in visible:
                if row >= height - 2:
                    break
                self._put(stdscr, row, 2, f"{date}  {kind:<22} {summary}", width - 3)
                row += 1

        footer = f" {self._status} "
        if log_lines:
            footer += f"· {log_lines[-1]} "
        keys = " [r]e-read  [j/k] scroll  [q]uit "
        self._put(stdscr, height - 1, 0, " " * (width - 1), width)
        self._put(stdscr, height - 1, 0, footer[: max(0, width - len(keys) - 2)], width)
        self._put(stdscr, height - 1, max(0, width - len(keys) - 1), keys, width, cyan)
        stdscr.refresh()

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
