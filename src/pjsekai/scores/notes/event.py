import dataclasses

from .base import BaseNote


@dataclasses.dataclass
class Event(BaseNote):
    bpm: float = None
    bar_length: int = None
    sentence_length: int = None
    speed: float = None

    section: str = None
    text: str = None

    def __hash__(self) -> int:
        return hash(str(self))

    def __or__(self, other: 'Event'):
        assert self.bar <= other.bar
        return Event(
            bar=other.bar,
            bpm=other.bpm or self.bpm,
            bar_length=other.bar_length or self.bar_length,
            sentence_length=other.sentence_length or self.sentence_length,
            speed=other.speed or self.speed,
            section=other.section or self.section,
            text=other.text or self.text,
        )
