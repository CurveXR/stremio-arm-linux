"""Skip intro: find the opening sequence two episodes of the same release share.

Episodes from one release group (Torrentio's bingeGroup) come from the same encode, so the title
sequence sounds the same every week. We decode the first minutes of audio of this episode and a
neighbour, fingerprint both (Haitsma–Kalker style: 32 bits per 64 ms frame from band-energy
differences), line them up by voting on matching fingerprints, and keep the longest stretch that
stays similar. Results are cached per episode and release.
"""

import json
import os
import threading
import time

import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .util import cache_dir  # noqa: E402

RATE = 8000
FRAME = 2048          # 256 ms analysis window
HOP = 512             # 64 ms per fingerprint
HOP_S = HOP / RATE
ANALYSE_S = 6 * 60    # intros start within the first few minutes
MIN_INTRO_S = 8
MAX_INTRO_S = 150
DECODE_TIMEOUT_S = 120
_BANDS = np.geomspace(300, 2000, 34)
_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

_lock = threading.Lock()
_CACHE_PATH = os.path.join(cache_dir("intros"), "intros.json")


# ---- cache ------------------------------------------------------------------

def _load():
    try:
        with open(_CACHE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cached(video_id, group):
    """(start, end) seconds, False when we looked and found none, or None if never analysed."""
    entry = _load().get(f"{video_id}|{group}")
    if entry is None:
        return None
    return (entry["start"], entry["end"]) if "start" in entry else False


def remember(video_id, group, segment):
    with _lock:
        data = _load()
        data[f"{video_id}|{group}"] = {"start": segment[0], "end": segment[1]} if segment else {"none": True}
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _CACHE_PATH)


# ---- audio --------------------------------------------------------------------

def decode_audio(uri, seconds=ANALYSE_S):
    """Blocking: first `seconds` of the first audio track as mono 8 kHz int16 (video is never decoded)."""
    Gst.init(None)
    pipe = Gst.Pipeline.new("intro")
    src = Gst.ElementFactory.make("uridecodebin", None)
    src.set_property("uri", uri)
    src.set_property("caps", Gst.Caps.from_string("audio/x-raw"))
    conv = Gst.ElementFactory.make("audioconvert", None)
    res = Gst.ElementFactory.make("audioresample", None)
    caps = Gst.ElementFactory.make("capsfilter", None)
    caps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,format=S16LE,rate={RATE},channels=1"))
    sink = Gst.ElementFactory.make("appsink", None)
    sink.set_property("sync", False)
    sink.set_property("max-buffers", 64)
    for e in (src, conv, res, caps, sink):
        pipe.add(e)
    conv.link(res)
    res.link(caps)
    caps.link(sink)
    linked = {"audio": False}

    def on_pad(_src, pad):
        name = pad.get_current_caps().to_string() if pad.get_current_caps() else ""
        if name.startswith("audio/x-raw") and not linked["audio"]:
            linked["audio"] = True
            pad.link(conv.get_static_pad("sink"))
        else:  # video, subtitles, extra audio tracks: drop them
            fake = Gst.ElementFactory.make("fakesink", None)
            fake.set_property("sync", False)
            pipe.add(fake)
            fake.sync_state_with_parent()
            pad.link(fake.get_static_pad("sink"))

    src.connect("pad-added", on_pad)
    pipe.set_state(Gst.State.PLAYING)
    chunks, got, deadline = [], 0, time.monotonic() + DECODE_TIMEOUT_S
    bus = pipe.get_bus()
    try:
        while got < seconds * RATE:
            if time.monotonic() > deadline:
                raise TimeoutError("audio decode took too long")
            msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if msg:
                err, _dbg = msg.parse_error()
                raise RuntimeError(err.message)
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                if sink.get_property("eos"):
                    break
                continue
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                chunks.append(np.frombuffer(info.data, dtype=np.int16).copy())
                got += len(chunks[-1])
                buf.unmap(info)
    finally:
        pipe.set_state(Gst.State.NULL)
    if not chunks:
        raise RuntimeError("no audio decoded")
    return np.concatenate(chunks)[: seconds * RATE]


# ---- fingerprints and matching ----------------------------------------------------

def fingerprint(samples):
    x = samples.astype(np.float32) / 32768.0
    n = 1 + max(0, (len(x) - FRAME) // HOP)
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * np.hanning(FRAME), axis=1)) ** 2
    freqs = np.fft.rfftfreq(FRAME, 1 / RATE)
    bands = np.stack([spec[:, (freqs >= lo) & (freqs < hi)].sum(axis=1)
                      for lo, hi in zip(_BANDS[:-1], _BANDS[1:])], axis=1)
    bands = np.log(bands + 1e-10)
    d = bands[:, :-1] - bands[:, 1:]                   # energy difference across bands
    bits = (d[1:] - d[:-1]) > 0                        # …and across time → 32 bits per frame
    weights = (1 << np.arange(32, dtype=np.uint64)).astype(np.uint64)
    return (bits.astype(np.uint64) * weights).sum(axis=1).astype(np.uint32)


def _bit_errors(a, b):
    x = np.bitwise_xor(a, b).view(np.uint8).reshape(-1, 4)
    return _POP8[x].sum(axis=1)


def find_common(fa, fb):
    """Longest similar stretch between two fingerprint sequences →
    ((start_a, end_a), (start_b, end_b)) in seconds, or None."""
    positions = {}
    for j, h in enumerate(fb):
        positions.setdefault(int(h), []).append(j)
    votes = {}
    for i, h in enumerate(fa):
        for j in positions.get(int(h), ()):
            votes[j - i] = votes.get(j - i, 0) + 1
    if not votes:
        return None
    best = None
    for off, _n in sorted(votes.items(), key=lambda kv: -kv[1])[:8]:
        i0, i1 = max(0, -off), min(len(fa), len(fb) - off)
        if i1 - i0 < MIN_INTRO_S / HOP_S:
            continue
        errors = _bit_errors(fa[i0:i1], fb[i0 + off:i1 + off]).astype(np.float32)
        smooth = np.convolve(errors, np.ones(31) / 31, mode="same")   # ~2 s window
        good = smooth < 32 * 0.30
        run_start, run = None, None
        for k, g in enumerate(np.append(good, False)):
            if g and run_start is None:
                run_start = k
            elif not g and run_start is not None:
                if run is None or k - run_start > run[1] - run[0]:
                    run = (run_start, k)
                run_start = None
        if run and (best is None or run[1] - run[0] > best[0][1] - best[0][0]):
            best = (run, off, i0)
    if not best:
        return None
    (s, e), off, i0 = best
    length = (e - s) * HOP_S
    if not MIN_INTRO_S <= length <= MAX_INTRO_S:
        return None
    a = ((i0 + s) * HOP_S, (i0 + e) * HOP_S)
    b = ((i0 + s + off) * HOP_S, (i0 + e + off) * HOP_S)
    return a, b


def detect(uri_a, uri_b):
    """Blocking: intro segments for two episodes, or None."""
    fa = fingerprint(decode_audio(uri_a))
    fb = fingerprint(decode_audio(uri_b))
    return find_common(fa, fb)
