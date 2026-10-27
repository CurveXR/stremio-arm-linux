"""Application entry point."""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, Gdk, Gio, Gst, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .core import streams  # noqa: E402
from .core.store import Store  # noqa: E402
from .core.util import APP_ID  # noqa: E402

SHORTCUTS = """
<interface>
  <object class="GtkShortcutsWindow" id="shortcuts">
    <property name="modal">1</property>
    <child><object class="GtkShortcutsSection"><property name="section-name">main</property>
      <child><object class="GtkShortcutsGroup"><property name="title">General</property>
        <child><object class="GtkShortcutsShortcut"><property name="title">Search</property>
          <property name="accelerator">&lt;Ctrl&gt;f</property></object></child>
        <child><object class="GtkShortcutsShortcut"><property name="title">Preferences</property>
          <property name="accelerator">&lt;Ctrl&gt;comma</property></object></child>
        <child><object class="GtkShortcutsShortcut"><property name="title">Back</property>
          <property name="accelerator">&lt;Alt&gt;Left</property></object></child>
        <child><object class="GtkShortcutsShortcut"><property name="title">Quit</property>
          <property name="accelerator">&lt;Ctrl&gt;q</property></object></child>
      </object></child>
      <child><object class="GtkShortcutsGroup"><property name="title">Player</property>
        <child><object class="GtkShortcutsShortcut"><property name="title">Play / pause</property>
          <property name="accelerator">space</property></object></child>
        <child><object class="GtkShortcutsShortcut"><property name="title">Seek 10 s (Shift: 60 s)</property>
          <property name="accelerator">Left Right</property></object></child>
        <child><object class="GtkShortcutsShortcut"><property name="title">Volume</property>
          <property name="accelerator">Up Down</property></object></child>
        <child><object class="GtkShortcutsShortcut"><property name="title">Mute</property>
          <property name="accelerator">m</property></object></child>
        <child><object class="GtkShortcutsShortcut"><property name="title">Fullscreen</property>
          <property name="accelerator">f</property></object></child>
      </object></child>
    </object></child>
  </object>
</interface>
"""


class StremioApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.store = None
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gst.init(None)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(os.path.dirname(__file__), "ui", "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb, accels in (
            ("preferences", lambda *_: self.window.show_preferences(), ["<Ctrl>comma"]),
            ("shortcuts", lambda *_: self._shortcuts(), ["<Ctrl>question"]),
            ("about", lambda *_: self._about(), []),
            ("quit", lambda *_: self.quit(), ["<Ctrl>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("win.search", ["<Ctrl>f"])
        self.store = Store()

    def do_activate(self):
        if not self.window:
            from .ui.window import MainWindow
            self.window = MainWindow(self, self.store)
            self.store.bootstrap()
            from .ui.control import Control
            self.control = Control(self, self.window)
        self.window.present()

    def do_shutdown(self):
        streams.shutdown()
        Adw.Application.do_shutdown(self)

    def _about(self):
        Adw.AboutDialog(application_name="Stremio Native", application_icon="video-x-generic",
                        version=__version__, comments="A native GTK client for the Stremio addon ecosystem.",
                        license_type=Gtk.License.GPL_3_0).present(self.window)

    def _shortcuts(self):
        builder = Gtk.Builder.new_from_string(SHORTCUTS, -1)
        win = builder.get_object("shortcuts")
        win.set_transient_for(self.window)
        win.present()


def main():
    return StremioApp().run(sys.argv)
