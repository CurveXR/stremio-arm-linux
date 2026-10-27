"""Pick the best stream for this device from an addon's stream list.

Built around what Torrentio sends (name "[RD+] Torrentio\\n1080p", a title with the release
name, "👤 seeders 💾 size", language flags, and behaviorHints.bingeGroup), but any addon's
streams get a sensible score from the same text.
"""

import math
import re
from dataclasses import dataclass, field

from . import codecs

# Language preference (ISO 639-2, as used for subtitles) → Torrentio's flag emoji.
FLAGS = {"eng": "🇬🇧", "spa": "🇪🇸", "fre": "🇫🇷", "ger": "🇩🇪", "ita": "🇮🇹", "por": "🇵🇹", "pob": "🇧🇷",
         "dut": "🇳🇱", "pol": "🇵🇱", "rus": "🇷🇺", "tur": "🇹🇷", "ara": "🇸🇦", "jpn": "🇯🇵", "kor": "🇰🇷",
         "chi": "🇨🇳", "hin": "🇮🇳", "swe": "🇸🇪", "dan": "🇩🇰", "nor": "🇳🇴", "fin": "🇫🇮", "cze": "🇨🇿",
         "hun": "🇭🇺", "rum": "🇷🇴", "gre": "🇬🇷", "heb": "🇮🇱", "ukr": "🇺🇦"}
_FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
_RES = [(r"2160p|\b4k\b|\buhd\b", 2160), (r"1080p|\bfhd\b", 1080), (r"720p", 720),
        (r"576p|480p|\bsd\b|\bdvdrip\b", 480), (r"360p|400p", 360)]
_SIZE = re.compile(r"💾\s*([\d.]+)\s*(TB|GB|MB)", re.I)
_SEEDS = re.compile(r"👤\s*(\d+)")
_PACK = re.compile(r"S\d+E\d+\s*-\s*E?\d+|\bS\d+\s*-\s*S?\d+\b|Season\s*\d+\s*-\s*\d+|Сезон|Серии|"
                   r"\bcomplete\b|\bS\d{2}\b(?!E)", re.I)
_LOW_QUALITY = re.compile(r"\b(CAM|CAMRip|HDCAM|TS|HDTS|TELESYNC|TC|TELECINE|SCR|SCREENER)\b")
MIN_SCORE = -20  # below this the "best" stream is probably unwatchable here; let the user choose
_CACHED = re.compile(r"\[(RD|AD|PM|DL|TB|ED|OC|PP)\+\]")
_UNCACHED = re.compile(r"\[(RD|AD|PM|DL|TB|ED|OC|PP) download\]", re.I)


@dataclass
class Ranked:
    addon: object
    stream: dict
    score: float
    resolution: int = 0
    instant: bool = False
    needs: list = field(default_factory=list)
    tags: list = field(default_factory=list)   # short human labels, best first
    codec: str = None
    hdr: bool = False
    size_gb: float = None
    seeds: int = None
    uncached: bool = False
    foreign: bool = False
    pack: bool = False
    release: str = ""
    source: str = None

    @property
    def label(self):
        return " · ".join(self.tags)

    @property
    def binge_group(self):
        return (self.stream.get("behaviorHints") or {}).get("bingeGroup")


def _text(stream):
    hints = stream.get("behaviorHints") or {}
    return "\n".join(filter(None, (stream.get("name"), stream.get("title"), stream.get("description"),
                                   hints.get("filename"))))


def _size_gb(text):
    m = _SIZE.search(text)
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).upper()
    return value * {"TB": 1024, "GB": 1, "MB": 1 / 1024}[unit]


def release_group(stream):
    """Release group from Torrentio's bingeGroup ("torrentio|1080p|ETHEL") or the file name."""
    group = ((stream.get("behaviorHints") or {}).get("bingeGroup") or "").split("|")[-1]
    if group and not re.fullmatch(r"[0-9a-f]{40}", group) and not group.startswith("torrentio"):
        return group
    name = (stream.get("behaviorHints") or {}).get("filename") or (stream.get("title") or "").split("\n")[0]
    m = re.search(r"-([A-Za-z0-9]+)(?:\[[^\]]*\])?(?:\.[a-z0-9]{2,4})?$", name.strip())
    return m.group(1) if m else None


def features(r):
    """What a learned preference can latch onto: resolution, codec, release group, source."""
    out = []
    if r.resolution:
        out.append(f"res:{r.resolution}")
    if r.codec:
        out.append(f"codec:{r.codec}")
    if (group := release_group(r.stream)):
        out.append(f"group:{group.lower()}")
    for src in ("WEB", "BluRay", "HDTV", "WEBRip"):
        if re.search(rf"\b{src}\b", r.release, re.I):
            out.append(f"src:{src.lower()}")
            break
    return out


