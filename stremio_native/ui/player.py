"""Built-in player (GTK media / GStreamer) with addon subtitles, resume and torrent stats."""

import os
import shutil
import subprocess

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from ..core import codecs, episodes, intro, ranking, streams, subtitles
from ..core.util import run_async
from .widgets import clear, spinner

SAVE_EVERY_S = 15
OSD_HIDE_MS = 3000
PREFETCH_NEXT_S = 150    # look up the next episode's stream this long before the end
NEXT_COUNTDOWN_S = 8
START_TIMEOUT_S = {"http": 45, "torrent": 120}  # no picture by then → treat the stream as dead


def external_players():
    return {name: path for name in ("mpv", "vlc", "celluloid") if (path := shutil.which(name))}


def launch_external(player, url, title, start_s=0):
    path = external_players().get(player)
    if not path:
        raise FileNotFoundError(f"{player} is not installed")
    args = [path]
    if player == "mpv":
        args += [f"--force-media-title={title}", f"--start={int(start_s)}", "--fs"]
    elif player == "vlc":
        args += ["--fullscreen", f"--start-time={int(start_s)}", f"--meta-title={title}"]
    subprocess.Popen(args + [url], start_new_session=True)


def _fmt_rate(bps):
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024:
            return f"{bps:.0f} {unit}"
        bps /= 1024
    return f"{bps:.1f} GB/s"


