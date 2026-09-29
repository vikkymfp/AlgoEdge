"""Login CAPTCHA: 6 characters from A-Z and 0-9, checked only on the server.

The answer is generated with `secrets`, rendered to a PNG on the server and
kept only as a SHA-256 digest keyed by a random, unguessable CAPTCHA id. The
browser receives the id and the image - never the text - so the answer is
in no HTML, JavaScript, cookie or localStorage. Each CAPTCHA expires, and
the first verification attempt consumes it whatever the outcome, so it can
be neither replayed nor brute-forced (one guess per image; issuing new
images is rate-limited per client by the caller).

The PNG is drawn with the standard library only (zlib + a 5x7 bitmap font),
so no imaging dependency is added.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
LENGTH = 6
MAX_STORED = 10_000  # outstanding CAPTCHAs kept at most (oldest dropped first)

# 5x7 glyphs, one string per row ("#" = ink).
_FONT = {
    "A": [" ### ", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"],
    "B": ["#### ", "#   #", "#   #", "#### ", "#   #", "#   #", "#### "],
    "C": [" ### ", "#   #", "#    ", "#    ", "#    ", "#   #", " ### "],
    "D": ["#### ", "#   #", "#   #", "#   #", "#   #", "#   #", "#### "],
    "E": ["#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#####"],
    "F": ["#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#    "],
    "G": [" ### ", "#   #", "#    ", "# ###", "#   #", "#   #", " ####"],
    "H": ["#   #", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"],
    "I": [" ### ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "],
    "J": ["  ###", "   # ", "   # ", "   # ", "   # ", "#  # ", " ##  "],
    "K": ["#   #", "#  # ", "# #  ", "##   ", "# #  ", "#  # ", "#   #"],
    "L": ["#    ", "#    ", "#    ", "#    ", "#    ", "#    ", "#####"],
    "M": ["#   #", "## ##", "# # #", "# # #", "#   #", "#   #", "#   #"],
    "N": ["#   #", "##  #", "# # #", "#  ##", "#   #", "#   #", "#   #"],
    "O": [" ### ", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "],
    "P": ["#### ", "#   #", "#   #", "#### ", "#    ", "#    ", "#    "],
    "Q": [" ### ", "#   #", "#   #", "#   #", "# # #", "#  # ", " ## #"],
    "R": ["#### ", "#   #", "#   #", "#### ", "# #  ", "#  # ", "#   #"],
    "S": [" ####", "#    ", "#    ", " ### ", "    #", "    #", "#### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  "],
    "U": ["#   #", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "],
    "V": ["#   #", "#   #", "#   #", "#   #", "#   #", " # # ", "  #  "],
    "W": ["#   #", "#   #", "#   #", "# # #", "# # #", "# # #", " # # "],
    "X": ["#   #", "#   #", " # # ", "  #  ", " # # ", "#   #", "#   #"],
    "Y": ["#   #", "#   #", " # # ", "  #  ", "  #  ", "  #  ", "  #  "],
    "Z": ["#####", "    #", "   # ", "  #  ", " #   ", "#    ", "#####"],
    "0": [" ### ", "#   #", "#  ##", "# # #", "##  #", "#   #", " ### "],
    "1": ["  #  ", " ##  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "],
    "2": [" ### ", "#   #", "    #", "   # ", "  #  ", " #   ", "#####"],
    "3": ["#### ", "    #", "    #", " ### ", "    #", "    #", "#### "],
    "4": ["   # ", "  ## ", " # # ", "#  # ", "#####", "   # ", "   # "],
    "5": ["#####", "#    ", "#### ", "    #", "    #", "#   #", " ### "],
    "6": [" ### ", "#    ", "#    ", "#### ", "#   #", "#   #", " ### "],
    "7": ["#####", "    #", "   # ", "  #  ", " #   ", " #   ", " #   "],
    "8": [" ### ", "#   #", "#   #", " ### ", "#   #", "#   #", " ### "],
    "9": [" ### ", "#   #", "#   #", " ####", "    #", "    #", " ### "],
}

_SCALE = 4
_CELL_W = 5 * _SCALE + 10  # glyph plus spacing
_WIDTH = LENGTH * _CELL_W + 16
_HEIGHT = 7 * _SCALE + 24


def generate_text() -> str:
    """Exactly LENGTH characters, each drawn uniformly from A-Z0-9 with a CSPRNG."""
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("ascii")).digest()


def render_png(text: str) -> bytes:
    """A small greyscale-on-dark PNG of `text`: per-character jitter plus
    noise dots and lines, drawn with the standard library only."""
    rng = secrets.SystemRandom()
    pixels = [[18 + rng.randrange(14) for _ in range(_WIDTH)] for _ in range(_HEIGHT)]
    for _ in range(_WIDTH * _HEIGHT // 9):  # speckle
        pixels[rng.randrange(_HEIGHT)][rng.randrange(_WIDTH)] = 60 + rng.randrange(90)
    for position, char in enumerate(text):
        x0 = 8 + position * _CELL_W + rng.randrange(-2, 3)
        y0 = 12 + rng.randrange(-5, 6)
        shade = 190 + rng.randrange(66)
        for row, line in enumerate(_FONT[char]):
            for column, mark in enumerate(line):
                if mark != "#":
                    continue
                for dy in range(_SCALE):
                    for dx in range(_SCALE):
                        y, x = y0 + row * _SCALE + dy, x0 + column * _SCALE + dx
                        if 0 <= y < _HEIGHT and 0 <= x < _WIDTH:
                            pixels[y][x] = shade
    for _ in range(4):  # strike-through lines
        y, slope = rng.randrange(_HEIGHT), rng.uniform(-0.35, 0.35)
        shade = 120 + rng.randrange(100)
        for x in range(_WIDTH):
            yy = int(y + slope * x)
            if 0 <= yy < _HEIGHT:
                pixels[yy][x] = shade
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", _WIDTH, _HEIGHT, 8, 0, 0, 0, 0)  # 8-bit greyscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


@dataclass
class _Entry:
    digest: bytes
    expires_at: datetime


class CaptchaStore:
    """Outstanding CAPTCHAs, in server memory only. Thread-safe."""

    def __init__(self, ttl: timedelta, clock: Callable[[], datetime]) -> None:
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def issue(self) -> dict[str, object]:
        """A new CAPTCHA: what the browser may see (id, image, expiry) - never the text."""
        text = generate_text()
        captcha_id = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            self._prune(now)
            while len(self._entries) >= MAX_STORED:
                self._entries.pop(next(iter(self._entries)))
            self._entries[captcha_id] = _Entry(_digest(text), now + self._ttl)
        image = "data:image/png;base64," + base64.b64encode(render_png(text)).decode("ascii")
        return {"captchaId": captcha_id, "image": image, "expiresInSeconds": int(self._ttl.total_seconds())}

    def verify_and_consume(self, captcha_id: object, answer: object) -> bool:
        """True only for an unexpired id whose answer matches. The id is
        removed on every call, right or wrong - one guess per CAPTCHA."""
        if not isinstance(captcha_id, str) or not isinstance(answer, str):
            return False
        with self._lock:
            entry = self._entries.pop(captcha_id, None)
        if entry is None or self._clock() >= entry.expires_at:
            return False
        candidate = answer.strip().upper()
        if len(candidate) != LENGTH or any(char not in ALPHABET for char in candidate):
            return False
        return hmac.compare_digest(_digest(candidate), entry.digest)

    def _prune(self, now: datetime) -> None:
        for captcha_id in [key for key, entry in self._entries.items() if now >= entry.expires_at]:
            del self._entries[captcha_id]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
