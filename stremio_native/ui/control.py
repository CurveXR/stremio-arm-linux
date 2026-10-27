"""Remote control over D-Bus, for voice assistants and scripts.

Two interfaces on the session bus:
- io.github.stremio_native.Control at /io/github/stremio_native/Control (on the app's own bus
  name): play a title by name, a given episode, playback commands, downloads, search, status.
- MPRIS (org.mpris.MediaPlayer2.stremio_native), the standard media-player interface, so
  playerctl / desktop media keys / voice assistants' media commands work without knowing this app.

`stremio-native ctl …` (core/ctl.py) is the command-line wrapper around the first.
"""

import difflib
import json
import re

from gi.repository import Gio, GLib

from ..core import episodes, ranking
from ..core.addons import catalog_extras
from ..core.util import APP_ID, run_async
from .detail import DetailPage, fetch_meta
from .player import PlayerPage

CONTROL_PATH = "/io/github/stremio_native/Control"
CONTROL_IFACE = "io.github.stremio_native.Control"
MPRIS_NAME = "org.mpris.MediaPlayer2." + APP_ID.rsplit(".", 1)[-1]
MPRIS_PATH = "/org/mpris/MediaPlayer2"

CONTROL_XML = f"""
<node>
  <interface name="{CONTROL_IFACE}">
    <method name="Play"><arg name="query" type="s" direction="in"/><arg name="reply" type="s" direction="out"/></method>
    <method name="PlayEpisode">
      <arg name="query" type="s" direction="in"/><arg name="season" type="i" direction="in"/>
      <arg name="episode" type="i" direction="in"/><arg name="reply" type="s" direction="out"/>
    </method>
    <method name="Command"><arg name="name" type="s" direction="in"/><arg name="reply" type="s" direction="out"/></method>
    <method name="Seek"><arg name="seconds" type="i" direction="in"/><arg name="reply" type="s" direction="out"/></method>
    <method name="Download">
      <arg name="query" type="s" direction="in"/><arg name="what" type="s" direction="in"/>
      <arg name="reply" type="s" direction="out"/>
    </method>
    <method name="Search"><arg name="query" type="s" direction="in"/><arg name="json" type="s" direction="out"/></method>
    <method name="Status"><arg name="json" type="s" direction="out"/></method>
  </interface>
</node>
"""

MPRIS_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/><method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/><property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/><property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
    <property name="Fullscreen" type="b" access="readwrite"/><property name="CanSetFullscreen" type="b" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/><method name="Previous"/><method name="Pause"/><method name="PlayPause"/>
    <method name="Stop"/><method name="Play"/>
    <method name="Seek"><arg name="Offset" type="x" direction="in"/></method>
    <method name="SetPosition"><arg name="TrackId" type="o" direction="in"/><arg name="Position" type="x" direction="in"/></method>
    <method name="OpenUri"><arg name="Uri" type="s" direction="in"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/><property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/><property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/><property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/><property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/><property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/><property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

_EPISODE = re.compile(r"\b(?:s(?:eason)?\s*(\d+)\s*,?\s*e(?:p(?:isode)?)?\s*(\d+)|(\d+)x(\d+))\b", re.I)


