"""Reusable widgets: async image loading, poster cards, catalog rows and grids."""

import hashlib
import os
from collections import OrderedDict

from gi.repository import Gdk, GLib, Gtk, Pango

from ..core.util import cache_dir, run_async, session

POSTER_W, POSTER_H = 150, 225


class ImageCache:
    """Downloads images on worker threads, caches them on disk and in memory."""

    def __init__(self, capacity=400):
        self.dir = cache_dir("images")
        self.mem = OrderedDict()
        self.capacity = capacity
        self.waiting = {}

    def load(self, url, callback):
        if not url:
            return
        tex = self.mem.get(url)
        if tex is not None:
            self.mem.move_to_end(url)
            callback(tex)
            return
        if url in self.waiting:
            self.waiting[url].append(callback)
            return
        self.waiting[url] = [callback]
        run_async(self._fetch, url, on_done=lambda t: self._deliver(url, t),
                  on_error=lambda e: self._deliver(url, None))

    def _fetch(self, url):
        path = os.path.join(self.dir, hashlib.sha1(url.encode()).hexdigest())
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
        else:
            r = session().get(url, timeout=20)
            r.raise_for_status()
            data = r.content
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))

    def _deliver(self, url, tex):
        if tex is not None:
            self.mem[url] = tex
            while len(self.mem) > self.capacity:
                self.mem.popitem(last=False)
        for cb in self.waiting.pop(url, []):
            if tex is not None:
                cb(tex)


images = ImageCache()


def set_picture(picture, url):
    picture._wanted_url = url

    def apply(tex):
        if picture._wanted_url == url:
            picture.set_paintable(tex)

    images.load(url, apply)


def clear(box):
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def spinner(size=32):
    s = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    s.set_size_request(size, size)
    return s


class PosterCard(Gtk.Button):
    def __init__(self, meta, on_activate, progress=None):
        super().__init__(css_classes=["flat", "poster-card"], tooltip_text=meta.get("name"))
        self.meta = meta
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        frame = Gtk.Overlay(css_classes=["poster-frame"], overflow=Gtk.Overflow.HIDDEN)
        frame.set_size_request(POSTER_W, POSTER_H)
        placeholder = Gtk.Label(label=meta.get("name", ""), wrap=True, justify=Gtk.Justification.CENTER,
                                css_classes=["dim-label", "caption"], max_width_chars=12)
        frame.set_child(placeholder)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        pic.set_size_request(POSTER_W, POSTER_H)
        frame.add_overlay(pic)
        if progress:
            bar = Gtk.ProgressBar(fraction=min(1.0, progress), valign=Gtk.Align.END, css_classes=["osd"])
            frame.add_overlay(bar)
        if meta.get("poster"):
            set_picture(pic, meta["poster"])
        box.append(frame)

        title = Gtk.Label(label=meta.get("name", ""), ellipsize=Pango.EllipsizeMode.END,
                          max_width_chars=1, xalign=0)
        title.set_size_request(POSTER_W, -1)
        box.append(title)
        sub = meta.get("releaseInfo") or meta.get("year")
        if sub:
            box.append(Gtk.Label(label=str(sub), xalign=0, css_classes=["dim-label", "caption"]))
        self.set_child(box)
        self.connect("clicked", lambda *_: on_activate(meta))


class CatalogRow(Gtk.Box):
    """A titled, horizontally scrolling strip of posters (the Stremio "board" row)."""

    def __init__(self, title, on_open, on_see_all=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["catalog-row"])
        self.on_open = on_open
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Label(label=title, xalign=0, hexpand=True, css_classes=["title-3"]))
        self.prev_btn = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat", "circular"])
        self.next_btn = Gtk.Button(icon_name="go-next-symbolic", css_classes=["flat", "circular"])
        self.prev_btn.connect("clicked", lambda *_: self._page(-1))
        self.next_btn.connect("clicked", lambda *_: self._page(1))
        head.append(self.prev_btn)
        head.append(self.next_btn)
        if on_see_all:
            see = Gtk.Button(label="See all", css_classes=["flat"])
            see.connect("clicked", lambda *_: on_see_all())
            head.append(see)
        self.append(head)

        self.scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                           hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        self.strip = Gtk.Box(spacing=12)
        self.strip.append(spinner())
        self.strip.set_size_request(-1, POSTER_H + 50)
        self.scroller.set_child(self.strip)
        # Vertical wheel scrolls the strip only while holding Shift; otherwise let the page scroll.
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._on_wheel)
        self.scroller.add_controller(wheel)
        self.append(self.scroller)

    def _on_wheel(self, ctl, dx, dy):
        if ctl.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK:
            adj = self.scroller.get_hadjustment()
            adj.set_value(adj.get_value() + dy * 60)
            return True
        return False

    def _page(self, direction):
        adj = self.scroller.get_hadjustment()
        adj.set_value(adj.get_value() + direction * adj.get_page_size() * 0.85)

    def set_metas(self, metas, progress_of=None):
        clear(self.strip)
        if not metas:
            self.set_visible(False)
            return
        self.set_visible(True)
        for m in metas:
            self.strip.append(PosterCard(m, self.on_open, progress_of(m) if progress_of else None))

    def set_error(self, msg):
        clear(self.strip)
        self.strip.append(Gtk.Label(label=msg, css_classes=["dim-label"], margin_start=6))


class PosterGrid(Gtk.FlowBox):
    def __init__(self, on_open):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                         max_children_per_line=40, min_children_per_line=2,
                         row_spacing=18, column_spacing=12, valign=Gtk.Align.START,
                         margin_top=12, margin_bottom=24, margin_start=18, margin_end=18)
        self.on_open = on_open

    def add_metas(self, metas, progress_of=None):
        for m in metas:
            card = PosterCard(m, self.on_open, progress_of(m) if progress_of else None)
            child = Gtk.FlowBoxChild(child=card, focusable=False)
            self.append(child)

    def reset(self):
        self.remove_all()
