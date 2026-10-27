"""Details page: metadata, seasons/episodes and the aggregated stream list."""

from datetime import datetime

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..core import codecs, episodes, ranking
from ..core import streams as streams_mod
from ..core.util import run_async
from .widgets import clear, set_picture, spinner

SIDE_WIDTH = 360


def _date(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%b %-d, %Y")
    except (AttributeError, ValueError):
        return ""


def fetch_meta(store, type_, id_):
    errors = []
    for addon in store.addons_for("meta", type_, id_):
        try:
            meta = addon.meta(type_, id_)
            if meta:
                return meta
        except Exception as e:  # noqa: BLE001 - try the next addon
            errors.append(f"{addon.name}: {e}")
    raise LookupError("; ".join(errors) or "No installed addon can describe this title")


class DetailPage(Adw.NavigationPage):
    def __init__(self, win, preview):
        super().__init__(title=preview.get("name") or "Details")
        self.win = win
        self.meta = dict(preview)

        overlay = Gtk.Overlay(css_classes=["detail"])
        self.bg = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, css_classes=["detail-bg"])
        overlay.set_child(self.bg)

        tv = Adw.ToolbarView(extend_content_to_top_edge=True)
        header = Adw.HeaderBar(css_classes=["flat"], show_title=False)
        tv.add_top_bar(header)
        body = Gtk.Box(css_classes=["detail-scrim"])
        tv.set_content(body)
        overlay.add_overlay(tv)
        overlay.set_measure_overlay(tv, True)  # the backdrop must not decide the page's size
        self.set_child(overlay)

        self.info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                            margin_top=56, margin_bottom=28, margin_start=28, margin_end=20)
        body.append(Gtk.ScrolledWindow(hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.info))

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["side-panel"])
        side.set_size_request(SIDE_WIDTH, -1)
        self.side_stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        side.append(self.side_stack)
        body.append(side)

        self._install_download_actions()
        self._build_episodes_view()
        self._build_streams_view()
        self.info.append(spinner())

        run_async(fetch_meta, win.store, preview["type"], preview["id"],
                  on_done=self._show_meta, on_error=self._meta_failed)
        self.lib_handler = win.store.connect("library-changed", lambda *_: self._library_changed())
        self.connect("destroy", lambda *_: win.store.disconnect(self.lib_handler))

    # ---- metadata ----------------------------------------------------------

    def _meta_failed(self, err):
        self._show_meta(self.meta, error=str(err))

    def _show_meta(self, meta, error=None):
        self.meta = {**self.meta, **meta}
        m = self.meta
        self.set_title(m.get("name", ""))
        clear(self.info)
        if m.get("background"):
            set_picture(self.bg, m["background"])

        if m.get("logo"):
            logo = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, halign=Gtk.Align.START,
                               css_classes=["detail-logo"])
            logo.set_size_request(300, 96)
            set_picture(logo, m["logo"])
            self.info.append(logo)
            logo.set_tooltip_text(m.get("name"))
        else:
            self.info.append(Gtk.Label(label=m.get("name", ""), xalign=0, wrap=True, css_classes=["title-1"]))

        facts = [str(x) for x in (m.get("releaseInfo") or m.get("year"), m.get("runtime")) if x]
        if m.get("imdbRating"):
            facts.append(f"★ {m['imdbRating']}")
        if facts:
            self.info.append(Gtk.Label(label="   ·   ".join(facts), xalign=0, css_classes=["heading"]))
        genres = m.get("genres") or m.get("genre") or []
        if genres:
            self.info.append(Gtk.Label(label=" / ".join(genres), xalign=0, css_classes=["dim-label"]))

        buttons = Gtk.Box(spacing=8, margin_top=6)
        self.play_btn = Gtk.Button(css_classes=["pill", "suggested-action"])
        self.play_btn.connect("clicked", lambda *_: self.smart_play())
        buttons.append(self.play_btn)
        self._finding = False
        self._update_play_button()
        self.lib_btn = Gtk.Button(css_classes=["pill"])
        self.lib_btn.connect("clicked", self._toggle_library)
        buttons.append(self.lib_btn)
        self._update_library_button()
        trailer = next((t for t in m.get("trailerStreams") or [] if t.get("ytId")), None)
        if trailer:
            tb = Gtk.Button(icon_name="camera-video-symbolic", tooltip_text="Watch trailer",
                            css_classes=["circular"], valign=Gtk.Align.CENTER)
            tb.connect("clicked", lambda *_: Gtk.UriLauncher.new(
                f"https://www.youtube.com/watch?v={trailer['ytId']}").launch(self.win, None, None))
            buttons.append(tb)
        imdb = m.get("imdb_id") or (m["id"] if str(m.get("id", "")).startswith("tt") else None)
        if imdb:
            ib = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Open on IMDb",
                            css_classes=["circular"], valign=Gtk.Align.CENTER)
            ib.connect("clicked", lambda *_: Gtk.UriLauncher.new(
                f"https://www.imdb.com/title/{imdb}/").launch(self.win, None, None))
            buttons.append(ib)
        self.info.append(buttons)

        if m.get("description"):
            desc = Gtk.Label(label=m["description"], xalign=0, wrap=True, css_classes=["body", "detail-desc"],
                             max_width_chars=70, selectable=True)
            self.info.append(desc)
        for label, key in (("Director", "director"), ("Writer", "writer"), ("Cast", "cast")):
            vals = m.get(key)
            if vals:
                row = Gtk.Box(spacing=12)
                row.append(Gtk.Label(label=label, xalign=0, width_chars=8, valign=Gtk.Align.START,
                                     css_classes=["dim-label"]))
                row.append(Gtk.Label(label=", ".join(vals if isinstance(vals, list) else [vals]),
                                     xalign=0, wrap=True, hexpand=True))
                self.info.append(row)
        if error:
            self.info.append(Gtk.Label(label=f"Some details are missing: {error}", xalign=0, wrap=True,
                                       css_classes=["warning", "caption"]))

        videos = [v for v in m.get("videos") or [] if v.get("id")]
        if m.get("type") != "movie" and len(videos) > 0:
            self._populate_seasons(videos)
            self.side_stack.set_visible_child_name("episodes")
        else:
            video_id = (m.get("behaviorHints") or {}).get("defaultVideoId") or m["id"]
            self.show_streams({"id": video_id, "title": m.get("name")}, back=False)

    def _library_changed(self):
        self._update_library_button()
        self._update_play_button()

    # ---- smart play --------------------------------------------------------

    def _play_target(self):
        """(video, button label) for the one-click Play button."""
        m = self.meta
        if m.get("type") != "movie" and episodes.ordered(m):
            state = (self.win.store.library.get(m["id"]) or {}).get("state")
            video, verb = episodes.up_next(m, state)
            return video, f"{verb} {episodes.label(video)}"
        video_id = (m.get("behaviorHints") or {}).get("defaultVideoId") or m["id"]
        resume = self.win.store.resume_offset(m["id"], video_id)
        return {"id": video_id, "title": m.get("name")}, "Resume" if resume else "Play"

    def _update_play_button(self):
        if not hasattr(self, "play_btn") or self._finding:
            return
        video, text = self._play_target()
        self.play_btn.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic", label=text))
        self.play_btn.set_tooltip_text("Plays the best stream for this device")

    def smart_play(self, video=None):
        if self._finding:
            return
        if video is None:
            video, _ = self._play_target()
        local = self.win.downloads.local_stream(video["id"])
        if local:
            self.win.toast("Playing from your drive")
            self.win.play(self.meta, video, local)
            return
        self._finding = True
        self.play_btn.set_sensitive(False)
        box = Gtk.Box(spacing=8)
        box.append(spinner(16))
        box.append(Gtk.Label(label="Finding the best stream…"))
        self.play_btn.set_child(box)

        def done(ranked):
            self._finding = False
            self.play_btn.set_sensitive(True)
            self._update_play_button()
            if ranked and ranked[0].score > ranking.MIN_SCORE:
                best = ranked[0]
                self.win.toast(f"Playing {best.label}")
                others = [r for r in ranked[1:] if r.score > ranking.MIN_SCORE]
                self.win.play(self.meta, video, best.stream, alternatives=others[:5])
            else:
                self.show_streams(video, back=self.meta.get("type") != "movie")
                self.win.toast("No stream looks right for this device – pick one from the list")

        def failed(e):
            self._finding = False
            self.play_btn.set_sensitive(True)
            self._update_play_button()
            self.win.toast(f"Couldn't fetch streams: {e}")

        run_async(episodes.best_stream, self.win.store, self.meta["type"], video["id"],
                  self.win.store.binge.get(self.meta["id"]), on_done=done, on_error=failed)


    # ---- downloads ---------------------------------------------------------

    def _install_download_actions(self):
        group = Gio.SimpleActionGroup()
        for name, cb in (("dl-next", lambda: self._download([self._play_target()[0]])),
                         ("dl-next3", self._download_next3),
                         ("dl-season", self._download_season)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            group.add_action(a)
        self.insert_action_group("detail", group)

    def _download_next3(self):
        start, _ = self._play_target()
        eps = episodes.ordered(self.meta)
        i = next((k for k, v in enumerate(eps) if v["id"] == start["id"]), 0)
        self._download(eps[i:i + 3])

    def _download_season(self):
        season = self.seasons[min(self.season_dd.get_selected(), len(self.seasons) - 1)]
        self._download([v for v in self.by_season[season] if v in episodes.ordered(self.meta)])

    def _download(self, videos):
        videos = [v for v in videos if v]
        if not videos:
            return
        self.win.toast(f"Finding the best stream for {len(videos)} download{'s' if len(videos) > 1 else ''}…")

        def done(added, skipped):
            msg = f"Downloading {added} to {self.win.downloads.root}" if added else "Nothing new to download"
            if skipped:
                msg += f" ({len(skipped)} had no good stream)"
            self.win.toast(msg)
            self._show_season()

        self.win.downloads.add_best(self.meta, videos, on_done=done)

    def _on_stream_right_click(self, gesture, _n, _x, y):
        row = self.stream_list.get_row_at_y(int(y))
        ranked = getattr(row, "ranked", None)
        if not ranked:
            return
        pop = Gtk.Popover()
        btn = Gtk.Button(label="Download this stream", css_classes=["flat"])
        btn.connect("clicked", lambda *_: (pop.popdown(), self.win.downloads.add(self.meta, self.video, ranked),
                                           self.win.toast(f"Downloading {ranked.label}")))
        pop.set_child(btn)
        pop.set_parent(row)
        pop.popup()

    def _update_library_button(self):
        if not hasattr(self, "lib_btn"):
            return
        inside = self.win.store.in_library(self.meta["id"])
        self.lib_btn.set_child(Adw.ButtonContent(
            icon_name="object-select-symbolic" if inside else "list-add-symbolic",
            label="In Library" if inside else "Add to Library"))

    def _toggle_library(self, *_):
        self.win.store.set_in_library(self.meta, not self.win.store.in_library(self.meta["id"]))

    # ---- seasons / episodes ------------------------------------------------

    def _build_episodes_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=8, margin_top=56, margin_start=12, margin_end=12, margin_bottom=8)
        head.append(Gtk.Label(label="Episodes", xalign=0, hexpand=True, css_classes=["title-4"]))
        self.season_dd = Gtk.DropDown.new_from_strings([])
        self.season_dd.connect("notify::selected", lambda *_: self._show_season())
        head.append(self.season_dd)
        menu = Gio.Menu()
        menu.append("Download next up", "detail.dl-next")
        menu.append("Download the next 3 episodes", "detail.dl-next3")
        menu.append("Download this season", "detail.dl-season")
        head.append(Gtk.MenuButton(icon_name="folder-download-symbolic", menu_model=menu,
                                   css_classes=["flat", "circular"], tooltip_text="Download for offline"))
        box.append(head)
        self.episode_list = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.NONE)
        self.episode_list.connect("row-activated", lambda _l, row: self.show_streams(row.video))
        box.append(Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      child=self.episode_list))
        self.side_stack.add_named(box, "episodes")

    def _populate_seasons(self, videos):
        by_season = {}
        for v in videos:
            by_season.setdefault(v.get("season", 0) or 0, []).append(v)
        for eps in by_season.values():
            eps.sort(key=lambda v: (v.get("episode") or v.get("number") or 0, v.get("released") or ""))
        self.seasons = sorted(by_season, key=lambda s: (s == 0, s))
        self.by_season = by_season
        names = ["Specials" if s == 0 else f"Season {s}" for s in self.seasons]
        self.season_dd.set_model(Gtk.StringList.new(names))
        last = (self.win.store.library.get(self.meta["id"]) or {}).get("state", {}).get("video_id")
        start = next((s for s, eps in by_season.items() if any(v["id"] == last for v in eps)), None)
        self.season_dd.set_selected(self.seasons.index(start) if start in self.seasons else 0)
        self._show_season()

    def _show_season(self):
        if not getattr(self, "seasons", None):
            return
        clear(self.episode_list)
        season = self.seasons[min(self.season_dd.get_selected(), len(self.seasons) - 1)]
        state = (self.win.store.library.get(self.meta["id"]) or {}).get("state") or {}
        for v in self.by_season[season]:
            row = Gtk.ListBoxRow()
            row.video = v
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6)
            thumb = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, css_classes=["episode-thumb"])
            thumb.set_size_request(128, 72)
            if v.get("thumbnail"):
                set_picture(thumb, v["thumbnail"])
            box.append(thumb)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
            num = v.get("episode") or v.get("number")
            title = v.get("name") or v.get("title") or f"Episode {num}"
            text.append(Gtk.Label(label=f"{num}. {title}" if num else title, xalign=0, wrap=True,
                                  lines=2, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"]))
            when = _date(v.get("released") or v.get("firstAired"))
            sub = []
            if when:
                sub.append(when)
            if state.get("video_id") == v["id"] and state.get("timeOffset"):
                sub.append("In progress")
            dl = self.win.downloads.job(v["id"])
            if dl:
                sub.append("⬇ On your drive" if dl["status"] == "done" else "⬇ " + dl["status"].title())
            if sub:
                text.append(Gtk.Label(label=" · ".join(sub), xalign=0, css_classes=["dim-label", "caption"]))
            box.append(text)
            row.set_child(box)
            self.episode_list.append(row)

    # ---- streams -----------------------------------------------------------

    def _build_streams_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=6, margin_top=56, margin_start=8, margin_end=12, margin_bottom=4)
        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat", "circular"])
        self.back_btn.connect("clicked", lambda *_: self.side_stack.set_visible_child_name("episodes"))
        head.append(self.back_btn)
        self.streams_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                                       css_classes=["title-4"])
        head.append(self.streams_title)
        dl = Gtk.Button(icon_name="folder-download-symbolic", css_classes=["flat", "circular"],
                        tooltip_text="Download the best stream (right-click a stream to download that one)")
        dl.connect("clicked", lambda *_: self._download([self.video]))
        head.append(dl)
        box.append(head)

        chips = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_bottom=6)
        saved = self.win.store.settings.get("stream_filters") or {}
        self.filters = {}
        for key, label, tip in (("instant", "Instant", "Only streams your debrid service has cached"),
                                ("max1080", "Up to 1080p", "Hide 4K / HDR releases"),
                                ("mylang", "My language", "Hide dubs in other languages")):
            chip = Gtk.ToggleButton(label=label, tooltip_text=tip, active=bool(saved.get(key)),
                                    css_classes=["chip"])
            chip.connect("toggled", self._on_filter)
            self.filters[key] = chip
            chips.append(chip)
        self.filter_count = Gtk.Label(hexpand=True, xalign=1, css_classes=["dim-label", "caption"])
        chips.append(self.filter_count)
        box.append(chips)

        self.codec_banner = Adw.Banner(title="Many streams need extra codecs (Dolby audio, XviD…)",
                                       button_label="Install codecs", use_markup=False,
                                       revealed=not codecs.ffmpeg_installed() and codecs.can_install())
        self.codec_banner.connect("button-clicked", lambda *_: self.win.install_codecs())
        box.append(self.codec_banner)
        self.stream_list = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.NONE)
        self.stream_list.connect("row-activated", self._on_stream)
        self.stream_list.set_filter_func(self._row_visible)
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", self._on_stream_right_click)
        self.stream_list.add_controller(right)
        box.append(Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      child=self.stream_list))
        self.stream_status = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=12, margin_bottom=12)
        box.append(self.stream_status)
        self.side_stack.add_named(box, "streams")

    def show_streams(self, video, back=True):
        self.video = video
        self.back_btn.set_visible(back)
        num = video.get("episode") or video.get("number")
        title = video.get("name") or video.get("title") or ""
        self.streams_title.set_label(f"S{video.get('season')}E{num} · {title}" if num and back else "Streams")
        self.side_stack.set_visible_child_name("streams")
        clear(self.stream_list)
        clear(self.stream_status)
        self._stream_gen = getattr(self, "_stream_gen", 0) + 1
        gen, type_ = self._stream_gen, self.meta["type"]
        addons = self.win.store.addons_for("stream", type_, video["id"])
        if not addons:
            self._stream_message("No stream addons installed.\nAdd one on the Addons page.")
            return
        self.stream_status.append(spinner(24))
        self.pending, self.found = len(addons), 0
        self.collected, self.ranked = [], []
        for addon in addons:
            run_async(addon.streams, type_, video["id"],
                      on_done=lambda s, a=addon: self._add_streams(gen, a, s),
                      on_error=lambda e, a=addon: self._add_streams(gen, a, [], e))

    def _stream_message(self, text):
        clear(self.stream_status)
        self.stream_status.append(Gtk.Label(label=text, justify=Gtk.Justification.CENTER, wrap=True,
                                            css_classes=["dim-label"], margin_start=12, margin_end=12))

    def _add_streams(self, gen, addon, streams, err=None):
        if gen != self._stream_gen:
            return
        self.pending -= 1
        self.found += len(streams)
        self.collected += [(addon, s) for s in streams]
        if err:
            print(f"[streams] {addon.name}: {err}")
        if streams:
            self._render_streams()
        if not self.pending:
            clear(self.stream_status)
            if not self.found:
                self._stream_message("No streams found for this title.")
            self._update_filter_count()

    def _ranked(self):
        store = self.win.store
        return ranking.rank(self.collected, kind=self.meta["type"], lang=store.settings.get("subtitle_lang", "eng"),
                            binge_group=store.binge.get(self.meta["id"]), prefs=store.prefs)

    def _render_streams(self):
        """One list for all addons, best first, each row with badges and the release name."""
        clear(self.stream_list)
        self.ranked = self._ranked()
        for i, r in enumerate(self.ranked):
            row = Gtk.ListBoxRow()
            row.ranked, row.stream = r, r.stream
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                          margin_top=6, margin_bottom=6, margin_start=4, margin_end=4)
            if i == 0 and r.score > ranking.MIN_SCORE:
                row.add_css_class("best-row")
                box.append(Gtk.Label(label="★ Best for this device", xalign=0, css_classes=["heading"]))
            box.append(Gtk.Label(label=_badges(r), use_markup=True, xalign=0, wrap=True))
            if r.release:
                box.append(Gtk.Label(label=r.release, xalign=0, wrap=True, lines=2,
                                     wrap_mode=Pango.WrapMode.WORD_CHAR, ellipsize=Pango.EllipsizeMode.END,
                                     css_classes=["caption"]))
            origin = r.addon.name + (f" · {r.source}" if r.source else "")
            box.append(Gtk.Label(label=origin, xalign=0, css_classes=["dim-label", "caption"]))
            row.set_child(box)
            self.stream_list.append(row)
        self._update_filter_count()

    # ---- filters -----------------------------------------------------------

    def _on_filter(self, *_):
        state = {k: chip.get_active() for k, chip in self.filters.items()}
        self.win.store.set_setting("stream_filters", state)
        self.stream_list.invalidate_filter()
        self._update_filter_count()

    def _row_visible(self, row):
        r = getattr(row, "ranked", None)
        if r is None:
            return True
        f = {k: chip.get_active() for k, chip in self.filters.items()}
        if f["instant"] and not r.instant:
            return False
        if f["max1080"] and (r.resolution > 1080 or r.hdr):
            return False
        if f["mylang"] and r.foreign:
            return False
        return True

    def _update_filter_count(self):
        ranked = getattr(self, "ranked", [])
        shown = sum(1 for i in range(len(ranked)) if self._row_visible(self.stream_list.get_row_at_index(i)))
        self.filter_count.set_label(f"{shown} of {len(ranked)}" if ranked else "")
        if ranked and not shown and not self.pending:
            self._stream_message("No streams match these filters.")
        elif ranked and not self.pending:
            clear(self.stream_status)

    def _on_stream(self, _list, row):
        chosen = getattr(row, "ranked", None)
        if not chosen:
            return
        others = [r for r in self.ranked if r is not chosen and r.score > ranking.MIN_SCORE]
        if self.ranked and chosen is not self.ranked[0]:
            self.win.store.learn(ranking.features(chosen))  # you overrode the pick: remember what you like
        self.win.play(self.meta, self.video, chosen.stream, alternatives=others[:5])


