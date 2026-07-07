"""Render a sonolus-level-converters Score to a chart PNG.

The renderer consumes ``sonolus_converters.notes.score.Score`` objects directly
and paints with Pillow + NumPy. Only the note types and attributes the chart
view draws are supported: Bpm, TimeScaleGroup (speed lines), Single, Slide,
Guide, Skill and Fever markers. speedRatio, timeScaleGroup assignment, Volume
and fake flags are ignored.
"""

import bisect
import dataclasses
import io
import math
import typing
import urllib.request
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from sonolus_converters.notes.bpm import Bpm
from sonolus_converters.notes.guide import Guide
from sonolus_converters.notes.score import Score
from sonolus_converters.notes.single import FeverChance, FeverStart, Single, Skill
from sonolus_converters.notes.slide import (
    Slide,
    SlideEndPoint,
    SlideRelayPoint,
    SlideStartPoint,
)
from sonolus_converters.notes.timescale import TimeScaleGroup

__all__ = ["ChartRenderer", "render_score", "load_sus", "load_pjsk"]

_ASSETS = Path(__file__).parent / "assets"
_FONT_MEDIUM = _ASSETS / "NotoSansCJKEx-Medium.otf"
_FONT_BLACK = _ASSETS / "NotoSansCJKEx-Black.otf"

LANE_WIDTH = 16
TIME_HEIGHT = 360
NOTE_SIZE = 16
FLICK_HEIGHT = 24
LANE_PADDING = 40
TIME_PADDING = 32
SLIDE_PATH_PADDING = -1
META_SIZE = 192
TICK_LENGTH = 24
TICK_2_LENGTH = 8
N_LANES = 12
SENTENCE_LENGTH = 4

WHITE = (255, 255, 255, 255)
LIGHT = (226, 226, 226, 255)
YELLOW = (254, 227, 0, 255)
MAGENTA = (255, 51, 255, 255)
SLIDE_FILL = (201, 252, 226, 204)
SLIDE_CRITICAL_FILL = (252, 241, 195, 204)
DECORATION_STOPS = ((201, 252, 226, 153), (201, 252, 226, 51))
DECORATION_CRITICAL_STOPS = ((252, 241, 195, 153), (252, 241, 195, 51))
LANE_FILL = (76, 77, 80, 128)
BACKGROUND_FILL = (158, 158, 158, 179)
META_FILL = (0, 0, 0, 255)


@dataclasses.dataclass
class _Point:
    beat: float  # in bars once normalized
    lane: float
    width: float
    kind: str  # start / end / tick / attach / invisible / guide
    ease: str = "linear"
    judge: str = "normal"
    critical: bool = False
    direction: str | None = None


@dataclasses.dataclass
class _Chain:
    points: list[_Point]
    critical: bool
    decoration: bool


@dataclasses.dataclass
class _Single:
    beat: float  # in bars once normalized
    lane: float
    width: float
    critical: bool
    trace: bool
    direction: str | None


@dataclasses.dataclass
class _Event:
    bar: float
    bpm: float | None = None
    bar_length: float | None = None
    speed: float | None = None
    text: str | None = None

    def merge(self, other: "_Event") -> None:
        self.bar = other.bar
        self.bpm = other.bpm if other.bpm is not None else self.bpm
        self.bar_length = (
            other.bar_length if other.bar_length is not None else self.bar_length
        )
        self.speed = other.speed if other.speed is not None else self.speed
        self.text = other.text if other.text is not None else self.text

    @property
    def special(self) -> bool:
        return (
            self.bpm is not None
            or self.bar_length is not None
            or self.speed is not None
            or self.text is not None
        )


class _Timeline:

    def __init__(
        self, bpms: list[tuple[float, float]], bar_lengths: list[tuple[int, float]]
    ):
        segments = sorted(bar_lengths, key=lambda x: x[0])
        self.bars: list[tuple[int, float, float]] = []
        start_beat = 0.0
        previous: tuple[int, float] | None = None
        for measure, length in segments:
            if previous is not None:
                start_beat += (measure - previous[0]) * previous[1]
            self.bars.append((measure, length, start_beat))
            previous = (measure, length)

        points = sorted(bpms, key=lambda x: x[0])
        if not points:
            points = [(0.0, 120.0)]
        if points[0][0] > 0:
            points.insert(0, (0.0, 120.0))
        self.bpms: list[tuple[float, float, float]] = []
        elapsed = 0.0
        previous_bpm: tuple[float, float] | None = None
        for beat, bpm in points:
            if previous_bpm is not None:
                elapsed += (beat - previous_bpm[0]) * 60.0 / previous_bpm[1]
            self.bpms.append((beat, bpm, elapsed))
            previous_bpm = (beat, bpm)

        self._bar_keys = [segment[0] for segment in self.bars]
        self._bar_beat_keys = [segment[2] for segment in self.bars]
        self._bpm_keys = [segment[0] for segment in self.bpms]
        self._time_of_bar_cache: dict[float, float] = {}

    def beat_of_bar(self, bar: float) -> float:
        measure, length, start_beat = self.bars[
            max(bisect.bisect_right(self._bar_keys, bar) - 1, 0)
        ]
        return start_beat + (bar - measure) * length

    def bar_of_beat(self, beat: float) -> float:
        measure, length, start_beat = self.bars[
            max(bisect.bisect_right(self._bar_beat_keys, beat) - 1, 0)
        ]
        return measure + (beat - start_beat) / length

    def bar_length_at(self, bar: float) -> float:
        return self.bars[max(bisect.bisect_right(self._bar_keys, bar) - 1, 0)][1]

    def time(self, beat: float) -> float:
        start_beat, bpm, elapsed = self.bpms[
            max(bisect.bisect_right(self._bpm_keys, beat) - 1, 0)
        ]
        return elapsed + (beat - start_beat) * 60.0 / bpm

    def time_of_bar(self, bar: float) -> float:
        cached = self._time_of_bar_cache.get(bar)
        if cached is None:
            cached = self.time(self.beat_of_bar(bar))
            self._time_of_bar_cache[bar] = cached
        return cached


