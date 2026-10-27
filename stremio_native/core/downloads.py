"""Offline downloads to a folder (by default the shared drive), one file at a time, resumable.

Debrid links (Real-Debrid via Torrentio) are plain HTTP, so a download is a ranged GET that picks up
where it stopped. Expired links are re-resolved by asking the addons again for the same release.
Torrents without debrid download through the built-in engine's local HTTP server. The index lives
next to the files (<folder>/.stremio-native/downloads.json), so it travels with the drive.
"""

import json
import os
import re
import shutil
import threading
import time
from urllib.parse import quote, unquote, urlparse

from gi.repository import GLib, GObject

from . import episodes, ranking, streams, subtitles
from .util import session

DEFAULT_DIRS = ["/media/shared/Stremio", os.path.expanduser("~/Videos/Stremio")]
CHUNK = 1 << 20
RESERVE_BYTES = 2 << 30          # keep this much free on the drive
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')  # exFAT/FAT forbid these in names


def default_dir():
    for d in DEFAULT_DIRS:
        if os.path.isdir(os.path.dirname(d)):
            return d
    return DEFAULT_DIRS[-1]


def _safe(name):
    return _BAD.sub("", name).strip(" .")[:120] or "Untitled"


def _ext(stream, url):
    for candidate in ((stream.get("behaviorHints") or {}).get("filename"), unquote(urlparse(url).path)):
        m = re.search(r"\.(mkv|mp4|avi|webm|m4v|mov|ts)$", candidate or "", re.I)
        if m:
            return "." + m.group(1).lower()
    return ".mkv"


