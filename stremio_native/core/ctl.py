"""`stremio-native ctl …`: drive a running Stremio Native over D-Bus (starting it if needed).

    stremio-native ctl play "it's always sunny"          resume / next episode / movie, best stream
    stremio-native ctl play "always sunny s18e3"          a given episode ("season 18 episode 3" works too)
    stremio-native ctl pause | resume | toggle | stop | next | previous | skip-intro | forward | rewind
                       | fullscreen | exit-fullscreen | mute | unmute | volume-up | volume-down
                       | subtitles-off | show | quit
    stremio-native ctl seek -30                           relative seconds
    stremio-native ctl download "the bear" [episode|next3|season]
    stremio-native ctl search "dune"                      JSON
    stremio-native ctl status                             JSON (what's playing, position, downloads)

Prints the app's one-line reply (meant to be spoken) and exits 0, or 1 when it couldn't do it.
"""

import shutil
import subprocess
import sys
import time

from gi.repository import Gio, GLib

from .util import APP_ID

PATH = "/io/github/stremio_native/Control"
IFACE = "io.github.stremio_native.Control"
COMMANDS = {"pause", "resume", "play-pause", "toggle", "stop", "next", "previous", "skip-intro", "forward",
            "rewind", "fullscreen", "exit-fullscreen", "mute", "unmute", "volume-up", "volume-down",
            "subtitles-off", "show", "raise", "quit", "back", "home"}
FAILURES = ("Sorry", "Nothing", "Unknown", "No intro", "I couldn't", "I found")


def _running(bus):
    reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                          GLib.Variant("(s)", (APP_ID,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 2000)
    return reply.unpack()[0]


def _ensure_running(bus, launcher, timeout=25):
    if _running(bus):
        return True
    cmd = [launcher]
    if shutil.which("systemd-run"):
        # A scope of its own, so the app outlives the caller (e.g. a voice assistant's service restarting).
        cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect", launcher]
    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if _running(bus):
            try:  # the bus name comes first; wait until the control object answers
                _call(bus, "Status", (), "()")
                return True
            except GLib.Error:
                continue
    return False


def _call(bus, method, args, sig):
    variant = GLib.Variant(sig, args)
    reply = bus.call_sync(APP_ID, PATH, IFACE, method, variant, GLib.VariantType("(s)"),
                          Gio.DBusCallFlags.NONE, 120_000)
    return reply.unpack()[0]


def main(argv, launcher):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0].lower(), argv[1:]
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    if cmd == "status" and not _running(bus):
        print('{"state": "not running"}')
        return 0
    if not _ensure_running(bus, launcher):
        print("Sorry: Stremio didn't start")
        return 1
    try:
        if cmd == "play" and rest:
            out = _call(bus, "Play", (" ".join(rest),), "(s)")
        elif cmd == "episode" and len(rest) >= 3:
            out = _call(bus, "PlayEpisode", (" ".join(rest[:-2]), int(rest[-2]), int(rest[-1])), "(sii)")
        elif cmd == "seek" and rest:
            out = _call(bus, "Seek", (int(float(rest[0])),), "(i)")
        elif cmd == "download" and rest:
            what = rest[-1] if rest[-1] in ("episode", "next3", "season", "movie") else "episode"
            query = rest[:-1] if rest[-1] == what and len(rest) > 1 else rest
            out = _call(bus, "Download", (" ".join(query), what), "(ss)")
        elif cmd == "search" and rest:
            out = _call(bus, "Search", (" ".join(rest),), "(s)")
        elif cmd == "status":
            out = _call(bus, "Status", (), "()")
        elif cmd in COMMANDS or cmd == "play":
            out = _call(bus, "Command", ({"play-pause": "toggle", "play": "play"}.get(cmd, cmd),), "(s)")
        else:
            print(f"Unknown command: {cmd}\n\n{__doc__.strip()}")
            return 2
    except GLib.Error as e:
        print(f"Sorry: {e.message}")
        return 1
    print(out)
    return 1 if out.startswith(FAILURES) else 0