def preference_bonus(r, prefs):
    if not prefs:
        return 0.0
    bonus = sum(min(8.0, 4 * math.log2(1 + prefs.get(f, 0))) for f in features(r))
    return min(20.0, bonus)


def rank_one(addon, stream, *, kind, lang="eng", binge_group=None):
    text = _text(stream)
    first_line = (stream.get("title") or stream.get("description") or "").split("\n", 1)[0]
    score, tags = 0.0, []

    res = next((r for pattern, r in _RES if re.search(pattern, text, re.I)), 0)
    score += {2160: -35, 1080: 30, 720: 18, 480: 0, 360: -10, 0: 5}[res]
    if res:
        tags.append("4K" if res == 2160 else f"{res}p")

    # Software decoding on this machine: HDR / Dolby Vision get no tone mapping and look washed
    # out, 10-bit and VVC are slow, so prefer plain 8-bit H.264 / HEVC.
    hdr = re.search(r"\bHDR|\bDV\b|Dolby.?Vision", text, re.I)
    if hdr:
        score -= 25
        tags.append("HDR")
    if re.search(r"10.?bit", text, re.I) and not hdr:
        score -= 8
    if re.search(r"\b(VVC|H\.?266)\b", text, re.I):
        score -= 60
    codec = None
    if re.search(r"\b(x265|HEVC|H\.?265)\b", text, re.I):
        codec = "HEVC"
    elif re.search(r"\b(x264|AVC|H\.?264)\b", text, re.I):
        codec = "H.264"
    elif re.search(r"\bAV1\b", text, re.I):
        codec = "AV1"
    elif re.search(r"\b(XviD|DivX)\b", text, re.I):
        codec = "XviD"
    if codec in ("HEVC", "H.264"):
        tags.append(codec)
    if re.search(r"\bREMUX\b", text, re.I):
        score -= 15  # huge files, no benefit on this screen

    needs = codecs.guess_missing(text)
    if needs:
        score -= 80

    instant = bool(_CACHED.search(text)) or (bool(stream.get("url")) and not _UNCACHED.search(text))
    uncached = bool(_UNCACHED.search(text))
    seeds = int(m.group(1)) if (m := _SEEDS.search(text)) else None
    if uncached:
        score -= 30
    elif instant:
        score += 40
        tags.insert(0, "Instant")
    if stream.get("infoHash") and not stream.get("url"):
        seeds = int(m.group(1)) if (m := _SEEDS.search(text)) else 0
        score += min(30, 8 * math.log10(1 + seeds)) - (40 if seeds == 0 else 0)
    elif (m := _SEEDS.search(text)):
        score += min(6, 2 * math.log10(1 + int(m.group(1))))  # popularity hint even when cached

    size = _size_gb(text)
    if size is not None:
        sweet = (0.25, 4) if kind == "series" else (1.2, 15)
        if size < sweet[0]:
            score -= 12
        elif size > sweet[1]:
            score -= min(25, 5 * math.log2(size / sweet[1]) + 5)
        tags.append(f"{size:.1f} GB" if size >= 1 else f"{size * 1024:.0f} MB")

    pack = bool(_PACK.search(first_line))
    if kind == "series" and pack:
        score -= 10  # whole-season bundles resolve slower and often mislabel files
    if _LOW_QUALITY.search(first_line):
        score -= 50

    flags = _FLAG.findall(text)
    want = FLAGS.get(lang, "🇬🇧")
    foreign = (flags and want not in flags and "multi" not in text.lower()) or re.search(
        "[а-яА-ЯёЁ]", first_line)
    if foreign:
        score -= 35
        tags.append("Other language")

    if binge_group and stream.get("behaviorHints", {}).get("bingeGroup") == binge_group:
        score += 25  # same release you were watching: consistent quality and usually cached
        tags.insert(0, "Same release")

    source = m.group(1).strip() if (m := re.search(r"⚙️\s*([^\n]+)", text)) else None
    return Ranked(addon, stream, score, res, instant, needs, tags, codec=codec, hdr=bool(hdr), size_gb=size,
                  seeds=seeds, uncached=uncached, foreign=bool(foreign), pack=pack,
                  release=first_line.strip(), source=source)


def rank(results, *, kind, lang="eng", binge_group=None, prefs=None):
    """results: iterable of (addon, stream). Returns Ranked list, best first. Streams the built-in
    player can't open (YouTube, external links) sink to the bottom, below MIN_SCORE."""
    ranked = []
    for a, s in results:
        r = rank_one(a, s, kind=kind, lang=lang, binge_group=binge_group)
        r.score += preference_bonus(r, prefs)
        if not (s.get("url") or s.get("infoHash")):
            r.score = min(r.score, MIN_SCORE) - 100
        ranked.append(r)
    return sorted(ranked, key=lambda r: r.score, reverse=True)
