import bisect
import functools

from .notes import *
from .types import *

from .meta import *
from . import sus

__all__ = ["Score"]


class Score:

    def __init__(self):
        self.meta = Meta()
        self.notes: list[Note] = []
        self.events: list[Event] = []

    def _init_by_sus(self, chart: sus.SusChart) -> None:
        self.meta = Meta()
        self.notes = []
        self.events = []

        for key, value in chart.metas.items():
            field = key.lower()
            if not hasattr(self.meta, field):
                continue
            if field in ("waveoffset", "movieoffset", "basebpm"):
                try:
                    setattr(self.meta, field, float(value))
                except ValueError:
                    setattr(self.meta, field, value)
            else:
                setattr(self.meta, field, value)

        for measure, length in chart.bar_lengths:
            self.events.append(
                Event(
                    bar=Fraction(measure),
                    bar_length=Fraction(
                        int(length * chart.ticks_per_beat), chart.ticks_per_beat
                    ),
                )
            )

        if chart.bpms:
            for tick, bpm in chart.bpms:
                self.events.append(Event(bar=chart.tick_to_bar(tick), bpm=bpm))
        else:
            self.events.append(Event(bar=Fraction(0), bpm=Fraction(120)))

        for tick, speed in chart.speeds:
            self.events.append(Event(bar=chart.tick_to_bar(tick), speed=speed))

        flicks: dict[tuple[int, int], DirectionalType] = {}
        ease_ins: set[tuple[int, int]] = set()
        ease_outs: set[tuple[int, int]] = set()
        criticals: set[tuple[int, int]] = set()
        step_ignores: set[tuple[int, int]] = set()
        frictions: set[tuple[int, int]] = set()
        hiddens: set[tuple[int, int]] = set()
        slide_keys: set[tuple[int, int]] = set()

        for directional in chart.directionals:
            key = (directional.tick, directional.lane)
            if directional.type == 1:
                flicks[key] = DirectionalType.UP
            elif directional.type == 3:
                flicks[key] = DirectionalType.UPPER_LEFT
            elif directional.type == 4:
                flicks[key] = DirectionalType.UPPER_RIGHT
            elif directional.type == 2:
                ease_ins.add(key)
            elif directional.type in (5, 6):
                ease_outs.add(key)

        for tap in chart.taps:
            key = (tap.tick, tap.lane)
            if tap.type == 2:
                criticals.add(key)
            elif tap.type == 3:
                step_ignores.add(key)
            elif tap.type == 5:
                frictions.add(key)
            elif tap.type == 6:
                criticals.add(key)
                frictions.add(key)
            elif tap.type == 7:
                hiddens.add(key)
            elif tap.type == 8:
                hiddens.add(key)
                criticals.add(key)

        for hold in chart.slides:
            for point in hold:
                if point.type in (1, 2, 3, 5):
                    slide_keys.add((point.tick, point.lane))

        def modifier_tap(
            slide: Slide, key: tuple[int, int], critical: bool
        ) -> Tap | None:
            if key in hiddens:
                type = TapType.CRITICAL_CANCEL if critical else TapType.CANCEL
            elif key in frictions:
                type = TapType.CRITICAL_TREND if critical else TapType.TREND
            elif critical:
                type = TapType.CRITICAL
            else:
                return None
            return Tap(bar=slide.bar, lane=slide.lane, width=slide.width, type=type)

        def ease_directional(slide: Slide, key: tuple[int, int]) -> Directional | None:
            if key in ease_ins:
                type = DirectionalType.DOWN
            elif key in ease_outs:
                type = DirectionalType.LOWER_LEFT
            else:
                return None
            return Directional(
                bar=slide.bar, lane=slide.lane, width=slide.width, type=type
            )

        seen_taps: set[tuple[int, int]] = set()
        for note in sorted(chart.taps, key=lambda n: n.tick):
            bar = chart.tick_to_bar(note.tick)

            if note.type == 4:
                self.events.append(Event(bar=bar, text="SKILL"))
                continue

            if note.lane == sus.FEVER_LANE and note.width == 1:
                if note.type == 1:
                    self.events.append(Event(bar=bar, text="FEVER CHANCE!"))
                elif note.type == 2:
                    self.events.append(Event(bar=bar, text="SUPER FEVER!!"))
                continue

            if note.type in (7, 8):
                continue

            if note.lane < sus.MIN_LANE or note.lane > sus.MAX_LANE:
                continue

            key = (note.tick, note.lane)
            if key in slide_keys:
                continue

            if key in seen_taps:
                continue
            seen_taps.add(key)

            critical = key in criticals
            friction = key in frictions
            tap = Tap(
                bar=bar,
                lane=note.lane,
                width=note.width,
                type=(
                    TapType.CRITICAL_TREND
                    if critical and friction
                    else (
                        TapType.TREND
                        if friction
                        else TapType.CRITICAL if critical else TapType.TAP
                    )
                ),
            )

            direction = flicks.get(key)
            if direction is not None:
                self.notes.append(
                    Directional(
                        bar=bar,
                        lane=note.lane,
                        width=note.width,
                        type=direction,
                        tap=tap,
                    )
                )
            else:
                self.notes.append(tap)

        def build_holds(holds: list[list[sus.SusNote]], decoration: bool) -> None:
            for hold in holds:
                if len(hold) < 2 or not any(n.type in (1, 2) for n in hold):
                    continue

                start_key = (hold[0].tick, hold[0].lane)
                critical = start_key in criticals

                chain: list[Slide] = []
                for point in hold:
                    if point.type not in (1, 2, 3, 5):
                        continue
                    key = (point.tick, point.lane)
                    slide = Slide(
                        bar=chart.tick_to_bar(point.tick),
                        lane=point.lane,
                        width=point.width,
                        type=SlideType(point.type),
                        channel=point.channel,
                        decoration=decoration,
                    )

                    if decoration:
                        if point.type == 1 and critical:
                            slide.tap = Tap(
                                bar=slide.bar,
                                lane=slide.lane,
                                width=slide.width,
                                type=TapType.CRITICAL_CANCEL,
                            )
                        slide.directional = ease_directional(slide, key)
                    elif point.type == 1:
                        slide.tap = modifier_tap(slide, key, critical)
                        slide.directional = ease_directional(slide, key)
                    elif point.type == 5:
                        slide.directional = ease_directional(slide, key)
                    elif point.type == 2:
                        slide.tap = modifier_tap(
                            slide, key, critical or key in criticals
                        )
                        direction = flicks.get(key)
                        if direction is not None:
                            slide.directional = Directional(
                                bar=slide.bar,
                                lane=slide.lane,
                                width=slide.width,
                                type=direction,
                            )
                    elif point.type == 3:
                        if key in step_ignores:
                            slide.tap = Tap(
                                bar=slide.bar,
                                lane=slide.lane,
                                width=slide.width,
                                type=TapType.FLICK,
                            )
                        else:
                            slide.directional = ease_directional(slide, key)

                    if chain:
                        chain[-1].next = slide
                    chain.append(slide)

                for slide in chain:
                    slide.head = chain[0]

                self.notes.extend(chain)

        build_holds(chart.slides, False)
        build_holds(chart.guides, True)

        self.notes.sort()
        self._init_events()

    def _init_notes(self):
        self.notes.sort()

        note_deleted = [False] * len(self.notes)
        note_indexes: dict[Fraction, list[int]] = {}

        for i, note in enumerate(self.notes):
            if not 0 <= note.lane - 2 < 12:
                note_deleted[i] = True
                self.events.append(
                    Event(
                        bar=note.bar,
                        text=(
                            "SKILL"
                            if note.lane == 0
                            else "FEVER CHANCE!" if note.type == 1 else "SUPER FEVER!!"
                        ),
                    )
                )
                continue

            if note.bar not in note_indexes:
                note_indexes[note.bar] = []

            note_indexes[note.bar].append(i)

        for i, directional in enumerate(self.notes):
            if note_deleted[i] or not isinstance(directional, Directional):
                continue

            for j in note_indexes[directional.bar]:
                tap = self.notes[j]
                if note_deleted[j] or not isinstance(tap, Tap):
                    continue

                if (
                    tap.bar == directional.bar
                    and tap.lane == directional.lane
                    and tap.width == directional.width
                ):
                    note_deleted[j] = True
                    directional.tap = tap

        for i, slide in enumerate(self.notes):
            if note_deleted[i] or not isinstance(slide, Slide):
                continue

            if slide.head is None:
                slide.head = slide

            for j in note_indexes[slide.bar]:
                tap = self.notes[j]
                if note_deleted[j] or not isinstance(tap, Tap):
                    continue

                if (
                    tap.bar == slide.bar
                    and tap.lane == slide.lane
                    and tap.width == slide.width
                ):
                    note_deleted[j] = True
                    slide.tap = tap

            for j in note_indexes[slide.bar]:
                directional = self.notes[j]
                if note_deleted[j] or not isinstance(directional, Directional):
                    continue

                if (
                    directional.bar == slide.bar
                    and directional.lane == slide.lane
                    and directional.width == slide.width
                ):
                    note_deleted[j] = True
                    slide.directional = directional
                    if directional.tap is not None:
                        slide.tap = directional.tap

            if slide.type != SlideType.END:
                for j in range(i + 1, len(self.notes)):
                    next = self.notes[j]
                    if (
                        note_deleted[j]
                        or not isinstance(next, Slide)
                        or next.channel != slide.channel
                        or next.decoration != slide.decoration
                    ):
                        continue

                    slide.next = next
                    next.head = slide.head
                    break

        self.notes = [note for i, note in enumerate(self.notes) if not note_deleted[i]]

    def _init_events(self):
        self.events.sort()
        events = []

        for event in self.events:
            if len(events) and event == events[-1]:
                events[-1] |= event
            else:
                events.append(event)

        self.events = events

    @classmethod
    def open(cls, file: str, *args, **kwargs):
        self = cls()
        with open(file, *args, **kwargs) as f:
            self._init_by_sus(sus.loads(f.read()))

        return self

    @functools.cached_property
    def timed_events(self):
        timed_events: list[tuple[Fraction, Event]] = []

        t = 0
        e = Event(bar=0, bpm=120, bar_length=4, sentence_length=4)
        for i, event in enumerate(self.events):
            t += (event.bar - e.bar) * e.bar_length * 60 / e.bpm
            e |= event

            timed_events.append((t, e))

        if not timed_events:
            timed_events.append((0, e))

        return timed_events

    def get_timed_event(self, bar: Fraction) -> tuple[Fraction, Event]:
        t, e = self.timed_events[
            bisect.bisect(self.timed_events, bar, key=lambda x: x[1].bar) - 1
        ]
        t += e.bar_length * 60 / e.bpm * (bar - e.bar)
        return t, e

    def get_time(self, bar: Fraction) -> Fraction:
        return self.get_timed_event(bar)[0]

    def get_event(self, bar: Fraction) -> Event:
        return self.get_timed_event(bar)[1]

    def get_time_delta(self, bar_from: Fraction, bar_to: Fraction) -> Fraction:
        return self.get_time(bar_to) - self.get_time(bar_from)

    def get_bar_by_time(self, time: float) -> Fraction:
        t = 0.0
        event = Event(bar=0, bpm=120, bar_length=4, sentence_length=4)

        for i in range(len(self.events)):
            event = event | self.events[i]
            if i + 1 == len(self.events):
                break

            event_time = (
                event.bar_length * 60 / event.bpm * (self.events[i + 1].bar - event.bar)
            )
            if t + event_time > time:
                break
            else:
                t += event_time

        bar = event.bar + (time - t) / (event.bar_length * 60 / event.bpm)

        return Fraction(bar).limit_denominator()

    def print(self, bar_from: int, bar_to: int):
        for note in self.notes:
            if bar_from <= note.bar < bar_to:
                print(note, f"{note.is_trend() = }")
                if hasattr(note, "tap") and note.tap:
                    print("    tap:", note.tap, f"{note.tap.is_trend() = }")
                if hasattr(note, "directional") and note.directional:
                    print(
                        "    directional:",
                        note.directional,
                        f"{note.directional.is_trend() = }",
                    )

                print()
