"""Downloads view: what's on the drive or on its way, with play / pause / delete."""

import os
import re

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .widgets import clear, set_picture


def _size(n):
    if not n:
        return "?"
    return f"{n / (1 << 30):.2f} GB" if n >= 1 << 30 else f"{n / (1 << 20):.0f} MB"


class DownloadsPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.dm = win.downloads
        self.rows = {}

        bar = Gtk.Box(spacing=8, margin_top=12, margin_start=18, margin_end=18, margin_bottom=6)
        self.where = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE,
                               css_classes=["dim-label"])
        bar.append(self.where)
        change = Gtk.Button(label="Change folder…", css_classes=["flat"])
        change.connect("clicked", self._choose_folder)
        bar.append(change)
        self.append(bar)

        self.stack = Gtk.Stack(vexpand=True)
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                margin_start=18, margin_end=18, margin_bottom=18, valign=Gtk.Align.START)
        clamp = Adw.Clamp(maximum_size=900, child=self.list)
        self.stack.add_named(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        self.stack.add_named(Adw.StatusPage(icon_name="folder-download-symbolic", title="No downloads yet",
                                            description="Use the download button on a movie or show to "
                                                        "keep it for offline viewing."), "empty")
        self.append(self.stack)
        self.dm.connect("changed", self._on_changed)
        self.refresh()

    def refresh(self):
        clear(self.list)
        self.rows = {}
        jobs = self.dm.ordered()
        for j in jobs:
            row = self._build_row(j)
            self.rows[j["id"]] = row
            self.list.append(row)
        self.stack.set_visible_child_name("list" if jobs else "empty")
        self._update_header()

    def _update_header(self):
        free = self.dm.free_bytes()
        self.where.set_label(f"{self.dm.root}  ·  {_size(free)} free")

    def _on_changed(self, _dm, vid):
        if not vid or vid not in self.rows:
            self.refresh()
        elif vid in self.dm.jobs:
            self._update_row(self.rows[vid], self.dm.jobs[vid])

    # ---- rows --------------------------------------------------------------------

    def _build_row(self, j):
        row = Gtk.ListBoxRow(activatable=False)
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        thumb = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, css_classes=["episode-thumb"])
        thumb.set_size_request(64, 96)
        if j["meta"].get("poster"):
            set_picture(thumb, j["meta"]["poster"])
        box.append(thumb)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        title = j["meta"]["name"] + (f" — {j['label']}" if j["meta"].get("type") == "series" else "")
        text.append(Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"]))
        row.info = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label", "caption"])
        text.append(row.info)
        row.bar = Gtk.ProgressBar()
        text.append(row.bar)
        box.append(text)

        row.play = Gtk.Button(icon_name="media-playback-start-symbolic", css_classes=["circular", "suggested-action"],
                              valign=Gtk.Align.CENTER, tooltip_text="Play")
        row.play.connect("clicked", lambda *_: self._play(j["id"]))
        row.toggle = Gtk.Button(css_classes=["circular", "flat"], valign=Gtk.Align.CENTER)
        row.toggle.connect("clicked", lambda *_: self._toggle(j["id"]))
        delete = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["circular", "flat"],
                            valign=Gtk.Align.CENTER, tooltip_text="Delete")
        delete.connect("clicked", lambda *_: self._confirm_delete(j["id"]))
        for b in (row.play, row.toggle, delete):
            box.append(b)
        row.set_child(box)
        self._update_row(row, j)
        return row

    def _update_row(self, row, j):
        status, done, size = j["status"], j.get("done") or 0, j.get("size")
        frac = min(1.0, done / size) if size else 0.0
        if status == "done":
            quality = " · ".join(t for t in (j.get("quality") or "").split(" · ")
                                 if not re.fullmatch(r"[\d.]+ (GB|MB)", t))
            info = f"{_size(size)} · {quality}"
            if j.get("subtitle"):
                info += " · subtitles saved"
            frac = 1.0
        elif status == "downloading":
            speed = j.get("speed") or 0
            eta = (size - done) / speed if speed and size else None
            info = f"{_size(done)} of {_size(size)} · {speed / (1 << 20):.1f} MB/s"
            if eta:
                info += f" · {int(eta // 60)} min left"
        elif status == "failed":
            info = f"Failed: {j.get('error')}"
        else:
            info = f"{status.title()} · {_size(done)} of {_size(size)}"
        row.info.set_label(info)
        row.bar.set_fraction(frac)
        row.bar.set_visible(status != "done")
        row.play.set_visible(status == "done")
        row.toggle.set_visible(status != "done")
        paused = status in ("paused", "failed")
        row.toggle.set_icon_name("media-playback-start-symbolic" if paused else "media-playback-pause-symbolic")
        row.toggle.set_tooltip_text("Resume" if paused else "Pause")

    # ---- actions ---------------------------------------------------------------

    def _play(self, vid):
        j = self.dm.jobs.get(vid)
        stream = self.dm.local_stream(vid)
        if j and stream:
            self.win.play(j["meta"], j["video"], stream)

    def _toggle(self, vid):
        j = self.dm.jobs.get(vid)
        if j and j["status"] in ("paused", "failed"):
            self.dm.resume(vid)
        else:
            self.dm.pause(vid)

    def _confirm_delete(self, vid):
        j = self.dm.jobs.get(vid)
        if not j:
            return
        dlg = Adw.AlertDialog(heading="Delete this download?",
                              body=f"{j['meta']['name']} {j['label'] if j['meta'].get('type') == 'series' else ''}"
                                   f"\nThe file is removed from {self.dm.root}.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "delete" and self.dm.remove(vid))
        dlg.present(self.win)

    def _choose_folder(self, *_):
        dialog = Gtk.FileDialog(title="Choose where downloads go")
        if os.path.isdir(self.dm.root):
            dialog.set_initial_folder(Gio.File.new_for_path(self.dm.root))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            self.dm.set_root(folder.get_path())
            self.win.toast(f"Downloads now go to {folder.get_path()}")

        dialog.select_folder(self.win, None, done)