def _badges(r):
    """Pango markup: colour-coded facts about a stream."""
    def span(text, color=None, bold=False):
        attrs = (f' foreground="{color}"' if color else "") + (' weight="bold"' if bold else "")
        return f"<span{attrs}>{GLib.markup_escape_text(text)}</span>"

    parts = []
    if r.instant:
        parts.append(span("● Instant", "#57e389", True))
    elif r.uncached:
        parts.append(span("○ Not cached", "#9a9996"))
    elif r.stream.get("infoHash"):
        parts.append(span("Torrent", "#99c1f1"))
    if r.resolution:
        parts.append(span("4K" if r.resolution == 2160 else f"{r.resolution}p", bold=True))
    if r.hdr:
        parts.append(span("HDR", "#ffa348"))
    if r.codec:
        parts.append(span(r.codec))
    if r.size_gb is not None:
        parts.append(span(f"{r.size_gb:.1f} GB" if r.size_gb >= 1 else f"{r.size_gb * 1024:.0f} MB"))
    if r.seeds is not None:
        parts.append(span(f"👤 {r.seeds}"))
    if r.foreign:
        parts.append(span("Other language", "#f8e45c"))
    if r.pack:
        parts.append(span("Season pack", "#9a9996"))
    if r.needs:
        parts.append(span("Needs codecs", "#ff7b63", True))
    if not (r.stream.get("url") or r.stream.get("infoHash")):
        parts.append(span("Opens externally", "#9a9996"))
    return "  ".join(parts) or span("Stream")
