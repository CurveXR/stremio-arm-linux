"""Main window: navigation stack with the Board / Discover / Library / Addons views."""

from gi.repository import Adw, Gdk, Gio, GObject, Gtk

from ..core import codecs
from ..core.util import run_async
from ..core.downloads import DownloadManager
from .addons_page import AddonsPage
from .downloads_page import DownloadsPage
from .browse import BoardPage, DiscoverPage, LibraryPage, SearchPage
from .detail import DetailPage
from .player import PlayerPage
from .preferences import PreferencesDialog


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, store):
        super().__init__(application=app, title="Stremio", default_width=1366, default_height=820)
        self.store = store
        self.downloads = DownloadManager(store)
        self._installing = False
        self._install_waiters = []
        self.sub_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.sub_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.apply_subtitle_style()

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        self.stack = Adw.ViewStack()
        self.board = BoardPage(self)
        self.discover = DiscoverPage(self)
        self.library = LibraryPage(self)
        self.addons = AddonsPage(self)
        self.downloads_page = DownloadsPage(self)
        for widget, name, title, icon in (
            (self.board, "board", "Board", "go-home-symbolic"),
            (self.discover, "discover", "Discover", "view-grid-symbolic"),
            (self.library, "library", "Library", "starred-symbolic"),
            (self.downloads_page, "downloads", "Downloads", "folder-download-symbolic"),
            (self.addons, "addons", "Addons", "application-x-addon-symbolic"),
        ):
            self.stack.add_titled_with_icon(widget, name, title, icon)

        header = Adw.HeaderBar(title_widget=Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text="Search (Ctrl+F)")
        header.pack_start(self.search_btn)
        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("Keyboard Shortcuts", "app.shortcuts")
        menu.append("About Stremio Native", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))

        self.search_entry = Gtk.SearchEntry(placeholder_text="Search movies and series", hexpand=True)
        self.search_entry.connect("activate", self._on_search)
        clamp = Adw.Clamp(maximum_size=560, child=self.search_entry)
        self.search_bar = Gtk.SearchBar(child=clamp, show_close_button=False)
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.set_key_capture_widget(self.stack)
        self.search_btn.bind_property("active", self.search_bar, "search-mode-enabled",
                                      GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.add_top_bar(self.search_bar)
        tv.set_content(self.stack)
        self.nav.add(Adw.NavigationPage(child=tv, title="Stremio", tag="home"))

        search_action = Gio.SimpleAction.new("search", None)
        search_action.connect("activate", lambda *_: self._focus_search())
        self.add_action(search_action)

        store.connect("error", lambda _s, msg: self.toast(f"Sync problem: {msg}"))

    # ---- navigation --------------------------------------------------------

    def _focus_search(self):
        if self.nav.get_visible_page().get_tag() != "home":
            self.nav.pop_to_tag("home")
        self.search_btn.set_active(True)
        self.search_entry.grab_focus()

    def _on_search(self, entry):
        query = entry.get_text().strip()
        if query:
            if self.nav.find_page("search"):
                self.nav.pop_to_tag("home")
            self.nav.push(SearchPage(self, query))

    def open_meta(self, meta):
        if meta.get("id") and meta.get("type"):
            self.nav.push(DetailPage(self, meta))

    def show_discover(self, addon, catalog):
        self.stack.set_visible_child_name("discover")
        self.discover.select(addon, catalog)

    def play(self, meta, video, stream, alternatives=None):
        self.nav.push(PlayerPage(self, meta, video, stream, alternatives))

    def replace_player(self, old, meta, video, stream, alternatives=None):
        """Swap the playing episode for the next one without leaving the player (or fullscreen)."""
        old._teardown()
        stack = self.nav.get_navigation_stack()
        pages = [stack.get_item(i) for i in range(stack.get_n_items())]
        pages = [p for p in pages if p is not old] + [PlayerPage(self, meta, video, stream, alternatives)]
        self.nav.replace(pages)

    def show_preferences(self):
        PreferencesDialog(self).present(self)

    def install_codecs(self, on_done=None):
        """Install FFmpeg's GStreamer plugin via PackageKit (the system prompts for the password)."""
        if on_done:
            self._install_waiters.append(on_done)
        if self._installing:
            return
        self._installing = True
        self.toast("Installing codecs – enter your password when your system asks")

        def done(_):
            self._installing = False
            ok = codecs.reload_plugins()
            self.toast("Codecs installed" if ok else "Codecs installed – restart Stremio to use them")
            self.emit_codecs_changed()
            self._notify_install(None)

        def failed(e):
            self._installing = False
            self.toast(f"Couldn't install codecs ({e}). Run: {codecs.INSTALL_COMMAND}")
            self._notify_install(e)

        run_async(codecs.install, on_done=done, on_error=failed)

    def _notify_install(self, err):
        waiters, self._install_waiters = self._install_waiters, []
        for cb in waiters:
            cb(err)

    def emit_codecs_changed(self):
        page = self.nav.get_visible_page()
        banner = getattr(page, "codec_banner", None)
        if banner:
            banner.set_revealed(not codecs.ffmpeg_installed())

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))

    def apply_subtitle_style(self):
        size = int(self.store.settings.get("subtitle_size", 28))
        self.sub_css.load_from_string(f"label.subtitle {{ font-size: {size}px; }}")