def _parse_bar_lengths(sus_text: str) -> list[tuple[int, float]]:
    bar_lengths: list[tuple[int, float]] = []
    measure_offset = 0
    for raw_line in sus_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue
        if line[1:2].isdigit():
            colon = line.find(":", 1)
            if colon == -1:
                continue
            header = line[1:colon].strip()
            if len(header) == 5 and header.endswith("02") and header[:3].isdigit():
                bar_lengths.append(
                    (int(header[:3]) + measure_offset, float(line[colon + 1 :].strip()))
                )
        elif line[1:10].upper() == "MEASUREBS":
            value = line[10:].strip().strip('"')
            try:
                measure_offset = int(value)
            except ValueError:
                pass
    if not bar_lengths:
        bar_lengths.append((0, 4.0))
    return bar_lengths


def load_sus(file: typing.Union[str, Path]) -> tuple[Score, list[tuple[int, float]]]:
    import sonolus_converters

    text = Path(file).read_text(encoding="utf-8")
    score = sonolus_converters.sus.load(io.StringIO(text))
    return score, _parse_bar_lengths(text)


def load_pjsk(file: typing.Union[str, Path]) -> tuple[Score, list[tuple[int, float]]]:
    import sonolus_converters

    score = sonolus_converters.pjsk.load(file)
    return score, [(0, 4.0)]


def _row1(y: int) -> int:
    return y if y % 2 == 0 else y - 1


_SPRITE_CACHE: dict[str, Image.Image | None] = {}
_SCALED_CACHE: dict[tuple, Image.Image | None] = {}
_NOTE_TILE_CACHE: dict[tuple, Image.Image | None] = {}
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_FRACTION_CACHE: dict[float, Fraction] = {}


_BEZIER_T = np.arange(1, 25) / 24
_BEZIER_U = 1 - _BEZIER_T
_BEZIER_W0 = _BEZIER_U * _BEZIER_U * _BEZIER_U
_BEZIER_W1 = 3 * _BEZIER_U * _BEZIER_U * _BEZIER_T
_BEZIER_W2 = 3 * _BEZIER_U * _BEZIER_T * _BEZIER_T
_BEZIER_W3 = _BEZIER_T * _BEZIER_T * _BEZIER_T


def _flatten_bezier(curve: tuple) -> np.ndarray:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = curve
    out = np.empty((24, 2))
    out[:, 0] = _BEZIER_W0 * x0 + _BEZIER_W1 * x1 + _BEZIER_W2 * x2 + _BEZIER_W3 * x3
    out[:, 1] = _BEZIER_W0 * y0 + _BEZIER_W1 * y1 + _BEZIER_W2 * y2 + _BEZIER_W3 * y3
    return out


def _binary_solution_for_x(y: float, curve: tuple, e: float = 0.1) -> float:
    lo, hi = 0.0, 1.0
    while True:
        t = (lo + hi) / 2
        u = 1 - t
        px = (
            curve[0][0] * u**3
            + curve[1][0] * u**2 * t * 3
            + curve[2][0] * u * t**2 * 3
            + curve[3][0] * t**3
        )
        py = (
            curve[0][1] * u**3
            + curve[1][1] * u**2 * t * 3
            + curve[2][1] * u * t**2 * 3
            + curve[3][1] * t**3
        )
        if y - e < py < y + e:
            return px
        if py > y:
            lo = t
        else:
            hi = t


