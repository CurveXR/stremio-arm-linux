"""Browsing views: Board (home), Discover, Library and Search results."""

from gi.repository import Adw, Gtk

from ..core.addons import catalog_extras, catalog_genres, catalog_title
from ..core.util import run_async
from .widgets import CatalogRow, PosterGrid, clear, spinner

ROW_LIMIT = 25


def library_meta(item):
    return {"id": item["_id"], "type": item.get("type"), "name": item.get("name"), "poster": item.get("poster")}


def library_progress(meta, items):
    state = (items.get(meta["id"]) or {}).get("state") or {}
    if state.get("duration"):
        return state.get("timeOffset", 0) / state["duration"]
    return None


class BoardPage(Gtk.ScrolledWindow):
    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.win = win
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                           margin_top=18, margin_bottom=24, margin_start=24, margin_end=24)
        self.set_child(self.box)
        self.continue_row = CatalogRow("Continue Watching", win.open_meta)
        win.store.connect("addons-changed", lambda *_: self.rebuild())
        win.store.connect("library-changed", lambda *_: self.update_continue())
        self.rebuild()

    def update_continue(self):
        items = self.win.store.continue_watching()[:ROW_LIMIT]
        lib = self.win.store.library
        self.continue_row.set_metas([library_meta(i) for i in items], lambda m: library_progress(m, lib))

    def rebuild(self):
        clear(self.box)
        self.box.append(self.continue_row)
        self.update_continue()
        for addon in self.win.store.addons:
            for cat in addon.catalogs:
                if catalog_extras(cat)[1]:
                    continue  # needs a search query / genre – not suitable for the board
                row = CatalogRow(catalog_title(addon, cat), self.win.open_meta,
                                 on_see_all=lambda a=addon, c=cat: self.win.show_discover(a, c))
                self.box.append(row)
                run_async(addon.catalog, cat["type"], cat["id"],
                          on_done=lambda metas, r=row: r.set_metas(metas[:ROW_LIMIT]),
                          on_error=lambda e, r=row: r.set_error(f"Couldn't load: {e}"))
        if not self.box.get_first_child().get_next_sibling():
            self.box.append(spinner())


class DiscoverPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.entries = []  # (addon, catalog)
        self._updating = False
        self._generation = 0
        self.loading = False
        self.has_more = False
        self.skip = 0

        bar = Gtk.Box(spacing=8, margin_top=12, margin_start=18, margin_end=18, halign=Gtk.Align.START)
        self.type_dd = Gtk.DropDown.new_from_strings([])
        self.cat_dd = Gtk.DropDown.new_from_strings([])
        self.genre_dd = Gtk.DropDown.new_from_strings([])
        for dd in (self.type_dd, self.cat_dd, self.genre_dd):
            bar.append(dd)
        self.type_dd.connect("notify::selected", lambda *_: self._on_type())
        self.cat_dd.connect("notify::selected", lambda *_: self._on_catalog())
        self.genre_dd.connect("notify::selected", lambda *_: self._updating or self.load())
        self.append(bar)

        self.grid = PosterGrid(win.open_meta)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.set_child(self.grid)
        self.scroller.connect("edge-reached", self._on_edge)
        self.status = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_bottom=12)
        self.append(self.scroller)
        self.append(self.status)
        win.store.connect("addons-changed", lambda *_: self.rebuild())
        self.rebuild()

    # ---- filters -----------------------------------------------------------

    def rebuild(self):
        self.entries = []
        for addon in self.win.store.addons:
            for cat in addon.catalogs:
                if catalog_extras(cat)[1] - {"genre", "skip"}:
                    continue
                self.entries.append((addon, cat))
        self.types = list(dict.fromkeys(c["type"] for _, c in self.entries))
        self._updating = True
        self.type_dd.set_model(Gtk.StringList.new([t.title() for t in self.types]))
        self._updating = False
        self._on_type()

    def _type_entries(self):
        i = self.type_dd.get_selected()
        if i >= len(self.types):
            return []
        return [(a, c) for a, c in self.entries if c["type"] == self.types[i]]

    def _on_type(self):
        if self._updating:
            return
        self._updating = True
        self.cat_entries = self._type_entries()
        self.cat_dd.set_model(Gtk.StringList.new(
            [f"{c.get('name') or c['id']} ({a.name})" for a, c in self.cat_entries]))
        self._updating = False
        self._on_catalog()

    def _on_catalog(self):
        if self._updating:
            return
        entry = self.current()
        self._updating = True
        genres = []
        if entry:
            genres = list(catalog_genres(entry[1]))
            if "genre" not in catalog_extras(entry[1])[1]:
                genres.insert(0, "All genres")
        self.genre_dd.set_model(Gtk.StringList.new(genres))
        self.genre_dd.set_visible(bool(genres))
        self._updating = False
        self.load()

    def current(self):
        i = self.cat_dd.get_selected()
        entries = getattr(self, "cat_entries", [])
        return entries[i] if i < len(entries) else None

    def select(self, addon, cat):
        if cat["type"] not in self.types:
            return
        self.type_dd.set_selected(self.types.index(cat["type"]))
        for i, (a, c) in enumerate(self.cat_entries):
            if a is addon and c["id"] == cat["id"]:
                self.cat_dd.set_selected(i)

    # ---- loading -----------------------------------------------------------

    def _extra(self):
        model, i = self.genre_dd.get_model(), self.genre_dd.get_selected()
        genre = model.get_string(i) if model and i < model.get_n_items() else None
        return {} if not genre or genre == "All genres" else {"genre": genre}

    def load(self):
        self._generation += 1
        self.grid.reset()
        self.skip = 0
        self.has_more = True
        self.loading = False
        self._fetch()

    def _fetch(self):
        entry = self.current()
        if not entry or self.loading or not self.has_more:
            return
        addon, cat = entry
        gen, extra = self._generation, self._extra()
        if self.skip:
            extra["skip"] = self.skip
        self.loading = True
        clear(self.status)
        self.status.append(spinner(24))

        def done(metas):
            if gen != self._generation:
                return
            self.loading = False
            clear(self.status)
            self.grid.add_metas(metas)
            self.skip += len(metas)
            self.has_more = bool(metas) and "skip" in catalog_extras(cat)[0]
            if not self.skip:
                self.status.append(Gtk.Label(label="Nothing here", css_classes=["dim-label"]))

        def failed(e):
            if gen != self._generation:
                return
            self.loading = False
            clear(self.status)
            self.status.append(Gtk.Label(label=f"Couldn't load catalog: {e}", css_classes=["error"]))

        run_async(lambda: addon.catalog(cat["type"], cat["id"], **extra), on_done=done, on_error=failed)

    def _on_edge(self, _sw, pos):
        if pos == Gtk.PositionType.BOTTOM:
            self._fetch()


class LibraryPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        bar = Gtk.Box(spacing=8, margin_top=12, margin_start=18, margin_end=18)
        self.filter_dd = Gtk.DropDown.new_from_strings(["All", "Movies", "Series"])
        self.filter_dd.connect("notify::selected", lambda *_: self.refresh())
        bar.append(self.filter_dd)
        self.sync_label = Gtk.Label(hexpand=True, xalign=1, css_classes=["dim-label", "caption"])
        bar.append(self.sync_label)
        self.append(bar)

        self.stack = Gtk.Stack(vexpand=True)
        self.grid = PosterGrid(win.open_meta)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.grid)
        self.stack.add_named(sw, "grid")
        self.empty = Adw.StatusPage(icon_name="view-grid-symbolic", title="Your library is empty",
                                    description="Add movies and series from their details page.")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)
        win.store.connect("library-changed", lambda *_: self.refresh())
        win.store.connect("account-changed", lambda *_: self.refresh())
        self.refresh()

    def refresh(self):
        store = self.win.store
        self.sync_label.set_label(f"Synced with {store.user.get('email', 'your account')}"
                                  if store.logged_in else "Local library – sign in to sync")
        want = [None, "movie", "series"][self.filter_dd.get_selected()]
        items = [i for i in store.library_items() if want is None or i.get("type") == want]
        self.grid.reset()
        self.grid.add_metas([library_meta(i) for i in items], lambda m: library_progress(m, store.library))
        self.stack.set_visible_child_name("grid" if items else "empty")


class SearchPage(Adw.NavigationPage):
    def __init__(self, win, query):
        super().__init__(title=f"Search: {query}", tag="search")
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                      margin_top=18, margin_bottom=24, margin_start=24, margin_end=24)
        tv.set_content(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=box))
        self.set_child(tv)

        searchable = [(a, c) for a in win.store.addons for c in a.catalogs if "search" in catalog_extras(c)[0]]
        if not searchable:
            box.append(Adw.StatusPage(icon_name="system-search-symbolic", title="No searchable catalogs",
                                      description="Install an addon that supports search."))
        self.pending = len(searchable)
        self.found = 0
        self.none = Adw.StatusPage(icon_name="system-search-symbolic", title="No results",
                                   description=f"Nothing matched “{query}”.", visible=False)
        box.append(self.none)
        for addon, cat in searchable:
            row = CatalogRow(f"{catalog_title(addon, cat)} — {addon.name}", win.open_meta)
            box.append(row)
            run_async(lambda a=addon, c=cat: a.catalog(c["type"], c["id"], search=query),
                      on_done=lambda m, r=row: self._result(r, m),
                      on_error=lambda e, r=row: self._result(r, []))

    def _result(self, row, metas):
        row.set_metas(metas)
        self.pending -= 1
        self.found += len(metas)
        if not self.pending and not self.found:
            self.none.set_visible(True)
