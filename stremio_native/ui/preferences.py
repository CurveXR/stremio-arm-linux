"""Settings dialog: account, playback and streaming options."""

from gi.repository import Adw, Gtk

from ..core import torrent
from ..core.subtitles import LANGS
from ..core.util import get_json, run_async
from .player import external_players

PLAYERS = [("builtin", "Built-in"), ("mpv", "mpv"), ("vlc", "VLC"), ("celluloid", "Celluloid")]
ENGINES = [("auto", "Automatic"), ("builtin", "Built-in (libtorrent)"), ("server", "Stremio streaming server")]
SUB_LANGS = sorted({code: name for code, name in LANGS.items() if len(code) == 3}.items(), key=lambda kv: kv[1])


def _combo(title, options, current, on_change, subtitle=None):
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new([label for _, label in options]))
    if subtitle:
        row.set_subtitle(subtitle)
    keys = [k for k, _ in options]
    row.set_selected(keys.index(current) if current in keys else 0)
    row.connect("notify::selected", lambda r, _p: on_change(keys[r.get_selected()]))
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferences")
        self.win = win
        store = win.store
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.add(page)

        # Account
        self.account = Adw.PreferencesGroup(title="Stremio account",
                                            description="Sign in to sync your addons and library.")
        page.add(self.account)
        self.account_rows = []
        self._build_account()
        self._acct_handler = store.connect("account-changed", lambda *_: self._build_account())
        self.connect("closed", lambda *_: store.disconnect(self._acct_handler))

        # Playback
        playback = Adw.PreferencesGroup(title="Playback")
        installed = external_players()
        players = [p for p in PLAYERS if p[0] == "builtin" or p[0] in installed]
        playback.add(_combo("Player", players, store.settings["player"],
                            lambda v: store.set_setting("player", v),
                            subtitle=None if installed else "Install mpv or VLC to use an external player"))
        langs = [(c, n) for c, n in SUB_LANGS]
        playback.add(_combo("Preferred subtitle language", langs, store.settings["subtitle_lang"],
                            lambda v: store.set_setting("subtitle_lang", v)))
        size = Adw.SpinRow.new_with_range(14, 64, 2)
        size.set_title("Subtitle size")
        size.set_value(store.settings["subtitle_size"])
        size.connect("notify::value", lambda r, _p: (store.set_setting("subtitle_size", int(r.get_value())),
                                                     win.apply_subtitle_style()))
        playback.add(size)
        embedded = Adw.SwitchRow(title="Show subtitles built into the video",
                                 subtitle="Off avoids double subtitles when an addon track is shown",
                                 active=store.settings["embedded_subtitles"])
        embedded.connect("notify::active", lambda r, _p: store.set_setting("embedded_subtitles", r.get_active()))
        playback.add(embedded)
        autonext = Adw.SwitchRow(title="Play next episode automatically",
                                 subtitle="Picks the same release when possible",
                                 active=store.settings["autoplay_next"])
        autonext.connect("notify::active", lambda r, _p: store.set_setting("autoplay_next", r.get_active()))
        playback.add(autonext)
        autoskip = Adw.SwitchRow(title="Skip intros automatically",
                                 subtitle="Otherwise a Skip intro button appears during the opening",
                                 active=store.settings["auto_skip_intro"])
        autoskip.connect("notify::active", lambda r, _p: store.set_setting("auto_skip_intro", r.get_active()))
        playback.add(autoskip)
        page.add(playback)

        # Downloads
        dl_group = Adw.PreferencesGroup(title="Downloads")
        self.dl_row = Adw.ActionRow(title="Download folder", subtitle=win.downloads.root)
        pick = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        pick.connect("clicked", lambda *_: (win.downloads_page._choose_folder(), self.close()))
        self.dl_row.add_suffix(pick)
        dl_group.add(self.dl_row)
        page.add(dl_group)

        # Streaming
        streaming = Adw.PreferencesGroup(title="Torrent streaming")
        engine_sub = ("libtorrent found" if torrent.available()
                      else "libtorrent not found – sudo apt install python3-libtorrent")
        streaming.add(_combo("Engine", ENGINES, store.settings["torrent_engine"],
                             lambda v: store.set_setting("torrent_engine", v), subtitle=engine_sub))
        server = Adw.EntryRow(title="Streaming server URL", text=store.settings["server_url"],
                              show_apply_button=True)
        server.connect("apply", lambda r: (store.set_setting("server_url", r.get_text().strip()),
                                           self._check_server()))
        streaming.add(server)
        self.server_status = Adw.ActionRow(title="Streaming server", subtitle="Checking…")
        streaming.add(self.server_status)
        page.add(streaming)
        self._check_server()

    def _build_account(self):
        for r in self.account_rows:
            self.account.remove(r)
        self.account_rows = []
        store = self.win.store
        if store.logged_in:
            row = Adw.ActionRow(title=store.user.get("email", "Signed in"), subtitle="Signed in")
            row.add_prefix(Adw.Avatar(size=32, text=store.user.get("email", "?"), show_initials=True))
            out = Gtk.Button(label="Sign out", valign=Gtk.Align.CENTER)
            out.connect("clicked", lambda *_: store.logout())
            row.add_suffix(out)
            sync = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                              tooltip_text="Sync now")
            sync.connect("clicked", lambda *_: (store.pull(), self.win.toast("Syncing…")))
            row.add_suffix(sync)
            rows = [row]
        else:
            email = Adw.EntryRow(title="Email")
            password = Adw.PasswordEntryRow(title="Password")
            btn_row = Adw.ActionRow()
            btn = Gtk.Button(label="Sign in", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
            btn_row.add_suffix(btn)
            self.login_error = Gtk.Label(css_classes=["error", "caption"], wrap=True, xalign=0)
            btn_row.add_prefix(self.login_error)

            def submit(*_):
                if not email.get_text() or not password.get_text():
                    return
                btn.set_sensitive(False)
                self.login_error.set_label("")

                def ok():
                    self.win.toast("Signed in – syncing your addons and library")

                def fail(e):
                    btn.set_sensitive(True)
                    self.login_error.set_label(str(e))

                store.login(email.get_text().strip(), password.get_text(), ok, fail)

            btn.connect("clicked", submit)
            password.connect("entry-activated", submit)
            rows = [email, password, btn_row]
        for r in rows:
            self.account.add(r)
            self.account_rows.append(r)

    def _check_server(self):
        url = self.win.store.settings["server_url"].rstrip("/")
        run_async(get_json, f"{url}/settings", 3,
                  on_done=lambda _r: self.server_status.set_subtitle(f"Running at {url}"),
                  on_error=lambda _e: self.server_status.set_subtitle(
                      f"Not running at {url} (only needed when the built-in engine is unavailable)"))
