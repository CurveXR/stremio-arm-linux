"""Addons view: installed addons, install by URL, and the official/community catalog."""

from gi.repository import Adw, GLib, Gtk

from ..core.addons import CINEMETA_URL, Addon
from ..core.util import run_async

RESOURCE_LABELS = {"catalog": "Catalogs", "meta": "Metadata", "stream": "Streams", "subtitles": "Subtitles",
                   "addon_catalog": "Addon catalog"}


def esc(text):
    return GLib.markup_escape_text(text or "")


def _resources(manifest):
    names = [r if isinstance(r, str) else r.get("name") for r in manifest.get("resources") or []]
    return ", ".join(RESOURCE_LABELS.get(n, n) for n in names if n)


class AddonsPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win

        install = Adw.PreferencesGroup(title="Install from URL",
                                       description="Paste an addon's manifest.json link (http(s):// or stremio://).")
        self.url_row = Adw.EntryRow(title="Addon URL", show_apply_button=True)
        self.url_row.connect("apply", self._install_url)
        install.add(self.url_row)
        self.add(install)

        self.installed = Adw.PreferencesGroup(title="Installed")
        self.add(self.installed)
        self.installed_rows = []

        self.catalog_groups = []
        for cat_id, title in (("official", "Official addons"), ("community", "Community addons")):
            group = Adw.PreferencesGroup(title=title)
            row = Adw.ActionRow(title="Loading…")
            group.add(row)
            group.placeholder = row
            self.add(group)
            self.catalog_groups.append((cat_id, group))
        self.catalog_rows = []

        win.store.connect("addons-changed", lambda *_: self.refresh())
        self.refresh()
        self._load_catalogs()

    def refresh(self):
        for row in self.installed_rows:
            self.installed.remove(row)
        self.installed_rows = []
        for addon in self.win.store.addons:
            row = Adw.ActionRow(title=esc(addon.name), subtitle=esc(addon.manifest.get("description", "")),
                                subtitle_lines=2, tooltip_text=addon.transport_url)
            row.add_prefix(Gtk.Label(label=f"v{addon.manifest.get('version', '?')}",
                                     css_classes=["dim-label", "caption"], width_chars=7))
            row.add_suffix(Gtk.Label(label=_resources(addon.manifest), css_classes=["dim-label", "caption"]))
            if addon.protected:
                row.add_suffix(Gtk.Image(icon_name="security-high-symbolic", tooltip_text="Protected"))
            else:
                rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                css_classes=["flat", "destructive-action"], tooltip_text="Uninstall")
                rm.connect("clicked", lambda _b, a=addon: self._confirm_remove(a))
                row.add_suffix(rm)
            self.installed.add(row)
            self.installed_rows.append(row)
        self._update_catalog_buttons()

    def _confirm_remove(self, addon):
        dlg = Adw.AlertDialog(heading=f"Uninstall {addon.name}?",
                              body="You can reinstall it later from its URL or the catalog below.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Uninstall")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "remove" and self.win.store.remove_addon(addon))
        dlg.present(self.win)

    def _install_url(self, row):
        url = row.get_text().strip()
        if not url:
            return
        row.set_sensitive(False)

        def done(addon):
            row.set_sensitive(True)
            row.set_text("")
            self.win.store.install_addon(addon)
            self.win.toast(f"Installed {addon.name}")

        def failed(e):
            row.set_sensitive(True)
            self.win.toast(f"Couldn't install addon: {e}")

        run_async(Addon.from_url, url, on_done=done, on_error=failed)

    def _load_catalogs(self):
        cinemeta = self.win.store.addon_by_url(CINEMETA_URL)
        source = cinemeta or Addon({"transportUrl": CINEMETA_URL, "manifest": {}})
        for cat_id, group in self.catalog_groups:
            run_async(source.addon_catalog, "all", cat_id,
                      on_done=lambda items, g=group: self._fill_catalog(g, items),
                      on_error=lambda e, g=group: g.placeholder.set_title(f"Couldn't load: {e}"))

    def _fill_catalog(self, group, items):
        group.remove(group.placeholder)
        for item in items:
            m = item.get("manifest") or {}
            url = item.get("transportUrl")
            if not url or url.startswith("http://127.0.0.1"):
                continue
            row = Adw.ActionRow(title=esc(m.get("name", url)), subtitle=esc(m.get("description", "")),
                                subtitle_lines=2)
            row.add_suffix(Gtk.Label(label=_resources(m), css_classes=["dim-label", "caption"]))
            btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
            btn.connect("clicked", lambda _b, d=item: self._install_descriptor(d))
            row.add_suffix(btn)
            row.url, row.btn = url, btn
            group.add(row)
            self.catalog_rows.append(row)
        self._update_catalog_buttons()

    def _install_descriptor(self, item):
        descriptor = {"transportUrl": item["transportUrl"], "transportName": "http",
                      "manifest": item.get("manifest") or {}, "flags": {}}
        self.win.store.install_addon(Addon(descriptor))
        self.win.toast(f"Installed {descriptor['manifest'].get('name', 'addon')}")

    def _update_catalog_buttons(self):
        for row in self.catalog_rows:
            installed = self.win.store.addon_by_url(row.url) is not None
            row.btn.set_label("Installed" if installed else "Install")
            row.btn.set_sensitive(not installed)
            if installed:
                row.btn.remove_css_class("suggested-action")
            else:
                row.btn.add_css_class("suggested-action")
