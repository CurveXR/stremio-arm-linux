"""Fetch subtitles from subtitle addons and parse SRT / WebVTT into timed cues."""

import bisect
import html
import re

from gi.repository import GLib, Pango

from .util import session

LANGS = {
    "eng": "English", "spa": "Spanish", "fre": "French", "fra": "French", "ger": "German", "deu": "German",
    "ita": "Italian", "por": "Portuguese", "pob": "Portuguese (BR)", "dut": "Dutch", "nld": "Dutch",
    "pol": "Polish", "rus": "Russian", "tur": "Turkish", "ara": "Arabic", "heb": "Hebrew", "gre": "Greek",
    "ell": "Greek", "swe": "Swedish", "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "cze": "Czech",
    "ces": "Czech", "hun": "Hungarian", "rum": "Romanian", "ron": "Romanian", "bul": "Bulgarian",
    "hrv": "Croatian", "srp": "Serbian", "slv": "Slovenian", "ukr": "Ukrainian", "chi": "Chinese",
    "zho": "Chinese", "jpn": "Japanese", "kor": "Korean", "vie": "Vietnamese", "tha": "Thai",
    "ind": "Indonesian", "may": "Malay", "hin": "Hindi", "per": "Persian", "fas": "Persian",
}

_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_TAG = re.compile(r"\{\\[^}]*\}")


def lang_name(code):
    return LANGS.get((code or "").lower(), code or "Unknown")


def _ts(text):
    m = _TIME.search(text)
    if not m:
        return None
    h, mi, s, ms = m.groups()
    return ((int(h or 0) * 60 + int(mi)) * 60 + int(s)) * 1_000_000 + int(ms.ljust(3, "0")) * 1000


def _markup(lines):
    text = html.escape(_TAG.sub("", "\n".join(lines)))
    # Allow the handful of inline tags SRT files use; drop the rest (font, c.class, …).
    plain = re.sub(r"&lt;/?[a-z][^&]*?&gt;", "", text, flags=re.I)
    styled = re.sub(r"&lt;(/?)([ibu])&gt;", r"<\1\2>", text, flags=re.I)
    styled = re.sub(r"&lt;/?[a-z][^&]*?&gt;", "", styled, flags=re.I)
    try:
        Pango.parse_markup(styled, -1, "\0")  # files often leave <i> unclosed
        return styled
    except GLib.Error:
        return plain


def parse(data):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.replace("\r\n", "\n")):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        for i, ln in enumerate(lines):
            if "-->" in ln:
                a, b = ln.split("-->", 1)
                start, end = _ts(a), _ts(b)
                if start is not None and end is not None and lines[i + 1:]:
                    cues.append((start, end, _markup(lines[i + 1:])))
                break
    cues.sort()
    return Track(cues)


class Track:
    def __init__(self, cues):
        self.cues = cues
        self.starts = [c[0] for c in cues]

    def at(self, us):
        """Markup for the cue showing at `us` microseconds, or ''."""
        i = bisect.bisect_right(self.starts, us) - 1
        out = []
        while i >= 0 and len(out) < 3:
            start, end, text = self.cues[i]
            if start <= us < end:
                out.insert(0, text)
            elif end < us - 30_000_000:
                break
            i -= 1
        return "\n".join(out)


def download(url):
    r = session().get(url, timeout=30)
    r.raise_for_status()
    return parse(r.content)


# ---- matching subtitles to the exact file ---------------------------------

_CHUNK = 65536


def video_hash(url, timeout=30):
    """OpenSubtitles hash (size + 64-bit sums of the first and last 64 KiB) via two range requests.
    Returns (hash_hex, size). Blocking."""
    import struct

    s = session()
    r = s.get(url, headers={"Range": f"bytes=0-{_CHUNK - 1}"}, timeout=timeout)
    r.raise_for_status()
    rng = r.headers.get("Content-Range", "")
    if r.status_code != 206 or "/" not in rng:
        raise ValueError("server doesn't support range requests")
    size = int(rng.rsplit("/", 1)[1])
    head = r.content[:_CHUNK]
    tail = s.get(r.url, headers={"Range": f"bytes={max(0, size - _CHUNK)}-{size - 1}"}, timeout=timeout).content
    h = size
    for chunk in (head, tail):
        chunk = chunk[: len(chunk) - len(chunk) % 8]
        for (q,) in struct.iter_unpack("<Q", chunk):
            h = (h + q) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}", size


def _tokens(name):
    return {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t and len(t) > 1}


def release_group(name):
    m = re.search(r"-([A-Za-z0-9]+)(?:\[[^\]]*\])?(?:\.[a-z0-9]{2,4})?$", (name or "").strip())
    return m.group(1) if m else None


def match_score(sub, filename):
    """How well a subtitle fits the file being played (0–100). OpenSubtitles marks hash matches
    with m='h'; otherwise compare the release it was timed for with the filename."""
    if sub.get("m") == "h":
        return 100
    if not filename:
        return 0
    want, have = _tokens(filename), _tokens(sub.get("movieReleaseName") or sub.get("subtitleFileName"))
    score = 50 * len(want & have) / len(want | have) if want and have else 0
    group = release_group(filename)
    if group and (sub.get("releaseGroup") or "").lower() == group.lower():
        score += 40
    return min(99, score)
