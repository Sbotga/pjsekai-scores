"""SUS parser mirroring sonolus-level-converters' sus loader (minus note speed
ratios, volumes, and HISPEED layer selection, which this renderer ignores)."""

import dataclasses

from .types import Fraction

__all__ = ["SusNote", "SusChart", "load", "loads"]

TICKS_PER_BEAT = 480
MIN_LANE = 2
MAX_LANE = 13
FEVER_LANE = 15


@dataclasses.dataclass
class SusNote:
    tick: int
    lane: int
    width: int
    type: int
    channel: int = 0


@dataclasses.dataclass
class _Bar:
    measure: int
    ticks_per_measure: int
    ticks: int


@dataclasses.dataclass
class SusChart:
    ticks_per_beat: int
    metas: dict[str, str]
    bars: list[_Bar]
    bar_lengths: list[tuple[int, float]]
    bpms: list[tuple[int, Fraction]]
    speeds: list[tuple[int, float]]
    taps: list[SusNote]
    directionals: list[SusNote]
    slides: list[list[SusNote]]
    guides: list[list[SusNote]]

    def tick_to_bar(self, tick: int) -> Fraction:
        segment = self.bars[0]
        segment_start = segment.ticks
        acc = segment.ticks
        for bar in self.bars[1:]:
            acc += bar.ticks
            if acc > tick:
                break
            segment = bar
            segment_start = acc
        rel = tick - segment_start
        measure = segment.measure + rel // segment.ticks_per_measure
        return Fraction(measure) + Fraction(
            rel % segment.ticks_per_measure, segment.ticks_per_measure
        )


def _get_bars(bar_lengths: list[tuple[int, float]], ticks_per_beat: int) -> list[_Bar]:
    sorted_bl = sorted(bar_lengths, key=lambda x: x[0])
    bars = [_Bar(sorted_bl[0][0], int(sorted_bl[0][1] * ticks_per_beat), 0)]
    for i in range(1, len(sorted_bl)):
        measure = sorted_bl[i][0]
        tpm = int(sorted_bl[i][1] * ticks_per_beat)
        ticks = int(
            (measure - sorted_bl[i - 1][0]) * sorted_bl[i - 1][1] * ticks_per_beat
        )
        bars.append(_Bar(measure, tpm, ticks))
    return bars


def _get_ticks(bars: list[_Bar], measure: int, i: int, total: int) -> int:
    b_index = 0
    acc_ticks = 0
    for idx in range(len(bars)):
        if bars[idx].measure > measure:
            break
        b_index = idx
        acc_ticks += bars[idx].ticks
    return (
        acc_ticks
        + (measure - bars[b_index].measure) * bars[b_index].ticks_per_measure
        + (i * bars[b_index].ticks_per_measure) // total
    )


def _parse_note_cells(data: str) -> list[tuple[str, float]]:
    if "," not in data:
        end = len(data) - len(data) % 2
        return [(data[i : i + 2], 1.0) for i in range(0, end, 2)]

    cells: list[tuple[str, float]] = []
    i = 0
    while i < len(data):
        while i < len(data) and data[i].isspace():
            i += 1
        if i + 1 >= len(data):
            break
        note_data = data[i : i + 2]
        i += 2
        speed_ratio = 1.0
        if i < len(data) and data[i] == ",":
            i += 1
            start = i
            while i < len(data) and not data[i].isspace() and data[i] != ",":
                i += 1
            if i > start:
                sr = float(data[start:i])
                if sr > 0.0:
                    speed_ratio = sr
        cells.append((note_data, speed_ratio))
        while i < len(data) and (data[i].isspace() or data[i] == ","):
            i += 1
    return cells


def _get_notes(
    header: str, data: str, bars: list[_Bar], measure: int, channel: int = 0
) -> list[SusNote]:
    notes: list[SusNote] = []
    cells = _parse_note_cells(data)
    for i, (cell, _) in enumerate(cells):
        if len(cell) < 2 or cell == "00":
            continue
        tick = _get_ticks(bars, measure, i * 2, len(cells) * 2)
        notes.append(
            SusNote(
                tick=tick,
                lane=int(header[4], 36),
                width=int(cell[1], 36),
                type=int(cell[0], 36),
                channel=channel,
            )
        )
    return notes


def _get_note_stream(stream: list[SusNote]) -> list[list[SusNote]]:
    sorted_stream = sorted(stream, key=lambda n: n.tick)
    holds: list[list[SusNote]] = []
    current: list[SusNote] = []
    new_hold = True
    for note in sorted_stream:
        if new_hold:
            current = []
            new_hold = False
        current.append(note)
        if note.type == 2:
            holds.append(current)
            new_hold = True
    return holds


def _dedup_holds(holds: list[list[SusNote]]) -> list[list[SusNote]]:
    seen: set[tuple[int, int, int, int]] = set()
    result: list[list[SusNote]] = []
    for hold in holds:
        if len(hold) < 2:
            continue
        key = (hold[0].tick, hold[0].lane, hold[-1].tick, hold[-1].lane)
        if key in seen:
            continue
        seen.add(key)
        result.append(hold)
    return result


def _parse_hispeed_entry(entry: str) -> tuple[int, int, float] | None:
    apos = entry.find("'")
    if apos == -1:
        return None
    colon = entry.find(":", apos + 1)
    if colon == -1:
        return None
    try:
        return (
            int(entry[:apos]),
            int(entry[apos + 1 : colon]),
            float(entry[colon + 1 :]),
        )
    except ValueError:
        return None