class Control:
    def __init__(self, app, win):
        self.app, self.win = app, win
        conn = app.get_dbus_connection()
        if conn is None:
            return
        self.conn = conn
        node = Gio.DBusNodeInfo.new_for_xml(CONTROL_XML)
        conn.register_object(CONTROL_PATH, node.interfaces[0], self._on_control, None, None)
        mpris = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
        for iface in mpris.interfaces:
            conn.register_object(MPRIS_PATH, iface, self._on_mpris_call, self._on_mpris_get, self._on_mpris_set)
        Gio.bus_own_name_on_connection(conn, MPRIS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
        self._last = {}
        GLib.timeout_add(700, self._watch)

    # ---- helpers -------------------------------------------------------------

    def player(self):
        page = self.win.nav.get_visible_page()
        return page if isinstance(page, PlayerPage) else None

    def _search(self, query):
        """Blocking: best catalog match for a spoken title → preview meta (or None)."""
        store = self.win.store
        results = []
        for addon in store.addons:
            for cat in addon.catalogs:
                if cat.get("type") in ("movie", "series") and "search" in catalog_extras(cat)[0]:
                    try:
                        found = addon.catalog(cat["type"], cat["id"], search=query)[:8]
                    except Exception:  # noqa: BLE001
                        continue
                    results += [{**m, "_pos": i} for i, m in enumerate(found)]
        if not results:
            return None, []
        q = query.lower().strip()

        def score(m):
            # The catalog's own order (relevance + popularity) matters most; a near-exact name wins.
            name = re.sub(r"[^a-z0-9 ]", "", (m.get("name") or "").lower())
            words = re.sub(r"[^a-z0-9 ]", "", q)
            s = 0.5 * difflib.SequenceMatcher(None, words, name).ratio() - 0.15 * m["_pos"]
            if name == words:
                s += 2
            elif words in name:
                s += 0.5
            return s

        uniq = {m["id"]: m for m in sorted(results, key=lambda m: m["_pos"], reverse=True)}.values()
        ranked = sorted(uniq, key=score, reverse=True)
        return ranked[0], ranked

    def _reply_later(self, invocation, fn, *args):
        run_async(fn, *args,
                  on_done=lambda text: invocation.return_value(GLib.Variant("(s)", (text,))),
                  on_error=lambda e: invocation.return_value(GLib.Variant("(s)", (f"Sorry: {e}",))))

    # ---- Control interface ----------------------------------------------------------

    def _on_control(self, _conn, _sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        if method == "Play":
            query, season, episode = args[0], None, None
            m = _EPISODE.search(query)
            if m:
                season, episode = int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4))
                query = _EPISODE.sub("", query).strip(" ,-")
            self._reply_later(invocation, self._play, query, season, episode)
        elif method == "PlayEpisode":
            self._reply_later(invocation, self._play, args[0], args[1] or None, args[2] or None)
        elif method == "Command":
            invocation.return_value(GLib.Variant("(s)", (self._command(args[0]),)))
        elif method == "Seek":
            invocation.return_value(GLib.Variant("(s)", (self._seek(args[0]),)))
        elif method == "Download":
            self._reply_later(invocation, self._download, args[0], args[1] or "episode")
        elif method == "Search":
            def search(q):
                _best, ranked = self._search(q)
                return json.dumps([{"id": m["id"], "type": m.get("type"), "name": m.get("name"),
                                    "year": m.get("releaseInfo")} for m in ranked[:10]])
            self._reply_later(invocation, search, args[0])
        elif method == "Status":
            invocation.return_value(GLib.Variant("(s)", (json.dumps(self.status()),)))

    def _resolve_target(self, query, season, episode):
        """Blocking: (meta, video, verb) for a spoken request."""
        preview, candidates = self._search(query)
        if not preview:
            raise LookupError(f"I couldn't find {query}")
        meta = None
        for cand in candidates[:4] if season else [preview]:
            if season and cand.get("type") != "series":
                continue
            full = {**cand, **fetch_meta(self.win.store, cand["type"], cand["id"])}
            if not season or any(v.get("season") == season and (v.get("episode") or v.get("number")) == episode
                                 for v in full.get("videos") or []):
                meta = full
                break
            meta = meta or full
        meta.pop("_pos", None)
        if meta.get("type") == "series" and episodes.ordered(meta):
            if season and episode:
                video = next((v for v in meta["videos"] if v.get("season") == season
                              and (v.get("episode") or v.get("number")) == episode), None)
                if not video:
                    raise LookupError(f"{meta['name']} has no season {season} episode {episode}")
                return meta, video, "Playing"
            state = (self.win.store.library.get(meta["id"]) or {}).get("state")
            video, verb = episodes.up_next(meta, state)
            return meta, video, {"Resume": "Resuming", "Next": "Playing", "Play": "Playing"}[verb]
        video_id = (meta.get("behaviorHints") or {}).get("defaultVideoId") or meta["id"]
        verb = "Resuming" if self.win.store.resume_offset(meta["id"], video_id) else "Playing"
        return meta, {"id": video_id, "title": meta.get("name")}, verb

    def _play(self, query, season, episode):
        meta, video, verb = self._resolve_target(query, season, episode)
        what = meta["name"] + (f" {episodes.label(video)}" if meta.get("type") == "series" else "")
        local = self.win.downloads.local_stream(video["id"])
        if local:
            GLib.idle_add(self._show_player, meta, video, local, [])
            return f"{verb} {what} from your drive"
        ranked = episodes.best_stream(self.win.store, meta["type"], video["id"],
                                      self.win.store.binge.get(meta["id"]))
        good = [r for r in ranked if r.score > ranking.MIN_SCORE]
        if not good:
            GLib.idle_add(self._show_details, meta)
            return f"I found {meta['name']} but no stream that plays well here; I opened its page"
        GLib.idle_add(self._show_player, meta, video, good[0].stream, good[1:6])
        return f"{verb} {what} in {good[0].label}"

    def _show_details(self, meta):
        self.win.nav.pop_to_tag("home")
        self.win.nav.push(DetailPage(self.win, meta))
        self.win.present()
        return False

    def _show_player(self, meta, video, stream, alternatives):
        self._show_details(meta)
        self.win.play(meta, video, stream, alternatives)
        self.win.fullscreen()
        return False

    def _command(self, name):
        name = name.strip().lower().replace("_", "-")
        p = self.player()
        if name in ("raise", "show"):
            self.win.present()
            return "Showing Stremio"
        if name == "quit":
            GLib.idle_add(self.app.quit)
            return "Closing Stremio"
        if not p or not p.media:
            if name in ("stop", "back", "home"):
                self.win.nav.pop_to_tag("home")
                return "Back to the home screen"
            return "Nothing is playing"
        m = p.media
        actions = {
            "play": lambda: (m.play(), "Playing"),
            "resume": lambda: (m.play(), "Playing"),
            "pause": lambda: (m.pause(), "Paused"),
            "toggle": lambda: self._toggle(m),
            "stop": lambda: (p.leave(), "Stopped"),
            "next": lambda: (p.play_next(), "Next episode") if p.next_video else (None, "This is the last episode"),
            "previous": lambda: self._previous(p),
            "skip-intro": lambda: (None, "Skipped the intro") if p.skip_intro() else (None, "No intro to skip right now"),
            "fullscreen": lambda: (self.win.fullscreen(), "Fullscreen"),
            "exit-fullscreen": lambda: (self.win.unfullscreen(), "Left fullscreen"),
            "mute": lambda: (m.set_muted(True), "Muted"),
            "unmute": lambda: (m.set_muted(False), "Unmuted"),
            "volume-up": lambda: (m.set_volume(min(1.0, m.get_volume() + 0.1)), "Volume up"),
            "volume-down": lambda: (m.set_volume(max(0.0, m.get_volume() - 0.1)), "Volume down"),
            "subtitles-off": lambda: (p._select_subtitle(None), "Subtitles off"),
            "forward": lambda: (None, self._seek(30)),
            "rewind": lambda: (None, self._seek(-30)),
        }
        if name not in actions:
            return f"Unknown command {name}. Try: {', '.join(sorted(actions))}"
        return actions[name]()[1]

    @staticmethod
    def _toggle(m):
        was_playing = m.get_playing()
        m.set_playing(not was_playing)
        return None, "Paused" if was_playing else "Playing"

    def _previous(self, p):
        prev = episodes.preceding(p.meta, p.video["id"])
        if not prev:
            p.media.seek(0)
            return "Restarted the episode"
        self._reply_none(self._play_video, p.meta, prev)
        return f"Playing {episodes.label(prev)}"

    def _reply_none(self, fn, *args):
        run_async(fn, *args)

    def _play_video(self, meta, video):
        local = self.win.downloads.local_stream(video["id"])
        ranked = [] if local else episodes.best_stream(self.win.store, meta["type"], video["id"],
                                                       self.win.store.binge.get(meta["id"]))
        good = [r for r in ranked if r.score > ranking.MIN_SCORE]
        stream = local or (good and good[0].stream)
        if stream:
            GLib.idle_add(self._show_player, meta, video, stream, good[1:6])

    def _seek(self, seconds):
        p = self.player()
        if not p or not p.media or not p.media.is_seekable():
            return "Nothing to seek"
        m = p.media
        ts = max(0, min(m.get_duration(), m.get_timestamp() + seconds * 1_000_000))
        m.seek(int(ts))
        return f"{'Forward' if seconds > 0 else 'Back'} {abs(seconds)} seconds"

    def _download(self, query, what):
        meta, video, _verb = self._resolve_target(*self._split_episode(query))
        if meta.get("type") == "series":
            eps = episodes.ordered(meta)
            i = next((k for k, v in enumerate(eps) if v["id"] == video["id"]), 0)
            if what == "next3":
                videos = eps[i:i + 3]
            elif what == "season":
                videos = [v for v in eps if v.get("season") == video.get("season")]
            else:
                videos = [video]
        else:
            videos = [video]
        GLib.idle_add(lambda: (self.win.downloads.add_best(meta, videos), False)[1])
        label = (f"{meta['name']} season {video.get('season')}" if what == "season"
                 else f"{len(videos)} episodes of {meta['name']}" if len(videos) > 1
                 else meta["name"] + (f" {episodes.label(video)}" if meta.get("type") == "series" else ""))
        return f"Downloading {label} to {self.win.downloads.root}"

    @staticmethod
    def _split_episode(query):
        m = _EPISODE.search(query)
        if not m:
            return query, None, None
        return _EPISODE.sub("", query).strip(" ,-"), int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4))

    def status(self):
        p = self.player()
        out = {"view": self.win.nav.get_visible_page().get_tag() or type(self.win.nav.get_visible_page()).__name__,
               "fullscreen": self.win.is_fullscreen()}
        if p:
            m = p.media
            out.update({
                "state": "loading" if not (m and m.is_prepared()) else "playing" if m.get_playing() else "paused",
                "title": p.meta.get("name"), "type": p.meta.get("type"),
                "episode": episodes.label(p.video) if p.meta.get("type") == "series" else None,
                "position": round(m.get_timestamp() / 1e6) if m else 0,
                "duration": round(m.get_duration() / 1e6) if m else 0,
                "intro_skippable": p.skip_btn.get_visible(),
                "next_episode": episodes.label(p.next_video) if p.next_video else None,
                "volume": round(m.get_volume(), 2) if m else None,
            })
        else:
            out["state"] = "idle"
        out["downloads"] = [{"title": j["meta"]["name"], "episode": j["label"] if j["meta"].get("type") == "series"
                             else None, "status": j["status"],
                             "percent": round(100 * (j.get("done") or 0) / j["size"]) if j.get("size") else None}
                            for j in self.win.downloads.ordered() if j["status"] != "done"]
        return out

    # ---- MPRIS ---------------------------------------------------------------------

    def _mpris_props(self):
        p = self.player()
        m = p.media if p else None
        status = "Stopped"
        if m and m.is_prepared():
            status = "Playing" if m.get_playing() else "Paused"
        meta = {"mpris:trackid": GLib.Variant("o", "/io/github/stremio_native/track/0")}
        if p:
            title = p.get_title()
            meta["xesam:title"] = GLib.Variant("s", title)
            meta["xesam:album"] = GLib.Variant("s", p.meta.get("name", ""))
            if m and m.get_duration():
                meta["mpris:length"] = GLib.Variant("x", m.get_duration())
            if p.meta.get("poster"):
                meta["mpris:artUrl"] = GLib.Variant("s", p.meta["poster"])
        return {
            "PlaybackStatus": GLib.Variant("s", status),
            "Metadata": GLib.Variant("a{sv}", meta),
            "Position": GLib.Variant("x", m.get_timestamp() if m else 0),
            "Volume": GLib.Variant("d", m.get_volume() if m else 1.0),
            "Rate": GLib.Variant("d", 1.0), "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", bool(p and p.next_video)),
            "CanGoPrevious": GLib.Variant("b", bool(p)),
            "CanPlay": GLib.Variant("b", bool(m)), "CanPause": GLib.Variant("b", bool(m)),
            "CanSeek": GLib.Variant("b", bool(m and m.is_seekable())), "CanControl": GLib.Variant("b", True),
            # org.mpris.MediaPlayer2
            "CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False), "Identity": GLib.Variant("s", "Stremio"),
            "DesktopEntry": GLib.Variant("s", "io.github.stremio_native"),
            "SupportedUriSchemes": GLib.Variant("as", []), "SupportedMimeTypes": GLib.Variant("as", []),
            "Fullscreen": GLib.Variant("b", self.win.is_fullscreen()),
            "CanSetFullscreen": GLib.Variant("b", True),
        }

    def _on_mpris_get(self, _conn, _sender, _path, _iface, prop):
        return self._mpris_props().get(prop)

    def _on_mpris_set(self, _conn, _sender, _path, _iface, prop, value):
        p = self.player()
        if prop == "Volume" and p and p.media:
            p.media.set_volume(max(0.0, min(1.0, value.unpack())))
        elif prop == "Fullscreen":
            self.win.fullscreen() if value.unpack() else self.win.unfullscreen()
        return True

    def _on_mpris_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        p = self.player()
        m = p.media if p else None
        if method == "Raise":
            self.win.present()
        elif method == "Quit":
            GLib.idle_add(self.app.quit)
        elif m and method == "Play":
            m.play()
        elif m and method == "Pause":
            m.pause()
        elif m and method == "PlayPause":
            m.set_playing(not m.get_playing())
        elif p and method == "Stop":
            p.leave()
        elif p and method == "Next" and p.next_video:
            p.play_next()
        elif p and method == "Previous":
            self._previous(p)
        elif m and method == "Seek":
            offset = params.unpack()[0]
            m.seek(max(0, min(m.get_duration(), m.get_timestamp() + offset)))
        elif m and method == "SetPosition":
            m.seek(max(0, min(m.get_duration(), params.unpack()[1])))
        invocation.return_value(None)

    def _watch(self):
        """Emit PropertiesChanged when what MPRIS clients see changes."""
        props = self._mpris_props()
        keys = ("PlaybackStatus", "Metadata", "Volume", "CanGoNext", "CanSeek")
        changed = {k: props[k] for k in keys if self._last.get(k) != props[k].print_(False)}
        if changed:
            self._last.update({k: v.print_(False) for k, v in changed.items()})
            self.conn.emit_signal(None, MPRIS_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                  GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", changed, [])))
        return GLib.SOURCE_CONTINUE
