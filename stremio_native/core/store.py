"""Profile state: settings, account, installed addons and library – persisted to disk
and, when signed in, synced with the Stremio account."""

import json
import os
import threading

from gi.repository import GObject

from . import api
from .addons import DEFAULT_ADDON_URLS, Addon
from .util import config_dir, run_async

DEFAULT_SETTINGS = {
    "server_url": "http://127.0.0.1:11470",
    "torrent_engine": "auto",      # auto | builtin | server
    "player": "builtin",           # builtin | mpv | vlc
    "subtitle_lang": "eng",
    "subtitle_size": 28,
    "embedded_subtitles": False,
    "autoplay_next": True,
    "auto_skip_intro": False,
    "stream_filters": {},          # chips on the stream list: instant / max1080 / mylang   # off: only addon subtitles, avoids showing two tracks at once
}


class Store(GObject.Object):
    __gsignals__ = {
        "addons-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "library-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "account-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__()
        self.path = os.path.join(config_dir(), "profile.json")
        self._lock = threading.Lock()
        data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                print(f"[store] ignoring unreadable profile: {e}")
        self.settings = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        self.auth_key = data.get("auth_key")
        self.user = data.get("user") or {}
        self.addons = [Addon(d) for d in data.get("addons", [])]
        self.library = {item["_id"]: item for item in data.get("library", [])}
        self.binge = data.get("binge", {})  # series id → bingeGroup of the release being watched
        self.prefs = data.get("prefs", {})  # learned stream tastes: "res:1080" / "group:ethel" → weight

    # ---- persistence -------------------------------------------------------

    def save(self):
        data = {
            "settings": self.settings,
            "auth_key": self.auth_key,
            "user": self.user,
            "addons": [a.descriptor for a in self.addons],
            "library": list(self.library.values()),
            "binge": self.binge,
            "prefs": self.prefs,
        }
        tmp = self.path + ".tmp"
        with self._lock:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)

    def set_setting(self, key, value):
        self.settings[key] = value
        self.save()

    @property
    def logged_in(self):
        return bool(self.auth_key)

    # ---- startup -----------------------------------------------------------

    def bootstrap(self):
        """Load defaults on first run, then refresh from the account if signed in."""
        if not self.addons:
            def fetch_defaults():
                out = []
                for url in DEFAULT_ADDON_URLS:
                    try:
                        out.append(Addon.from_url(url))
                    except Exception as e:  # noqa: BLE001
                        print(f"[store] default addon {url} failed: {e}")
                return out

            def done(addons):
                if addons:
                    addons[0].descriptor["flags"] = {"official": True, "protected": True}
                self.addons = addons
                self.save()
                self.emit("addons-changed")

            run_async(fetch_defaults, on_done=done, on_error=self._report)
        if self.logged_in:
            self.pull()

    def pull(self):
        key = self.auth_key

        def fetch():
            return api.get_addons(key), api.get_library(key)

        def done(result):
            descriptors, items = result
            if descriptors:
                self.addons = [Addon(d) for d in descriptors]
            self.library = {i["_id"]: i for i in items}
            self.save()
            self.emit("addons-changed")
            self.emit("library-changed")

        run_async(fetch, on_done=done, on_error=self._report)

    def _report(self, exc):
        if isinstance(exc, api.ApiError) and "session does not exist" in str(exc).lower():
            # Signed out elsewhere or the key expired – drop it so the user can sign in again.
            self.auth_key, self.user = None, {}
            self.save()
            self.emit("account-changed")
            exc = "Your Stremio session expired – please sign in again"
        self.emit("error", str(exc))

    # ---- account -----------------------------------------------------------

    def login(self, email, password, on_done, on_error):
        def done(result):
            self.auth_key, self.user = result
            self.save()
            self.emit("account-changed")
            self.pull()
            on_done()

        run_async(api.login, email, password, on_done=done, on_error=on_error)

    def logout(self):
        key, self.auth_key, self.user = self.auth_key, None, {}
        self.library = {}
        self.save()
        run_async(api.logout, key)
        self.emit("account-changed")
        self.emit("library-changed")

    # ---- addons ------------------------------------------------------------

    def addon_by_url(self, url):
        return next((a for a in self.addons if a.transport_url == url), None)

    def install_addon(self, addon):
        if self.addon_by_url(addon.transport_url):
            return
        self.addons.append(addon)
        self._addons_updated()

    def remove_addon(self, addon):
        if addon.protected:
            return
        self.addons = [a for a in self.addons if a.transport_url != addon.transport_url]
        self._addons_updated()

    def _addons_updated(self):
        self.save()
        self.emit("addons-changed")
        if self.logged_in:
            run_async(api.set_addons, self.auth_key, [a.descriptor for a in self.addons],
                      on_error=self._report)

    def addons_for(self, resource, type_, id_=None):
        return [a for a in self.addons if a.supports(resource, type_, id_)]

    # ---- library -----------------------------------------------------------

    def in_library(self, meta_id):
        item = self.library.get(meta_id)
        return bool(item and not item.get("removed") and not item.get("temp"))

    def library_items(self):
        items = [i for i in self.library.values() if not i.get("removed") and not i.get("temp")]
        return sorted(items, key=lambda i: i.get("_mtime") or "", reverse=True)

    def continue_watching(self):
        items = [i for i in self.library.values()
                 if not i.get("removed") and (i.get("state") or {}).get("timeOffset", 0) > 0]
        return sorted(items, key=lambda i: (i.get("state") or {}).get("lastWatched") or "", reverse=True)

    def _ensure_item(self, meta):
        item = self.library.get(meta["id"])
        if item is None:
            item = api.new_library_item(meta)
            self.library[meta["id"]] = item
        return item

    def set_in_library(self, meta, wanted):
        item = self._ensure_item(meta)
        item["removed"] = not wanted
        item["temp"] = False
        item["_mtime"] = api.now_iso()
        self._library_updated([item])

    def save_progress(self, meta, video_id, offset_ms, duration_ms):
        item = self._ensure_item(meta)
        if item.get("removed"):
            item["removed"], item["temp"] = False, True
        state = item.setdefault("state", {})
        finished = duration_ms and offset_ms > duration_ms * 0.9
        state.update({
            "lastWatched": api.now_iso(),
            "timeOffset": 0 if finished else int(offset_ms),
            "duration": int(duration_ms),
            "video_id": video_id,
        })
        if finished:
            state["timesWatched"] = state.get("timesWatched", 0) + 1
            state["flaggedWatched"] = 1
        item["_mtime"] = api.now_iso()
        self._library_updated([item])

    def learn(self, features, weight=1):
        """Strengthen the features of a stream you chose or watched to the end (capped, so old habits fade)."""
        for f in features:
            self.prefs[f] = min(12, self.prefs.get(f, 0) + weight)
        self.save()

    def set_binge_group(self, meta_id, group):
        if group and self.binge.get(meta_id) != group:
            self.binge[meta_id] = group
            self.save()

    def resume_offset(self, meta_id, video_id):
        state = (self.library.get(meta_id) or {}).get("state") or {}
        if state.get("video_id") == video_id:
            return state.get("timeOffset", 0)
        return 0

    def _library_updated(self, changed):
        self.save()
        self.emit("library-changed")
        if self.logged_in:
            run_async(api.put_library, self.auth_key, changed, on_error=self._report)