class PlayerPage(Adw.NavigationPage):
    def __init__(self, win, meta, video, stream, alternatives=None):
        title = meta.get("name", "")
        if video.get("season") is not None and (video.get("episode") or video.get("number")):
            title += f" — S{video['season']}E{video.get('episode') or video.get('number')}"
        super().__init__(title=title, css_classes=["player"])
        self.win, self.meta, self.video, self.stream = win, meta, video, stream
        self.media = None
        self.playable = None
        self.track = None
        self.timers = []
        self.last_saved = 0
        self.handed_off = False  # an external player took over; keep the torrent alive
        self.playback_started = False
        self.closed = False
        self.continuing = False  # replaced by the next episode or a fallback stream: keep fullscreen
        self.alternatives = list(alternatives or [])  # ranked fallbacks if this stream fails
        self.next_video = episodes.following(meta, video["id"]) if meta.get("type") != "movie" else None
        self.next_choice = None
        self._prefetching = False
        self._countdown = None
        binge = (stream.get("behaviorHints") or {}).get("bingeGroup")
        if self.next_video is not None or meta.get("type") == "series":
            win.store.set_binge_group(meta["id"], binge)

        self.overlay = Gtk.Overlay(css_classes=["player-surface"])
        self.video_widget = Gtk.Video(autoplay=True, hexpand=True, vexpand=True)
        if hasattr(self.video_widget, "set_graphics_offload"):
            self.video_widget.set_graphics_offload(Gtk.GraphicsOffloadEnabled.ENABLED)
        self.overlay.set_child(self.video_widget)

        self.sub_label = Gtk.Label(css_classes=["subtitle"], valign=Gtk.Align.END, halign=Gtk.Align.CENTER,
                                   justify=Gtk.Justification.CENTER, wrap=True, margin_bottom=72,
                                   use_markup=True, can_target=False, max_width_chars=60)
        self.overlay.add_overlay(self.sub_label)
        self.skip_btn = Gtk.Button(label="Skip intro", css_classes=["pill", "skip-intro"], visible=False,
                                   halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=32, margin_bottom=96)
        self.skip_btn.connect("clicked", lambda *_: self.skip_intro())
        self.overlay.add_overlay(self.skip_btn)
        self.intro = None          # (start, end) seconds of the opening sequence, once known
        self._intro_looked = False
        self._auto_skipped = False

        self.center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                              valign=Gtk.Align.CENTER, css_classes=["osd", "player-status"])
        self.overlay.add_overlay(self.center)
        self._status("Loading stream…", busy=True)

        self.osd = self._build_osd(title)
        self.overlay.add_overlay(self.osd)
        self.set_child(self.overlay)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._poke_osd)
        self.overlay.add_controller(motion)
        dbl = Gtk.GestureClick()
        dbl.connect("pressed", lambda g, n, *_: n == 2 and self.toggle_fullscreen())
        self.overlay.add_controller(dbl)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self.connect("hiding", lambda *_: self._teardown())
        self.connect("shown", lambda *_: self.grab_focus())
        run_async(streams.resolve, stream, win.store.settings, on_done=self._resolved, on_error=self._failed)

    # ---- UI pieces ---------------------------------------------------------

    def _build_osd(self, title):
        osd = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.CROSSFADE, reveal_child=True,
                           valign=Gtk.Align.START)
        bar = Gtk.Box(spacing=8, css_classes=["osd", "player-top"])
        back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat", "circular"], tooltip_text="Back")
        back.connect("clicked", lambda *_: self.leave())
        bar.append(back)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        labels.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"]))
        self.stats = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"])
        labels.append(self.stats)
        bar.append(labels)

        self.sub_btn = Gtk.MenuButton(icon_name="media-view-subtitles-symbolic", tooltip_text="Subtitles",
                                      css_classes=["flat", "circular"], sensitive=False)
        pop = Gtk.Popover()
        self.sub_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["navigation-sidebar"])
        self.sub_list.connect("row-activated", self._on_sub_row)
        sw = Gtk.ScrolledWindow(child=self.sub_list, propagate_natural_height=True, max_content_height=420,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_size_request(260, -1)
        pop.set_child(sw)
        self.sub_btn.set_popover(pop)
        bar.append(self.sub_btn)

        ext = Gtk.MenuButton(icon_name="view-more-symbolic", css_classes=["flat", "circular"],
                             tooltip_text="More")
        menu = Gio.Menu()
        for name in external_players():
            menu.append(f"Open in {name}", f"player.external::{name}")
        menu.append("Copy stream link", "player.copy")
        ext.set_menu_model(menu)
        group = Gio.SimpleActionGroup()
        a = Gio.SimpleAction.new("external", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, v: self._open_external(v.get_string()))
        group.add_action(a)
        c = Gio.SimpleAction.new("copy", None)
        c.connect("activate", lambda *_: self.playable and Gdk.Display.get_default()
                  .get_clipboard().set(self.playable.url))
        group.add_action(c)
        self.insert_action_group("player", group)
        bar.append(ext)

        if self.next_video:
            nxt = Gtk.Button(icon_name="media-skip-forward-symbolic", css_classes=["flat", "circular"],
                             tooltip_text=f"Next episode: {episodes.label(self.next_video)} (N)")
            nxt.connect("clicked", lambda *_: self.play_next())
            bar.append(nxt)
        fs = Gtk.Button(icon_name="view-fullscreen-symbolic", css_classes=["flat", "circular"],
                        tooltip_text="Fullscreen (F)")
        fs.connect("clicked", lambda *_: self.toggle_fullscreen())
        bar.append(fs)
        osd.set_child(bar)
        return osd

    def _status(self, text, busy=False, error=False, detail=None, actions=()):
        """Centre message; `actions` is a list of (label, callback, suggested)."""
        clear(self.center)
        if not text:
            self.center.set_visible(False)
            return
        self.center.set_visible(True)
        if busy:
            self.center.append(spinner(48))
        lbl = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=60,
                        css_classes=["title-4"] + (["error"] if error else []))
        self.center.append(lbl)
        if detail:
            self.center.append(Gtk.Label(label=detail, wrap=True, justify=Gtk.Justification.CENTER,
                                         max_width_chars=60, selectable=True, css_classes=["body"]))
        if actions:
            row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
            for label, cb, suggested in actions:
                b = Gtk.Button(label=label, css_classes=["pill"] + (["suggested-action"] if suggested else []))
                b.connect("clicked", lambda _b, cb=cb: cb())
                row.append(b)
            self.center.append(row)

    def _poke_osd(self, *_):
        self.osd.set_reveal_child(True)
        self.set_cursor(None)
        if getattr(self, "_osd_timer", None):
            GLib.source_remove(self._osd_timer)
        self._osd_timer = GLib.timeout_add(OSD_HIDE_MS, self._hide_osd)

    def _hide_osd(self):
        self._osd_timer = None
        if self.media and self.media.get_playing():
            self.osd.set_reveal_child(False)
            self.set_cursor(Gdk.Cursor.new_from_name("none"))
        return GLib.SOURCE_REMOVE

    # ---- playback ----------------------------------------------------------

    def _resolved(self, playable):
        self.playable = playable
        if playable.kind == "external":
            Gtk.UriLauncher.new(playable.url).launch(self.win, None, None)
            self.win.toast("Opened in your browser")
            self.leave()
            return
        player = self.win.store.settings.get("player", "builtin")
        offset_s = self.win.store.resume_offset(self.meta["id"], self.video["id"]) / 1000
        if player != "builtin" and player in external_players():
            launch_external(player, playable.url, self.get_title(), offset_s)
            self._status(f"Playing in {player}…\nKeep this page open while watching a torrent.")
            self._start_stats()
            return
        self._start_stats()
        if codecs.ffmpeg_installed():
            self._start_playback()
        else:
            # Without FFmpeg most releases fail (Dolby audio, XviD, …) – check before playing.
            self._status("Checking video format…", busy=True)
            run_async(codecs.probe, playable.url, on_done=self._probed,
                      on_error=lambda _e: self._start_playback())

    def _probed(self, missing):
        if self.closed:
            return
        if missing:
            self._codecs_missing(missing)
        else:
            self._start_playback()

    def _start_playback(self):
        if self.closed:
            return
        playable = self.playable
        self._status("Buffering…" if playable.kind == "torrent" else "Opening…", busy=True)
        if self.media:
            self.media.set_playing(False)
        codecs.set_embedded_subtitles(self.win.store.settings.get("embedded_subtitles", False))
        self.media = Gtk.MediaFile.new_for_file(Gio.File.new_for_uri(playable.url))
        self.media.connect("notify::prepared", self._on_prepared)
        limit = START_TIMEOUT_S.get(playable.kind, 60)
        self.timers.append(GLib.timeout_add_seconds(limit, self._check_started))
        self.media.connect("notify::error", self._on_error)
        self.media.connect("notify::ended", self._on_ended)
        self.video_widget.set_media_stream(self.media)
        if not self.playback_started:
            self.playback_started = True
            self.timers.append(GLib.timeout_add(100, self._tick_subtitles))
            self.timers.append(GLib.timeout_add_seconds(5, self._periodic_save))
            self._load_subtitles()
        self._poke_osd()

    # ---- missing codecs ----------------------------------------------------

    def _codecs_missing(self, missing):
        if self.closed:
            return
        if self.media:
            self.media.set_playing(False)
        names = "\n".join(f"• {m}" for m in missing) if missing else "• (unknown decoder)"
        actions = []
        if codecs.can_install():
            actions.append(("Install codecs", self._install_codecs, True))
        actions += [(f"Open in {name}", lambda n=name: self._open_external(n), False)
                    for name in external_players()]
        actions.append(("Choose another stream", self.leave, False))
        self._status("This video needs codecs that aren't installed", error=True,
                     detail=f"{names}\n\nThey come with FFmpeg for GStreamer. Install them here, "
                            f"or in a terminal:\n{codecs.INSTALL_COMMAND}",
                     actions=actions)
        self.osd.set_reveal_child(True)

    def _install_codecs(self):
        self._status("Installing codecs…", busy=True,
                     detail="Your system will ask for your password to install gstreamer1.0-libav.")

        def finished(err):
            if self.closed:
                return
            if err is None and codecs.ffmpeg_installed():
                self._start_playback()
            elif err is None:
                self._status("Codecs installed – restart Stremio to use them",
                             actions=[("Choose another stream", self.leave, False)])
            else:
                self._status("Couldn't install codecs", error=True,
                             detail=f"{err}\n\nRun this in a terminal instead:\n{codecs.INSTALL_COMMAND}",
                             actions=[("Copy command", lambda: Gdk.Display.get_default().get_clipboard()
                                       .set(codecs.INSTALL_COMMAND), False),
                                      ("Try again", self._install_codecs, True)])

        self.win.install_codecs(finished)

    def _check_started(self):
        if self.media and not self.media.is_prepared() and not self.media.get_error():
            self._fallback("didn't start")
        return GLib.SOURCE_REMOVE

    def _fallback(self, why):
        """Try the next-ranked stream for the same video. Returns False when none is left."""
        if self.closed or not self.alternatives:
            return False
        nxt, rest = self.alternatives[0], self.alternatives[1:]
        self.continuing = True
        self.win.toast(f"Stream {why} – trying {nxt.label}")
        self.win.replace_player(self, self.meta, self.video, nxt.stream, alternatives=rest)
        return True

    def _failed(self, err):
        if self._fallback("couldn't be opened"):
            return
        self._status(f"Can't play this stream\n{err}", error=True)

    def _on_prepared(self, media, _p):
        if not media.is_prepared():
            return
        self._find_intro()
        self._status(None)
        offset = self.win.store.resume_offset(self.meta["id"], self.video["id"])
        if offset and media.is_seekable():
            media.seek(int(offset) * 1000)
            mins, secs = divmod(int(offset / 1000), 60)
            self.win.toast(f"Resumed at {mins // 60}:{mins % 60:02d}:{secs:02d}")

    def _on_error(self, media, _p):
        err = media.get_error()
        if not err:
            return
        msg = err.message or "unknown error"
        self.osd.set_reveal_child(True)
        if not codecs.ffmpeg_installed() and self.playable:
            # GStreamer's own wording ("text pad but no video pad"…) rarely says "missing codec".
            self._status("Checking why this didn't play…", busy=True)
            run_async(codecs.probe, self.playable.url,
                      on_done=lambda missing: self._codecs_missing(missing or [msg]),
                      on_error=lambda _e: self._codecs_missing([msg]))
            return
        if self._fallback("failed"):
            return
        actions = [(f"Open in {name}", lambda n=name: self._open_external(n), False)
                   for name in external_players()]
        actions.append(("Choose another stream", self.leave, False))
        self._status("Playback error", error=True, detail=msg, actions=actions)

    def _start_stats(self):
        if self.playable and self.playable.kind == "torrent":
            self.timers.append(GLib.timeout_add_seconds(1, self._update_stats))
            self._update_stats()
        elif self.playable:
            self.stats.set_label(f"Playing via {self.playable.via}")

    def _update_stats(self):
        eng = streams._engine
        info = eng.stats(self.playable.info_hash) if eng and self.playable.info_hash else None
        if info:
            state = "" if info["has_metadata"] else "fetching metadata · "
            self.stats.set_label(f"{state}{info['peers']} peers · ↓ {_fmt_rate(info['down'])}")
        else:
            self.stats.set_label(f"Torrent via {self.playable.via}")
        return GLib.SOURCE_CONTINUE

    def toggle_fullscreen(self):
        if self.win.is_fullscreen():
            self.win.unfullscreen()
        else:
            self.win.fullscreen()

    def _on_key(self, _ctl, keyval, _code, state):
        m = self.media
        if keyval in (Gdk.KEY_f, Gdk.KEY_F11):
            self.toggle_fullscreen()
        elif keyval == Gdk.KEY_Escape:
            self.win.unfullscreen() if self.win.is_fullscreen() else self.leave()
        elif m and keyval == Gdk.KEY_space:
            m.set_playing(not m.get_playing())
        elif m and keyval in (Gdk.KEY_Left, Gdk.KEY_Right) and m.is_seekable():
            step = 10_000_000 * (6 if state & Gdk.ModifierType.SHIFT_MASK else 1)
            ts = m.get_timestamp() + (step if keyval == Gdk.KEY_Right else -step)
            m.seek(max(0, min(ts, m.get_duration() or ts)))
        elif m and keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            m.set_volume(max(0.0, min(1.0, m.get_volume() + (0.05 if keyval == Gdk.KEY_Up else -0.05))))
        elif m and keyval == Gdk.KEY_m:
            m.set_muted(not m.get_muted())
        elif keyval == Gdk.KEY_n and self.next_video:
            self.play_next()
        elif keyval == Gdk.KEY_s and self.skip_btn.get_visible():
            self.skip_intro()
        else:
            return False
        self._poke_osd()
        return True

    # ---- subtitles ---------------------------------------------------------

    def _filename(self):
        hints = self.stream.get("behaviorHints") or {}
        return hints.get("filename") or (self.stream.get("title") or "").split("\n", 1)[0]

    def _load_subtitles(self):
        addons = self.win.store.addons_for("subtitles", self.meta["type"], self.video["id"])
        if not addons:
            return
        url, filename = self.playable.url, self._filename()
        sidecar = self.stream.get("subtitlesFile")
        if sidecar and os.path.exists(sidecar):  # downloaded with the video: works offline
            self.track = subtitles.parse(open(sidecar, "rb").read())

        def fetch():
            # Same extras the official apps send, so addons can return subtitles for this exact file.
            extra = {"filename": filename} if filename else {}
            try:
                extra["videoHash"], extra["videoSize"] = subtitles.video_hash(url)
            except Exception as e:  # noqa: BLE001 - hashing is a nicety
                print(f"[subtitles] no file hash: {e}")
            found = []
            for a in addons:
                try:
                    found += [(a.name, s) for s in a.subtitles(self.meta["type"], self.video["id"], **extra)]
                except Exception as e:  # noqa: BLE001
                    print(f"[subtitles] {a.name}: {e}")
            return found

        run_async(fetch, on_done=self._got_subtitles)

    def _got_subtitles(self, found):
        if self.closed:
            return
        if not found:
            if self.track is not None:
                self.sub_btn.set_sensitive(False)
            return
        pref = self.win.store.settings.get("subtitle_lang", "eng")
        filename = self._filename()
        scored = [(source, s, subtitles.match_score(s, filename)) for source, s in found]
        scored.sort(key=lambda f: (f[1].get("lang") != pref, subtitles.lang_name(f[1].get("lang")), -f[2]))
        off = Gtk.ListBoxRow()
        off.sub = None
        off.set_child(Gtk.Label(label="Off", xalign=0))
        self.sub_list.append(off)
        counts = {}
        for source, s, score in scored:
            lang = subtitles.lang_name(s.get("lang"))
            counts[lang] = counts.get(lang, 0) + 1
            row = Gtk.ListBoxRow()
            row.sub = s
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.append(Gtk.Label(label=f"{lang} {counts[lang]}" + ("  ✓ matches this file" if score >= 60 else ""),
                                 xalign=0))
            release = s.get("movieReleaseName") or s.get("subtitleFileName")
            if release:
                box.append(Gtk.Label(label=release, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                                     max_width_chars=34, css_classes=["dim-label", "caption"]))
            row.set_child(box)
            row.set_tooltip_text(source)
            self.sub_list.append(row)
        self.sub_btn.set_sensitive(True)
        best = next(((s, score) for _, s, score in scored if s.get("lang") == pref), None)
        if best and self.track is None:
            self._select_subtitle(best[0])
            if best[1] >= 60:
                self.win.toast(f"Subtitles matched to this release ({subtitles.lang_name(pref)})")

    def _on_sub_row(self, _list, row):
        self.sub_btn.popdown()
        self._select_subtitle(row.sub)

    def _select_subtitle(self, sub):
        self.track = None
        self.sub_label.set_label("")
        if not sub:
            return
        run_async(subtitles.download, sub["url"], on_done=lambda t: setattr(self, "track", t),
                  on_error=lambda e: self.win.toast(f"Subtitle failed to load: {e}"))

    # ---- skip intro ----------------------------------------------------------

    def _binge_group(self):
        return (self.stream.get("behaviorHints") or {}).get("bingeGroup")

    def _find_intro(self):
        """Look up (or work out in the background) where this episode's intro is."""
        group = self._binge_group()
        if self._intro_looked or self.meta.get("type") != "series" or not group or not self.playable:
            return
        self._intro_looked = True
        known = intro.cached(self.video["id"], group)
        if known:
            self.intro = known
            return
        if known is False or not self.playable.url.startswith(("http", "file:")):
            return
        meta, video, url, store = self.meta, self.video, self.playable.url, self.win.store
        downloads = getattr(self.win, "downloads", None)

        def job():
            for other in (episodes.following(meta, video["id"]), episodes.preceding(meta, video["id"])):
                if not other:
                    continue
                other_url = downloads.local_uri(other["id"]) if downloads else None
                if not other_url:
                    ranked = episodes.best_stream(store, meta["type"], other["id"], group)
                    same = next((r for r in ranked if r.binge_group == group and r.stream.get("url")), None)
                    other_url = same and same.stream["url"]
                if not other_url:
                    continue
                found = intro.detect(url, other_url)
                intro.remember(video["id"], group, found and found[0])
                if found:
                    intro.remember(other["id"], group, found[1])
                    return found[0]
                return None
            return None

        def done(segment):
            if segment and not self.closed:
                self.intro = segment
                print(f"[intro] {episodes.label(video)}: {segment[0]:.0f}–{segment[1]:.0f} s")

        run_async(job, on_done=done, on_error=lambda e: print(f"[intro] detection failed: {e}"))

    def skip_intro(self):
        """Jump past the intro if it's playing or still ahead; never backwards."""
        if self.media and self.intro and self.media.get_timestamp() / 1_000_000 < self.intro[1] - 1:
            self.media.seek(int(self.intro[1] * 1_000_000))
            self.skip_btn.set_visible(False)
            return True
        return False

    def _tick_intro(self):
        if not (self.media and self.intro):
            return
        ts = self.media.get_timestamp() / 1_000_000
        inside = self.intro[0] - 0.5 <= ts < self.intro[1] - 1
        if inside and self.win.store.settings.get("auto_skip_intro") and not self._auto_skipped:
            self._auto_skipped = True
            self.skip_intro()
            self.win.toast("Skipped the intro")
            return
        self.skip_btn.set_visible(inside)

    def _tick_subtitles(self):
        self._tick_intro()
        if self.media and self.track:
            text = self.track.at(self.media.get_timestamp())
            if text != self.sub_label.get_label():
                self.sub_label.set_markup(text)
            self.sub_label.set_visible(bool(text))
        return GLib.SOURCE_CONTINUE

    # ---- progress / lifecycle ----------------------------------------------

    def _periodic_save(self):
        self._save_progress()
        m = self.media
        if (self.next_video and not self.next_choice and not self._prefetching and m and m.is_prepared()
                and m.get_duration() and m.get_duration() - m.get_timestamp() < PREFETCH_NEXT_S * 1_000_000):
            self._prefetch_next()
        return GLib.SOURCE_CONTINUE

    # ---- next episode ------------------------------------------------------

    def _prefetch_next(self, then=None):
        """Rank the next episode's streams in the background, preferring this release (bingeGroup)."""
        local = self.win.downloads.local_stream(self.next_video["id"])
        if local:  # already on the drive: no network needed
            self.next_choice = ranking.rank_one(None, local, kind=self.meta["type"])
            self.next_choice.tags = ["On your drive"]
            if then:
                then()
            return
        self._prefetching = True

        def done(ranked):
            self._prefetching = False
            good = [r for r in ranked if r.score > ranking.MIN_SCORE]
            self.next_choice = good[0] if good else None
            self.next_alternatives = good[1:6]
            if then:
                then()
            elif getattr(self, "_waiting_for_next", False):
                self._waiting_for_next = False
                self._after_lookup()

        def failed(e):
            self._prefetching = False
            print(f"[next] lookup failed: {e}")
            if then:
                then()

        run_async(episodes.best_stream, self.win.store, self.meta["type"], self.next_video["id"],
                  self.win.store.binge.get(self.meta["id"]), on_done=done, on_error=failed)

    def _on_ended(self, media, _p):
        if not media.get_ended():
            return
        self._save_progress(force=True)
        # Watched to the end: this kind of release works for you.
        self.win.store.learn(ranking.features(ranking.rank_one(None, self.stream, kind=self.meta["type"])))
        if self.next_video and self.win.store.settings.get("autoplay_next", True):
            self._start_countdown(NEXT_COUNTDOWN_S)

    def _start_countdown(self, seconds):
        self._cancel_countdown()
        self._remaining = seconds
        self._show_countdown()
        self._countdown = GLib.timeout_add_seconds(1, self._tick_countdown)

    def _show_countdown(self):
        v = self.next_video
        title = v.get("name") or v.get("title") or ""
        self._status(f"Up next: {episodes.label(v)}" + (f" · {title}" if title else ""),
                     detail=f"Starting in {self._remaining} s",
                     actions=[("Play now", self.play_next, True), ("Cancel", self._cancel_countdown_ui, False)])
        self.osd.set_reveal_child(True)

    def _tick_countdown(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._countdown = None
            self.play_next()
            return GLib.SOURCE_REMOVE
        self._show_countdown()
        return GLib.SOURCE_CONTINUE

    def _cancel_countdown(self):
        if self._countdown:
            GLib.source_remove(self._countdown)
            self._countdown = None

    def _cancel_countdown_ui(self):
        self._cancel_countdown()
        self._status(None)

    def play_next(self):
        if self.closed or not self.next_video:
            return
        self._cancel_countdown()
        if self.next_choice:
            self._go_next(self.next_choice)
            return
        self._status(f"Finding the best stream for {episodes.label(self.next_video)}…", busy=True)
        if not self._prefetching:
            self._prefetch_next(then=self._after_lookup)
        else:
            self._waiting_for_next = True

    def _after_lookup(self):
        if self.closed:
            return
        if self.next_choice:
            self._go_next(self.next_choice)
        else:
            self._status(f"No good stream found for {episodes.label(self.next_video)}", error=True,
                         actions=[("Choose a stream", self.leave, True)])

    def _go_next(self, choice):
        self.continuing = True
        self.win.toast(f"{episodes.label(self.next_video)}: {choice.label}")
        self.win.replace_player(self, self.meta, self.next_video, choice.stream,
                                alternatives=getattr(self, "next_alternatives", []))

    def _save_progress(self, force=False):
        m = self.media
        if not m or not m.is_prepared() or not m.get_duration():
            return
        dur = m.get_duration()
        ts = dur if m.get_ended() else m.get_timestamp()  # GTK reports 0 once a stream has ended
        if not force and abs(ts - self.last_saved) < SAVE_EVERY_S * 1_000_000:
            return
        self.last_saved = ts
        self.win.store.save_progress(self.meta, self.video["id"], ts / 1000, dur / 1000)

    def leave(self):
        self.win.nav.pop()

    def _teardown(self):
        if self.closed:
            return
        self.closed = True
        self._cancel_countdown()
        self._save_progress(force=True)
        for t in self.timers:
            GLib.source_remove(t)
        self.timers = []
        if self.media:
            self.media.set_playing(False)
            self.video_widget.set_media_stream(None)
            self.media = None
        if self.win.is_fullscreen() and not self.continuing:
            self.win.unfullscreen()
        self.set_cursor(None)
        player = self.win.store.settings.get("player", "builtin")
        if (self.playable and self.playable.info_hash and streams._engine and player == "builtin"
                and not self.handed_off):
            streams._engine.remove(self.playable.info_hash)

    def _open_external(self, name):
        if self.playable:
            ts = self.media.get_timestamp() / 1_000_000 if self.media else 0
            if self.media:
                self.media.set_playing(False)
            self.handed_off = True
            launch_external(name, self.playable.url, self.get_title(), ts)