def _is_command(line: str) -> bool:
    if line[1:2].isdigit():
        return False
    first_quote = line.find('"')
    if first_quote != -1:
        last_quote = line.rfind('"')
        if first_quote != last_quote:
            space = line.find(" ")
            if space != -1 and ":" in line[:space]:
                return False
            return True
    return ":" not in line


def load(fp) -> SusChart:
    return loads(fp.read())


def loads(data: str) -> SusChart:
    ticks_per_beat = TICKS_PER_BEAT
    metas: dict[str, str] = {}
    bar_lengths: list[tuple[int, float]] = []
    bpm_definitions: dict[str, Fraction] = {}
    bpms: list[tuple[int, Fraction]] = []
    speeds: list[tuple[int, float]] = []
    taps: list[SusNote] = []
    directionals: list[SusNote] = []
    slide_streams: dict[int, list[SusNote]] = {}
    guide_streams: dict[int, list[SusNote]] = {}

    measure_offset = 0
    lines_to_process: list[tuple[str, int]] = []
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue

        if _is_command(line):
            space = line.find(" ", 1)
            if space == -1:
                lines_to_process.append((line, measure_offset))
                continue
            key = line[1:space].upper()
            value = line[space + 1 :].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            if key == "REQUEST":
                parts = value.split()
                if len(parts) == 2 and parts[0] == "ticks_per_beat":
                    ticks_per_beat = int(parts[1])
            elif key == "MEASUREBS":
                measure_offset = int(value)
        else:
            colon = line.find(":", 1)
            if colon == -1:
                lines_to_process.append((line, measure_offset))
                continue
            header = line[1:colon].strip()
            line_data = line[colon + 1 :].strip()
            if len(header) == 5 and header.endswith("02") and header[:3].isdigit():
                bar_lengths.append((int(header[:3]) + measure_offset, float(line_data)))

        lines_to_process.append((line, measure_offset))

    if not bar_lengths:
        bar_lengths.append((0, 4.0))

    bars = _get_bars(bar_lengths, ticks_per_beat)

    for line, m_offset in lines_to_process:
        if _is_command(line):
            space = line.find(" ", 1)
            if space == -1:
                continue
            key = line[1:space].upper()
            value = line[space + 1 :].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            if key not in ("REQUEST", "MEASUREBS", "HISPEED", "NOSPEED"):
                metas[key] = value
            continue

        colon = line.find(":", 1)
        if colon == -1:
            continue
        header = line[1:colon].strip()
        line_data = line[colon + 1 :].strip()

        if len(header) not in (5, 6):
            continue

        if len(header) == 5 and header.endswith("02") and header[:3].isdigit():
            pass
        elif header.startswith("BPM") and len(header) == 5:
            bpm_definitions[header[3:]] = Fraction(line_data)
        elif len(header) == 5 and header.endswith("08") and header[:3].isdigit():
            measure = int(header[:3]) + m_offset
            stripped = line_data.replace(" ", "")
            pairs = [
                stripped[j : j + 2]
                for j in range(0, len(stripped) - len(stripped) % 2, 2)
            ]
            for j, pair in enumerate(pairs):
                if pair == "00":
                    continue
                tick = _get_ticks(bars, measure, j, len(pairs))
                bpms.append((tick, bpm_definitions.get(pair, Fraction(120))))
        elif header.startswith("TIL") and len(header) == 5:
            stripped = line_data.strip('"').replace(" ", "")
            entries: list[tuple[int, float]] = []
            for entry in stripped.split(","):
                parsed = _parse_hispeed_entry(entry)
                if parsed:
                    measure, tick_offset, value = parsed
                    entries.append(
                        (_get_ticks(bars, measure, 0, 1) + tick_offset, value)
                    )
            previous = 1.0
            for tick, value in sorted(entries, key=lambda x: x[0]):
                if value != previous:
                    speeds.append((tick, value))
                    previous = value
        elif len(header) == 5 and header[3] == "1" and header[:3].isdigit():
            measure = int(header[:3]) + m_offset
            taps.extend(_get_notes(header, line_data, bars, measure))
        elif len(header) == 5 and header[3] == "5" and header[:3].isdigit():
            measure = int(header[:3]) + m_offset
            directionals.extend(_get_notes(header, line_data, bars, measure))
        elif len(header) == 6 and header[3] == "3" and header[:3].isdigit():
            measure = int(header[:3]) + m_offset
            channel = int(header[5], 36)
            slide_streams.setdefault(channel, []).extend(
                _get_notes(header, line_data, bars, measure, channel)
            )
        elif len(header) == 6 and header[3] == "9" and header[:3].isdigit():
            measure = int(header[:3]) + m_offset
            channel = int(header[5], 36)
            guide_streams.setdefault(channel, []).extend(
                _get_notes(header, line_data, bars, measure, channel)
            )

    slides: list[list[SusNote]] = []
    for stream in slide_streams.values():
        slides.extend(_get_note_stream(stream))

    guides: list[list[SusNote]] = []
    for stream in guide_streams.values():
        guides.extend(_get_note_stream(stream))

    return SusChart(
        ticks_per_beat=ticks_per_beat,
        metas=metas,
        bars=bars,
        bar_lengths=bar_lengths,
        bpms=sorted(bpms, key=lambda x: x[0]),
        speeds=speeds,
        taps=taps,
        directionals=directionals,
        slides=_dedup_holds(slides),
        guides=_dedup_holds(guides),
    )
