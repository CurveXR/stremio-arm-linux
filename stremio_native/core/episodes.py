"""Episode ordering for series: what to play next, and what comes after an episode."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import ranking


def _released(v):
    when = v.get("released") or v.get("firstAired")
    if not when:
        return True
    try:
        return datetime.fromisoformat(when.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except ValueError:
        return True


def ordered(meta):
    """Aired regular episodes (no specials), in watch order."""
    eps = [v for v in meta.get("videos") or [] if v.get("id") and (v.get("season") or 0) > 0 and _released(v)]
    return sorted(eps, key=lambda v: (v.get("season") or 0, v.get("episode") or v.get("number") or 0))


def following(meta, video_id):
    eps = ordered(meta)
    for i, v in enumerate(eps):
        if v["id"] == video_id:
            return eps[i + 1] if i + 1 < len(eps) else None
    return None


def preceding(meta, video_id):
    eps = ordered(meta)
    for i, v in enumerate(eps):
        if v["id"] == video_id:
            return eps[i - 1] if i > 0 else None
    return None


def label(v):
    return f"S{v.get('season')}E{v.get('episode') or v.get('number')}"


def up_next(meta, state):
    """(video, verb) for the series Play button: resume, continue after the last one, or start."""
    eps = ordered(meta)
    if not eps:
        return None, None
    last = (state or {}).get("video_id")
    current = next((v for v in eps if v["id"] == last), None)
    if current and (state or {}).get("timeOffset"):
        return current, "Resume"
    if current:
        nxt = following(meta, current["id"])
        return (nxt, "Next") if nxt else (current, "Play")
    return eps[0], "Play"


def best_stream(store, type_, video_id, binge_group=None):
    """Blocking: ask every stream addon in parallel and return the ranked list, best first."""
    addons = store.addons_for("stream", type_, video_id)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(addons))) as pool:
        for addon, streams in zip(addons, pool.map(lambda a: _safe_streams(a, type_, video_id), addons)):
            results += [(addon, s) for s in streams]
    return ranking.rank(results, kind=type_, lang=store.settings.get("subtitle_lang", "eng"),
                        binge_group=binge_group, prefs=store.prefs)


def _safe_streams(addon, type_, video_id):
    try:
        return addon.streams(type_, video_id)
    except Exception as e:  # noqa: BLE001 - one broken addon shouldn't block the others
        print(f"[streams] {addon.name}: {e}")
        return []