class ChartRenderer:

    def __init__(
        self,
        score: Score,
        *,
        title: str | None = None,
        artist: str | None = None,
        difficulty: str | None = None,
        playlevel: str | None = None,
        jacket: str | None = None,
        bar_lengths: list[tuple[int, float]] | None = None,
    ):
        self.title = title if title is not None else score.metadata.title
        self.artist = artist if artist is not None else score.metadata.artist
        self.difficulty = difficulty
        self.playlevel = playlevel
        self.jacket = jacket

        self._images: dict[str, Image.Image | None] = {}
        self._text_tiles: dict[tuple, tuple[Image.Image, int, int, float, int, int]] = (
            {}
        )

        self.singles: list[_Single] = []
        self.chains: list[_Chain] = []
        bpms: list[tuple[float, float]] = []
        speeds: list[tuple[float, float]] = []
        texts: list[tuple[float, str]] = []

        for note in score.notes:
            if isinstance(note, Bpm):
                bpms.append((note.beat, note.bpm))
            elif isinstance(note, TimeScaleGroup):
                previous = 1.0
                for point in sorted(note.changes, key=lambda p: p.beat):
                    if point.timeScale != previous:
                        speeds.append((point.beat, point.timeScale))
                        previous = point.timeScale
            elif isinstance(note, Skill):
                texts.append((note.beat, "SKILL"))
            elif isinstance(note, FeverChance):
                texts.append((note.beat, "FEVER CHANCE!"))
            elif isinstance(note, FeverStart):
                texts.append((note.beat, "SUPER FEVER!!"))
            elif isinstance(note, Single):
                self.singles.append(
                    _Single(
                        beat=note.beat,
                        lane=note.lane - note.size + 8,
                        width=note.size * 2,
                        critical=bool(note.critical),
                        trace=bool(note.trace),
                        direction=note.direction,
                    )
                )
            elif isinstance(note, Slide):
                points: list[_Point] = []
                for connection in sorted(note.connections, key=lambda c: c.beat):
                    lane = connection.lane - connection.size + 8
                    width = connection.size * 2
                    if isinstance(connection, SlideStartPoint):
                        points.append(
                            _Point(
                                beat=connection.beat,
                                lane=lane,
                                width=width,
                                kind="start",
                                ease=connection.ease,
                                judge=connection.judgeType,
                                critical=bool(note.critical),
                            )
                        )
                    elif isinstance(connection, SlideEndPoint):
                        points.append(
                            _Point(
                                beat=connection.beat,
                                lane=lane,
                                width=width,
                                kind="end",
                                judge=connection.judgeType,
                                critical=bool(connection.critical),
                                direction=connection.direction,
                            )
                        )
                    elif isinstance(connection, SlideRelayPoint):
                        if connection.critical is None:
                            kind = "invisible"
                        elif connection.type == "attach":
                            kind = "attach"
                        else:
                            kind = "tick"
                        points.append(
                            _Point(
                                beat=connection.beat,
                                lane=lane,
                                width=width,
                                kind=kind,
                                ease=connection.ease,
                            )
                        )
                if points:
                    self.chains.append(
                        _Chain(
                            points=points,
                            critical=bool(note.critical),
                            decoration=False,
                        )
                    )
            elif isinstance(note, Guide):
                points = [
                    _Point(
                        beat=point.beat,
                        lane=point.lane - point.size + 8,
                        width=point.size * 2,
                        kind="guide",
                        ease=point.ease,
                    )
                    for point in sorted(note.midpoints, key=lambda p: p.beat)
                ]
                if points:
                    self.chains.append(
                        _Chain(
                            points=points,
                            critical=note.color == "yellow",
                            decoration=True,
                        )
                    )

        self.timeline = _Timeline(bpms, bar_lengths or [(0, 4.0)])

        for single in self.singles:
            single.beat = self.timeline.bar_of_beat(single.beat)
        for chain in self.chains:
            for point in chain.points:
                point.beat = self.timeline.bar_of_beat(point.beat)

        raw_events: list[_Event] = []
        for measure, length in sorted(bar_lengths or [(0, 4.0)], key=lambda x: x[0]):
            raw_events.append(_Event(bar=float(measure), bar_length=length))
        for beat, bpm in sorted(bpms, key=lambda x: x[0]):
            raw_events.append(_Event(bar=self.timeline.bar_of_beat(beat), bpm=bpm))
        for beat, value in speeds:
            raw_events.append(_Event(bar=self.timeline.bar_of_beat(beat), speed=value))
        for beat, text in sorted(texts, key=lambda x: x[0]):
            raw_events.append(_Event(bar=self.timeline.bar_of_beat(beat), text=text))

        raw_events.sort(key=lambda e: e.bar)
        self.events: list[_Event] = []
        for event in raw_events:
            if self.events and self.events[-1].bar == event.bar:
                self.events[-1].merge(event)
            else:
                self.events.append(event)

        participants: list[tuple[float, bool]] = []
        for single in self.singles:
            participants.append((single.beat, not single.trace))
        for chain in self.chains:
            for index, point in enumerate(chain.points):
                if chain.decoration:
                    if chain.critical and index == 0:
                        continue
                    if point.ease != "linear":
                        participants.append((point.beat, True))
                    continue
                if point.kind in ("tick", "attach"):
                    participants.append((point.beat, False))
                elif point.kind in ("start", "end"):
                    if point.judge == "none":
                        continue
                    participants.append((point.beat, point.judge != "trace"))
        participants.sort(key=lambda p: p[0])
        self._tick_participants = participants

    # --- assets ---------------------------------------------------------------

    def _load_image(self, source: str) -> Image.Image | None:
        if source in self._images:
            return self._images[source]
        image: Image.Image | None = None
        try:
            if source.startswith(("http://", "https://")):
                request = urllib.request.Request(
                    source, headers={"User-Agent": "pjsekai-scores"}
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    image = Image.open(io.BytesIO(response.read())).convert("RGBA")
            else:
                image = Image.open(source).convert("RGBA")
        except Exception:
            image = None
        self._images[source] = image
        return image

    def _sprite(self, name: str) -> Image.Image | None:
        if name in _SPRITE_CACHE:
            return _SPRITE_CACHE[name]
        image: Image.Image | None = None
        try:
            image = Image.open(_ASSETS / name).convert("RGBA")
        except Exception:
            image = None
        _SPRITE_CACHE[name] = image
        return image

    def _scaled_image(
        self, name: str, width: int, height: int, flip: bool = False
    ) -> Image.Image | None:
        key = (name, width, height, flip)
        if key in _SCALED_CACHE:
            return _SCALED_CACHE[key]
        source = self._sprite(name)
        result: Image.Image | None = None
        if source is not None and width > 0 and height > 0:
            result = source.resize((width, height), Image.Resampling.LANCZOS)
            if flip:
                result = result.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        _SCALED_CACHE[key] = result
        return result

    def _font(self, size: int, weight: int) -> ImageFont.FreeTypeFont:
        file = str(_FONT_BLACK if weight >= 900 else _FONT_MEDIUM)
        key = (file, size)
        if key not in _FONT_CACHE:
            _FONT_CACHE[key] = ImageFont.truetype(file, size)
        return _FONT_CACHE[key]

    # --- note body tiles --------------------------------------------------------

    def _note_tile(self, sprite_no: int, width_units: int) -> Image.Image | None:
        key = (sprite_no, width_units)
        if key in _NOTE_TILE_CACHE:
            return _NOTE_TILE_CACHE[key]
        if not 1 <= width_units <= N_LANES:
            _NOTE_TILE_CACHE[key] = None
            return None

        tile_w = LANE_WIDTH * (width_units + 1)
        tile_h = round(LANE_WIDTH / 64 * 56 * 2)
        dy = (tile_h - NOTE_SIZE) / 2

        cap = NOTE_SIZE / 56 * 32
        middle = LANE_WIDTH * width_units - cap - 2
        pad = (tile_w - cap - middle - cap) / 2

        sprite_w = round(118 * 32 / 112)
        sprite_h = round(62 * 16 / 56)
        sprite_dx = -3 * 32 / 112
        sprite_dy = -3 * 16 / 56

        canvas = Image.new("RGBA", (tile_w, tile_h))

        def paint(image_name: str, x: float, w: int, clip: tuple[float, float]) -> None:
            image = self._scaled_image(image_name, w, sprite_h)
            if image is None:
                return
            layer = Image.new("RGBA", canvas.size)
            layer.paste(image, (round(x), round(dy + sprite_dy)))
            mask = Image.new("L", canvas.size, 0)
            ImageDraw.Draw(mask).rectangle(
                [
                    round(clip[0]),
                    round(dy),
                    round(clip[1]) - 1,
                    round(dy + NOTE_SIZE) - 1,
                ],
                fill=255,
            )
            layer.putalpha(
                Image.composite(
                    layer.getchannel("A"), Image.new("L", canvas.size, 0), mask
                )
            )
            canvas.alpha_composite(layer)

        sprite_name = f"notes_{sprite_no}.png"
        strip_name = f"notes_{sprite_no}_middle.png"
        paint(sprite_name, pad + sprite_dx, sprite_w, (pad, pad + cap))
        paint(
            strip_name,
            pad + cap,
            max(1, round(middle)),
            (pad + cap, pad + cap + middle),
        )
        paint(
            sprite_name,
            pad + cap + middle + cap - 32 + sprite_dx,
            sprite_w,
            (pad + cap + middle, pad + cap + middle + cap),
        )

        _NOTE_TILE_CACHE[key] = canvas
        return canvas

    # --- ribbon fills -------------------------------------------------------------

    def _coverage(self, points: np.ndarray) -> tuple[np.ndarray, int, int] | None:
        pts = np.asarray(points, dtype=np.float64)
        min_x = math.floor(pts[:, 0].min())
        max_x = math.ceil(pts[:, 0].max())
        min_y = math.floor(pts[:, 1].min())
        max_y = math.ceil(pts[:, 1].max())
        width = max_x - min_x
        height = max_y - min_y
        if width <= 0 or height <= 0:
            return None

        local = pts - (min_x, min_y)
        x0 = local[:, 0]
        y0 = local[:, 1]
        x1 = np.roll(x0, -1)
        y1 = np.roll(y0, -1)

        sloped = y0 != y1
        x0 = x0[sloped]
        y0 = y0[sloped]
        x1 = x1[sloped]
        y1 = y1[sloped]
        if len(y0) == 0:
            return None

        first = np.ceil(np.minimum(y0, y1) - 0.5).astype(np.int64)
        last = np.ceil(np.maximum(y0, y1) - 0.5).astype(np.int64)
        counts = np.maximum(last - first, 0)
        total = int(counts.sum())
        if total == 0:
            return None

        edge_index = np.repeat(np.arange(len(counts)), counts)
        offsets = np.concatenate(([0], np.cumsum(counts)[:-1]))
        rows = np.arange(total) - np.repeat(offsets, counts) + np.repeat(first, counts)
        t = (rows + 0.5 - y0[edge_index]) / (y1[edge_index] - y0[edge_index])
        xs = x0[edge_index] + t * (x1[edge_index] - x0[edge_index])

        keep = (rows >= 0) & (rows < height)
        rows = rows[keep]
        xs = np.clip(xs[keep], 0.0, float(width))
        order = np.lexsort((xs, rows))
        rows = rows[order]
        xs = xs[order]
        if len(rows) % 2:
            return None

        xa = xs[0::2]
        xb = xs[1::2]
        row = rows[0::2]

        acc = np.zeros((height, width + 2), dtype=np.float32)
        ka = np.floor(xa).astype(np.int64)
        kb = np.floor(xb).astype(np.int64)

        same = ka == kb
        np.add.at(acc, (row[same], ka[same]), (xb[same] - xa[same]))

        diff = ~same
        np.add.at(acc, (row[diff], ka[diff]), 1.0 - (xa[diff] - ka[diff]))
        np.add.at(acc, (row[diff], kb[diff]), xb[diff] - kb[diff])
        interior = np.zeros((height, width + 2), dtype=np.float32)
        np.add.at(interior, (row[diff], ka[diff] + 1), 1.0)
        np.add.at(interior, (row[diff], kb[diff]), -1.0)
        acc += np.cumsum(interior, axis=1)

        return np.clip(acc[:, :width], 0.0, 1.0), min_x, min_y

    def _fill_polygon(
        self,
        canvas: Image.Image,
        points: np.ndarray,
        fill: tuple[int, int, int, int] | None,
        gradient: tuple[tuple, tuple] | None,
    ) -> None:
        if len(points) < 3:
            return
        result = self._coverage(points)
        if result is None:
            return
        coverage, min_x, min_y = result
        height, width = coverage.shape

        tile = np.empty((height, width, 4), dtype=np.uint8)
        if gradient is not None:
            bottom, top = gradient
            ys = (np.arange(height) + 0.5) / height
            t = np.clip(1.0 - ys, 0.0, 1.0)
            color = np.asarray(bottom, dtype=np.float32) + np.outer(
                t,
                np.asarray(top, dtype=np.float32)
                - np.asarray(bottom, dtype=np.float32),
            )
            tile[:, :, :3] = color[:, None, :3].astype(np.uint8)
            tile[:, :, 3] = (coverage * color[:, None, 3] + 0.5).astype(np.uint8)
        else:
            assert fill is not None
            tile[:, :, 0] = fill[0]
            tile[:, :, 1] = fill[1]
            tile[:, :, 2] = fill[2]
            tile[:, :, 3] = (coverage * fill[3] + 0.5).astype(np.uint8)

        dest_x = min_x
        dest_y = min_y
        if dest_x < 0:
            tile = tile[:, -dest_x:]
            dest_x = 0
        if dest_y < 0:
            tile = tile[-dest_y:, :]
            dest_y = 0
        if tile.shape[0] <= 0 or tile.shape[1] <= 0:
            return
        if dest_x >= canvas.width or dest_y >= canvas.height:
            return
        canvas.alpha_composite(Image.fromarray(tile, "RGBA"), (dest_x, dest_y))

    # --- text --------------------------------------------------------------------

    def _draw_text(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        text: str,
        x: float,
        y: float,
        *,
        size: int,
        weight: int = 400,
        fill: tuple[int, int, int, int],
        anchor: str = "start",
        spacing: float = 0.0,
        rotate: tuple[float, float] | None = None,
    ) -> None:
        if not text:
            return
        font = self._font(size, weight)
        pil_anchor = {"start": "ls", "middle": "ms", "end": "rs"}[anchor]

        if rotate is None and not spacing:
            draw.text((x, y), text, font=font, fill=fill, anchor=pil_anchor)
            return

        key = (text, id(font), fill, spacing, rotate is not None)
        cached = self._text_tiles.get(key)
        if cached is not None:
            tile, tmp_w, tmp_h, length, baseline, pad = cached
        else:
            pad = int(font.size)
            if spacing:
                length = sum(font.getlength(c) + spacing for c in text)
            else:
                length = font.getlength(text)
            tmp_w = math.ceil(length) + pad * 2
            ascent, descent = font.getmetrics()
            tmp_h = ascent + descent + pad * 2
            tile = Image.new("RGBA", (tmp_w, tmp_h))
            tile_draw = ImageDraw.Draw(tile)
            baseline = pad + ascent
            if spacing:
                pen_x = float(pad)
                for ch in text:
                    tile_draw.text(
                        (pen_x, baseline), ch, font=font, fill=fill, anchor="ls"
                    )
                    pen_x += font.getlength(ch) + spacing
            else:
                tile_draw.text((pad, baseline), text, font=font, fill=fill, anchor="ls")
            if rotate is not None:
                tile = tile.rotate(90, expand=True)
            self._text_tiles[key] = (tile, tmp_w, tmp_h, length, baseline, pad)

        if anchor == "start":
            anchor_dx = 0.0
        elif anchor == "middle":
            anchor_dx = length / 2
        else:
            anchor_dx = length

        if rotate is None:
            canvas.alpha_composite(
                tile, (round(x - pad - anchor_dx), round(y - baseline))
            )
            return

        cx, cy = rotate
        local_x = x - pad - anchor_dx
        local_y = y - baseline
        dest_x = cx + (local_y - cy)
        dest_y = cy - (local_x + tmp_w - cx)
        canvas.alpha_composite(tile, (round(dest_x), round(dest_y)))

    # --- chart layout --------------------------------------------------------------

    def _n_bars(self) -> int:
        last = 0.0
        for single in self.singles:
            last = max(last, single.beat)
        for chain in self.chains:
            last = max(last, chain.points[-1].beat)
        return math.ceil(last)

    def render(self) -> Image.Image:
        if not self.singles and not self.chains:
            return Image.new("RGB", (1, 1), (255, 255, 255))

        n_bars = self._n_bars()
        ranges: list[tuple[int, int]] = []
        bar = 0
        for i in range(n_bars + 1):
            if bar != i and (i == bar + SENTENCE_LENGTH or i == n_bars):
                ranges.append((bar, i))
                bar = i

        heights = [
            round(
                TIME_HEIGHT
                * (self.timeline.time_of_bar(stop) - self.timeline.time_of_bar(start))
                + TIME_PADDING * 2
            )
            for start, stop in ranges
        ]
        sentence_w = round(LANE_WIDTH * N_LANES + LANE_PADDING * 2)
        chart_w = sentence_w * len(ranges)
        chart_h = max(heights)

        total_w = chart_w + LANE_PADDING * 2
        total_h = chart_h + TIME_PADDING * 2 + META_SIZE + TIME_PADDING * 2
        canvas = Image.new("RGBA", (total_w, total_h), WHITE)
        draw = ImageDraw.Draw(canvas, "RGBA")

        draw.rectangle(
            [0, 0, total_w - 1, chart_h + TIME_PADDING * 2 - 1], fill=BACKGROUND_FILL
        )
        meta_y = chart_h + TIME_PADDING * 2
        draw.rectangle([0, meta_y, total_w - 1, total_h - 1], fill=META_FILL)
        draw.rectangle([0, meta_y - 1, total_w - 1, meta_y], fill=LIGHT)

        self._draw_meta(canvas, draw, chart_h)

        x = LANE_PADDING
        for (start, stop), height in zip(ranges, heights):
            sentence = self._render_sentence(start, stop, sentence_w, height)
            canvas.alpha_composite(sentence, (x, chart_h - height + TIME_PADDING))
            x += sentence_w

        return canvas.convert("RGB")

    def _draw_meta(
        self, canvas: Image.Image, draw: ImageDraw.ImageDraw, chart_h: int
    ) -> None:
        jacket = None
        if self.jacket:
            jacket = self._load_image(self.jacket)
        if jacket is None:
            jacket = self._load_image(str(_ASSETS / "jacket_placeholder.png"))
        if jacket is not None:
            resized = jacket.resize((META_SIZE, META_SIZE), Image.Resampling.LANCZOS)
            canvas.alpha_composite(
                resized, (LANE_PADDING * 2, chart_h + TIME_PADDING * 3)
            )

        title = " - ".join(filter(None, [self.title, self.artist])) or "Untitled"
        self._draw_text(
            canvas,
            draw,
            title,
            LANE_PADDING * 4 + META_SIZE,
            META_SIZE + chart_h + TIME_PADDING * 3 - 16,
            size=96,
            weight=900,
            fill=WHITE,
        )
        subtitle = " ".join(
            filter(
                None,
                [
                    self.difficulty and str(self.difficulty).upper(),
                    self.playlevel,
                    "Chart drawn by sbuga.com",
                ],
            )
        )
        self._draw_text(
            canvas,
            draw,
            subtitle,
            LANE_PADDING * 4 + META_SIZE,
            META_SIZE // 3 + chart_h + TIME_PADDING * 3 - 8,
            size=48,
            weight=700,
            fill=WHITE,
        )

    # --- sentence rendering -----------------------------------------------------------

    def _render_sentence(
        self, bar_start: int, bar_stop: int, width: int, height: int
    ) -> Image.Image:
        canvas = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(canvas, "RGBA")
        stop_time = self.timeline.time_of_bar(bar_stop)

        def bar_y(bar: float) -> float:
            return (
                TIME_HEIGHT * (stop_time - self.timeline.time_of_bar(bar))
                + TIME_PADDING
            )

        draw.rectangle([0, 0, width - 1, height - 1], fill=BACKGROUND_FILL)
        draw.rectangle(
            [LANE_PADDING, 0, LANE_PADDING + LANE_WIDTH * N_LANES - 1, height - 1],
            fill=LANE_FILL,
        )

        lane_right = LANE_PADDING + LANE_WIDTH * N_LANES - 1
        for lane in range(0, N_LANES + 1, 2):
            x = LANE_WIDTH * lane + LANE_PADDING
            draw.rectangle([x, 0, x, height - 1], fill=LIGHT)

        for bar in range(bar_start, bar_stop + 1):
            y = round(bar_y(bar))
            draw.rectangle([LANE_PADDING, y - 2, lane_right, y + 1], fill=LIGHT)
            length = self.timeline.bar_length_at(bar)
            for i in range(1, math.ceil(length)):
                sub = _row1(round(bar_y(bar + i / length)))
                draw.rectangle([LANE_PADDING, sub, lane_right, sub], fill=LIGHT)

        visible_chains = [
            chain
            for chain in self.chains
            if self._chain_visible(chain, bar_start, bar_stop)
        ]
        self._draw_events(canvas, draw, bar_start, bar_stop, bar_y)
        amongs = self._draw_chains(canvas, visible_chains, bar_y)
        self._draw_notes(
            canvas, draw, visible_chains, bar_start, bar_stop, bar_y, amongs
        )
        self._draw_ticks(canvas, draw, bar_start, bar_stop, bar_y)
        return canvas

    def _draw_events(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        bar_start: int,
        bar_stop: int,
        bar_y,
    ) -> None:
        lane_right = LANE_PADDING + LANE_WIDTH * N_LANES - 1
        synthesized = [_Event(bar=float(i)) for i in range(bar_start, bar_stop + 1)]
        merged: list[_Event] = []
        for event in sorted(synthesized + self.events, key=lambda e: e.bar):
            if event.speed is not None:
                y = round(bar_y(event.bar))
                row = _row1(y)
                draw.rectangle([LANE_PADDING, row, lane_right, row], fill=MAGENTA)
                self._draw_text(
                    canvas,
                    draw,
                    "%gx" % event.speed,
                    LANE_WIDTH * N_LANES + LANE_PADDING - 2,
                    y - 2,
                    size=12,
                    fill=MAGENTA,
                    anchor="end",
                )
                continue

            if merged and event.bar - merged[-1].bar <= 1 / 16:
                merged[-1].merge(event)
            else:
                merged.append(
                    _Event(
                        bar=event.bar,
                        bpm=event.bpm,
                        bar_length=event.bar_length,
                        text=event.text,
                    )
                )

            y = round(bar_y(event.bar))
            flag = YELLOW if event.special else WHITE
            draw.rectangle([0, y - 2, LANE_PADDING - 1, y + 1], fill=flag)

        for event in merged:
            if not bar_start - 1 <= event.bar < bar_stop + 1:
                continue
            pieces = [
                "#%g" % event.bar if int(event.bar) == event.bar else None,
                "%g BPM" % event.bpm if event.bpm else None,
                "%g/4" % event.bar_length if event.bar_length else None,
                event.text,
            ]
            text = ", ".join(p for p in pieces if p)
            if not text:
                continue
            y = bar_y(event.bar)
            self._draw_text(
                canvas,
                draw,
                text,
                LANE_PADDING + 8,
                round(y - LANE_WIDTH * 1.5),
                size=12,
                weight=900,
                fill=YELLOW if event.special else WHITE,
                spacing=4 if event.special else 0,
                rotate=(LANE_PADDING, round(y)),
            )

    def _chain_visible(self, chain: _Chain, bar_start: int, bar_stop: int) -> bool:
        lower = bar_start - 1
        upper = bar_stop + 1
        before = False
        for point in chain.points:
            if point.kind == "attach":
                continue
            if lower <= point.beat < upper:
                return True
            if point.beat < lower:
                before = True
            elif before and point.beat > upper:
                return True
        return False

    def _draw_chains(
        self,
        canvas: Image.Image,
        visible_chains: list[_Chain],
        bar_y,
    ) -> list[tuple[float, float, bool]]:
        amongs: list[tuple[float, float, bool]] = []

        for chain in visible_chains:
            padding = 0 if chain.decoration else SLIDE_PATH_PADDING
            path_indices = [i for i, p in enumerate(chain.points) if p.kind != "attach"]
            if len(path_indices) < 2:
                continue

            chunks: list[np.ndarray] = []
            right_curves: list[tuple] = []
            for seg, (i0, i1) in enumerate(zip(path_indices, path_indices[1:])):
                p0 = chain.points[i0]
                p1 = chain.points[i1]
                y0 = bar_y(p0.beat)
                y1 = bar_y(p1.beat)
                ease_in = p0.ease == "in"
                ease_out = p0.ease == "out"

                lx0 = LANE_WIDTH * (p0.lane - 2) + LANE_PADDING - padding
                lx1 = LANE_WIDTH * (p1.lane - 2) + LANE_PADDING - padding
                rx0 = LANE_WIDTH * (p0.lane - 2 + p0.width) + LANE_PADDING + padding
                rx1 = LANE_WIDTH * (p1.lane - 2 + p1.width) + LANE_PADDING + padding

                left = (
                    (lx0, y0),
                    (lx0, (y0 + y1) / 2 if ease_in else y0),
                    (lx1, (y0 + y1) / 2 if ease_out else y1),
                    (lx1, y1),
                )
                right = (
                    (rx0, y0),
                    (rx0, (y0 + y1) / 2 if ease_in else y0),
                    (rx1, (y0 + y1) / 2 if ease_out else y1),
                    (rx1, y1),
                )
                right_curves.append(right)

                rounded_left = tuple((round(px), round(py)) for px, py in left)
                if seg == 0:
                    chunks.append(np.array([rounded_left[0]], dtype=np.float64))
                chunks.append(_flatten_bezier(rounded_left))

                for j in range(i0 + 1, i1 + 1):
                    between = chain.points[j]
                    if between.kind in ("tick", "attach"):
                        y = bar_y(between.beat)
                        x_l = _binary_solution_for_x(y, left)
                        x_r = _binary_solution_for_x(y, right)
                        amongs.append(((x_l + x_r) / 2, y, chain.critical))

            for seg, right in enumerate(reversed(right_curves)):
                rounded = tuple((round(px), round(py)) for px, py in right)
                if seg == 0:
                    chunks.append(np.array([rounded[3]], dtype=np.float64))
                chunks.append(
                    _flatten_bezier((rounded[3], rounded[2], rounded[1], rounded[0]))
                )
            polygon = np.concatenate(chunks)

            if chain.decoration:
                gradient = (
                    DECORATION_CRITICAL_STOPS if chain.critical else DECORATION_STOPS
                )
                self._fill_polygon(canvas, polygon, None, gradient)
            else:
                fill = SLIDE_CRITICAL_FILL if chain.critical else SLIDE_FILL
                self._fill_polygon(canvas, polygon, fill, None)

        return amongs

    def _draw_notes(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        visible_chains: list[_Chain],
        bar_start: int,
        bar_stop: int,
        bar_y,
        amongs: list[tuple[float, float, bool]],
    ) -> None:
        lower = bar_start - 1
        upper = bar_stop + 1

        bodies: list[tuple[float, float, float, int]] = []
        frictions: list[tuple[float, float, float, str]] = []
        flicks: list[tuple[float, float, float, str, bool]] = []

        def friction_name(critical: bool, flick: bool) -> str:
            if critical:
                return "notes_friction_among_crtcl.png"
            return (
                "notes_friction_among_flick.png"
                if flick
                else "notes_friction_among_long.png"
            )

        for single in self.singles:
            if not lower <= single.beat < upper:
                continue
            if single.trace:
                sprite = 5 if single.critical else 6 if single.direction else 4
                frictions.append(
                    (
                        single.beat,
                        single.lane,
                        single.width,
                        friction_name(single.critical, bool(single.direction)),
                    )
                )
            else:
                sprite = 0 if single.critical else 3 if single.direction else 2
            bodies.append((single.beat, single.lane, single.width, sprite))
            if single.direction:
                flicks.append(
                    (
                        single.beat,
                        single.lane,
                        single.width,
                        single.direction,
                        single.critical,
                    )
                )

        for chain in visible_chains:
            if chain.decoration:
                continue
            for point in chain.points:
                if point.kind not in ("start", "end"):
                    continue
                if not lower <= point.beat < upper:
                    continue
                if point.judge == "none":
                    continue
                critical = point.critical or chain.critical
                if point.judge == "trace":
                    sprite = 5 if critical else 4
                    frictions.append(
                        (
                            point.beat,
                            point.lane,
                            point.width,
                            friction_name(critical, False),
                        )
                    )
                elif critical:
                    sprite = 0
                elif point.kind == "end" and point.direction:
                    sprite = 3
                else:
                    sprite = 1
                bodies.append((point.beat, point.lane, point.width, sprite))
                if point.kind == "end" and point.direction:
                    flicks.append(
                        (point.beat, point.lane, point.width, point.direction, critical)
                    )

        bodies.sort(key=lambda b: b[0])
        h = LANE_WIDTH / 64 * 56 * 2
        for beat, lane, note_width, sprite in bodies:
            y = bar_y(beat)
            x = LANE_WIDTH * (lane - 2.5) + LANE_PADDING
            tile = self._note_tile(sprite, round(note_width))
            if tile is not None:
                canvas.alpha_composite(tile, (round(x), round(y - h / 2)))

        for x, y, critical in amongs:
            w = LANE_WIDTH
            name = "notes_long_among_crtcl.png" if critical else "notes_long_among.png"
            image = self._scaled_image(name, round(w), round(w))
            if image is not None:
                canvas.alpha_composite(image, (round(x - w / 2), round(y - w / 2)))

        for beat, lane, note_width, name in frictions:
            y = bar_y(beat)
            x = LANE_WIDTH * (lane + note_width / 2 - 2) + LANE_PADDING
            w = LANE_WIDTH * 0.75
            image = self._scaled_image(name, round(w), round(w))
            if image is not None:
                canvas.alpha_composite(image, (round(x - w / 2), round(y - w / 2)))

        flicks.sort(key=lambda f: f[0])
        for beat, lane, note_width, direction, critical in reversed(flicks):
            width_units = min(int(note_width), 6)
            h0 = FLICK_HEIGHT
            arrow_h = h0 * ((width_units + 3) / 3) ** 0.75
            arrow_w = h0 * 1.5 * ((width_units + 0.5) / 3) ** 0.75
            x = LANE_WIDTH * (lane - 2 + note_width / 2) + LANE_PADDING
            bias = (
                -NOTE_SIZE / 4
                if direction == "left"
                else NOTE_SIZE / 4 if direction == "right" else 0
            )
            diagonal = direction in ("left", "right")
            name = "notes_flick_arrow%s_0%d%s.png" % (
                "_crtcl" if critical else "",
                width_units,
                "_diagonal" if diagonal else "",
            )
            image = self._scaled_image(
                name, round(arrow_w), round(arrow_h), flip=direction == "right"
            )
            if image is None:
                continue
            y = bar_y(beat)
            dest_x = round(x - arrow_w / 2 + bias)
            if direction == "right":
                origin = round(x + bias)
                dest_x = 2 * origin - (dest_x + round(arrow_w))
            canvas.alpha_composite(image, (dest_x, round(y + NOTE_SIZE / 4 - arrow_h)))

    def _draw_ticks(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        bar_start: int,
        bar_stop: int,
        bar_y,
    ) -> None:
        participants = self._tick_participants
        lower = bar_start - 1
        upper = bar_stop + 1
        for i, (beat, is_tick) in enumerate(participants):
            if not lower <= beat < upper:
                continue
            y = round(bar_y(beat))
            row = _row1(y)

            if not is_tick:
                draw.rectangle(
                    [LANE_PADDING - TICK_2_LENGTH, row, LANE_PADDING - 1, row],
                    fill=LIGHT,
                )
                continue

            next_beat = beat
            for j in range(i + 1, len(participants)):
                if participants[j][1] and participants[j][0] > beat:
                    next_beat = participants[j][0]
                    break

            if (
                next_beat == beat
                or next_beat - beat > 1
                or (next_beat - beat > 0.5 and int(next_beat) != int(beat))
            ):
                interval = math.floor(beat + 1) - beat
            else:
                interval = next_beat - beat

            interval *= self.timeline.bar_length_at(beat) / 4
            fraction = _FRACTION_CACHE.get(interval)
            if fraction is None:
                fraction = Fraction(interval).limit_denominator(100)
                _FRACTION_CACHE[interval] = fraction
            if fraction == 0:
                continue

            draw.rectangle(
                [LANE_PADDING - TICK_LENGTH, row, LANE_PADDING - 1, row], fill=LIGHT
            )
            text = (
                "%g/%g" % (fraction.numerator, fraction.denominator)
                if fraction.numerator != 1
                else "/%g" % fraction.denominator
            )
            self._draw_text(
                canvas,
                draw,
                text,
                LANE_PADDING - 4,
                y - 2,
                size=12,
                fill=LIGHT,
                anchor="end",
            )


def render_score(score: Score, **kwargs) -> Image.Image:
    return ChartRenderer(score, **kwargs).render()