class DownloadManager(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),   # job id ("" = list changed)
    }

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.jobs = {}
        self.current = None
        self._load()
        threading.Thread(target=self._worker, daemon=True, name="downloads").start()

    # ---- storage -------------------------------------------------------------

    @property
    def root(self):
        return self.store.settings.get("download_dir") or default_dir()

    def _index_path(self):
        return os.path.join(self.root, ".stremio-native", "downloads.json")

    def _load(self):
        try:
            with open(self._index_path()) as f:
                self.jobs = {j["id"]: j for j in json.load(f)}
        except (OSError, ValueError):
            self.jobs = {}
        for j in self.jobs.values():
            if j["status"] == "downloading":
                finished = (j.get("path") and os.path.exists(j["path"])
                            and not os.path.exists(j["path"] + ".part"))
                j["status"] = "done" if finished else "queued"  # interrupted by a quit: carry on

    def _save(self):
        path = self._index_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self.lock:
            data = list(self.jobs.values())
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)

    def set_root(self, folder):
        self.store.set_setting("download_dir", folder)
        self._load()
        self._emit("")

    def free_bytes(self):
        try:
            os.makedirs(self.root, exist_ok=True)
            return shutil.disk_usage(self.root).free
        except OSError:
            return 0

    # ---- queries ---------------------------------------------------------------

    def ordered(self):
        return sorted(self.jobs.values(), key=lambda j: (j["meta"]["name"], j["video"].get("season") or 0,
                                                          j["video"].get("episode") or 0, j["added"]))

    def job(self, video_id):
        return self.jobs.get(video_id)

    def local_uri(self, video_id):
        j = self.jobs.get(video_id)
        if j and j["status"] == "done" and os.path.exists(j["path"]):
            return "file://" + quote(j["path"])
        return None

    def local_stream(self, video_id):
        """A stream dict the player can open, for a finished download."""
        uri = self.local_uri(video_id)
        if not uri:
            return None
        j = self.jobs[video_id]
        return {"url": uri, "name": "Downloaded", "title": j.get("release") or j["label"],
                "behaviorHints": {**(j["stream"].get("behaviorHints") or {}), "local": True},
                "subtitlesFile": j.get("subtitle")}

    # ---- actions -----------------------------------------------------------------

    def add(self, meta, video, ranked):
        """Queue one video with an already-chosen stream (a ranking.Ranked)."""
        vid = video["id"]
        if vid in self.jobs and self.jobs[vid]["status"] != "failed":
            return self.jobs[vid]
        keep = {k: meta.get(k) for k in ("id", "type", "name", "poster", "background", "logo", "year",
                                          "releaseInfo", "videos")}
        if meta.get("type") == "series":
            show = _safe(meta.get("name", "Show"))
            label = f"{episodes.label(video)} · {video.get('name') or video.get('title') or ''}".strip(" ·")
            folder = os.path.join(self.root, "TV", show, f"Season {int(video.get('season') or 0):02d}")
            base = _safe(f"{meta.get('name')} - S{int(video.get('season') or 0):02d}"
                         f"E{int(video.get('episode') or video.get('number') or 0):02d}"
                         f" - {video.get('name') or video.get('title') or ''}")
        else:
            year = meta.get("year") or (meta.get("releaseInfo") or "")[:4]
            name = _safe(f"{meta.get('name')} ({year})" if year else meta.get("name", "Movie"))
            label, folder, base = meta.get("name", "Movie"), os.path.join(self.root, "Movies", name), name
        job = {
            "id": vid, "meta": keep, "video": {k: video.get(k) for k in ("id", "season", "episode", "number",
                                                                       "name", "title", "thumbnail")},
            "stream": ranked.stream, "release": ranked.release, "quality": ranked.label, "label": label,
            "folder": folder, "base": base, "path": None, "size": ranked.size_gb and int(ranked.size_gb * (1 << 30)),
            "done": 0, "status": "queued", "error": None, "added": time.time(), "subtitle": None,
        }
        with self.lock:
            self.jobs[vid] = job
        self._save()
        self._emit("")
        self.wake.set()
        return job

    def add_best(self, meta, videos, on_done=None):
        """Rank streams for each video (preferring the release already downloaded or watched) and
        queue the best. Runs in the background; on_done(count, skipped) on the main loop."""
        group = self.store.binge.get(meta["id"])

        def job():
            nonlocal group
            added, skipped = 0, []
            for v in videos:
                if v["id"] in self.jobs and self.jobs[v["id"]]["status"] != "failed":
                    continue
                ranked = episodes.best_stream(self.store, meta["type"], v["id"], group)
                best = next((r for r in ranked if r.score > ranking.MIN_SCORE
                             and (r.stream.get("url") or r.stream.get("infoHash"))), None)
                if not best:
                    skipped.append(v)
                    continue
                group = best.binge_group or group      # keep the whole batch on one release
                GLib.idle_add(lambda v=v, b=best: (self.add(meta, v, b), False)[1])
                added += 1
            return added, skipped

        from .util import run_async
        run_async(job, on_done=lambda r: on_done and on_done(*r),
                  on_error=lambda e: on_done and on_done(0, videos))

    def pause(self, vid):
        j = self.jobs.get(vid)
        if j and j["status"] in ("queued", "downloading"):
            j["status"] = "paused"
            self._save()
            self._emit(vid)

    def resume(self, vid):
        j = self.jobs.get(vid)
        if j and j["status"] in ("paused", "failed"):
            j["status"], j["error"] = "queued", None
            self._save()
            self._emit(vid)
            self.wake.set()

    def remove(self, vid, delete_file=True):
        with self.lock:
            j = self.jobs.pop(vid, None)
        if not j:
            return
        j["status"] = "removed"
        if delete_file:
            for p in (j.get("path"), (j.get("path") or "") + ".part", j.get("subtitle")):
                if p and os.path.exists(p):
                    os.remove(p)
            try:
                os.removedirs(j["folder"])   # only removes folders left empty
            except OSError:
                pass
        self._save()
        self._emit("")

    # ---- worker --------------------------------------------------------------------

    def _emit(self, vid):
        GLib.idle_add(lambda: (self.emit("changed", vid), False)[1])

    def _worker(self):
        while True:
            nxt = next((j for j in sorted(self.jobs.values(), key=lambda j: j["added"])
                        if j["status"] == "queued"), None)
            if not nxt:
                self.wake.wait(30)
                self.wake.clear()
                continue
            self.current = nxt
            try:
                self._download(nxt)
            except Exception as e:  # noqa: BLE001 - shown on the Downloads page
                if nxt["status"] == "downloading":
                    nxt["status"], nxt["error"] = "failed", str(e)
            finally:
                self.current = None
                if nxt["status"] != "removed":
                    self._save()
                    self._emit(nxt["id"])

    def _resolve(self, j, refresh=False):
        if refresh:  # debrid links expire: ask the addons again for the same release
            ranked = episodes.best_stream(self.store, j["meta"]["type"], j["id"],
                                          (j["stream"].get("behaviorHints") or {}).get("bingeGroup"))
            same = next((r for r in ranked if r.release == j["release"]), None) or (ranked[0] if ranked else None)
            if not same:
                raise RuntimeError("the stream is no longer available")
            j["stream"] = same.stream
        return streams.resolve(j["stream"], self.store.settings).url

    def _download(self, j):
        j["status"], j["error"] = "downloading", None
        self._emit(j["id"])
        url = self._resolve(j)
        if not j["path"]:
            j["path"] = os.path.join(j["folder"], j["base"] + _ext(j["stream"], url))
        os.makedirs(j["folder"], exist_ok=True)
        part = j["path"] + ".part"
        attempts, last_emit, window = 0, 0.0, []
        while True:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                r = session().get(url, headers=headers, stream=True, timeout=(15, 60))
                if r.status_code in (401, 403, 404, 410, 503) and attempts < 3:
                    attempts += 1
                    url = self._resolve(j, refresh=True)
                    continue
                r.raise_for_status()
            except Exception:  # noqa: BLE001
                if attempts >= 3:
                    raise
                attempts += 1
                time.sleep(5)
                continue
            if have and r.status_code != 206:
                have = 0  # server ignored the range: start over
            total = have + int(r.headers.get("Content-Length") or 0)
            j["size"] = total or j["size"]
            if total and self.free_bytes() < (total - have) + RESERVE_BYTES:
                raise RuntimeError(f"Not enough space on {self.root}")
            with open(part, "ab" if have else "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    if j["status"] != "downloading":
                        return  # paused or removed
                    f.write(chunk)
                    have += len(chunk)
                    j["done"] = have
                    now = time.monotonic()
                    window.append((now, have))
                    window = [w for w in window if now - w[0] < 5]
                    j["speed"] = (have - window[0][1]) / max(0.5, now - window[0][0])
                    if now - last_emit > 0.5:
                        last_emit = now
                        self._emit(j["id"])
            if total and have < total:
                attempts += 1
                if attempts > 5:
                    raise RuntimeError("the connection kept dropping")
                continue
            break
        os.replace(part, j["path"])
        j["status"], j["done"], j["speed"] = "done", os.path.getsize(j["path"]), 0
        j["size"] = j["done"]
        self._save()  # record it before the (slower) subtitle lookup
        self._emit(j["id"])
        self._fetch_subtitle(j)

    def _fetch_subtitle(self, j):
        """Save the best-matching subtitle in your language next to the video, for offline viewing."""
        lang = self.store.settings.get("subtitle_lang", "eng")
        filename = (j["stream"].get("behaviorHints") or {}).get("filename") or j["release"]
        found = []
        for addon in self.store.addons_for("subtitles", j["meta"]["type"], j["id"]):
            try:
                found += addon.subtitles(j["meta"]["type"], j["id"], filename=filename,
                                         videoSize=j["size"])
            except Exception:  # noqa: BLE001
                continue
        mine = sorted((s for s in found if s.get("lang") == lang),
                      key=lambda s: -subtitles.match_score(s, filename))
        if not mine:
            return
        try:
            data = session().get(mine[0]["url"], timeout=30).content
        except Exception:  # noqa: BLE001
            return
        path = os.path.splitext(j["path"])[0] + f".{lang}.srt"
        with open(path, "wb") as f:
            f.write(data)
        j["subtitle"] = path
