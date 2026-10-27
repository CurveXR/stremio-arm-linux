# Stremio for ARM Linux (Stremio Native)

A native GTK4 / libadwaita client for the Stremio addon ecosystem, written in Python. It needs no
Electron, Qt WebEngine or Node.js, so it runs on ARM64 boards such as Jetson / L4T, where the
official desktop app has no builds. It also runs on x86-64.

> Unofficial and not affiliated with Stremio. It speaks the public Stremio addon protocol and
> account API. Which addons you install, and what you stream with them, is up to you.

## Features

- **Board**: rows from every catalog of your installed addons, plus *Continue Watching*.
- **Discover**: grid with type, catalog and genre filters, and infinite scroll.
- **Search** across every addon catalog that supports it (Ctrl+F, or just start typing).
- **Details**: backdrop, logo, cast, trailer, seasons and episodes, and streams gathered
  from all stream addons.
- **Library**: kept locally, or synced with your Stremio account.
- **Addons**: install by URL (`https://…/manifest.json` or `stremio://…`), uninstall, or
  pick from the official and community catalogs.
- **Stremio account**: sign in to sync your addon collection and library both ways,
  including watch progress.
- **One-click Play**: ranks every stream for this device and plays the best one. It prefers
  cached debrid results (`[RD+]`), 1080p H.264 or 8-bit HEVC over 4K/HDR/Dolby Vision (this
  machine decodes in software), healthy seeders, sensible sizes and your language, and it
  skips CAM releases, season packs and streams needing missing codecs. For series it resumes
  or moves to the next episode. Each stream list also gets a "Best for this device" row.
- **Auto-next episode**: about 2½ minutes before an episode ends, the app looks up the next one,
  preferring the same release (Torrentio's `bingeGroup`). An 8-second countdown then plays it
  without leaving fullscreen. Press N to skip ahead now; switch it off in Preferences.
- **Stream list**: one list for all addons, best first. Colour-coded badges show cached/not
  cached, resolution, HDR, codec, size, seeders, language, season pack and missing codecs,
  followed by the release name and source. Filter chips (Instant, Up to 1080p, My language) are
  remembered between sessions.
- **Learns your taste**: when you pick a stream other than the suggested one, or watch one to
  the end, its resolution, codec, release group and source count towards future rankings.
- **Automatic fallback**: if a stream errors or shows no picture within 45 s (120 s for
  torrents), the player switches to the next-best stream for the same episode.
- **Subtitles matched to the file**: the app sends the OpenSubtitles file hash, size and
  filename, then prefers subtitles timed for the same release (marked "✓ matches this file").
- **Skip intro**: episodes of one release share an identical title sequence. The app decodes the
  first 6 minutes of audio of this episode and a neighbouring one (in the background, about a
  minute), finds the stretch they share, and shows a **Skip intro** button there (or press S).
  You can also skip automatically (Preferences). Results are cached per episode and release.
- **Offline downloads** to `/media/shared/Stremio` (change it on the Downloads tab):
  - **What:** one episode, the next 3, a whole season, or a movie. A batch stays on one release,
    and right-clicking a stream downloads exactly that one.
  - **How:** downloads resume after a pause or a quit, and expired Real-Debrid links are fetched
    again automatically.
  - **Extras:** the best-matching subtitles are saved next to each video. Play prefers the copy on
    your drive, and so does auto-next.
  - **Layout:** files go under `TV/<Show>/Season NN/` and `Movies/<Title (Year)>/`, with names that
    are safe on exFAT.
- **Built-in player** (GTK media / GStreamer): resumes where you left off, shows subtitles
  from subtitle addons (OpenSubtitles v3 is installed by default) and torrent peer and
  speed stats. Keys: Space, ←/→ (Shift = 60 s), ↑/↓, M, F, Esc.
- **Built-in torrent engine** (libtorrent): streams `infoHash` results by downloading the
  pieces just ahead of the playback position, with no Stremio server needed. It can also
  use an official Stremio streaming server at `http://127.0.0.1:11470`.
- **External players**: mpv, VLC or Celluloid if installed (Preferences, or ⋮ in the player).

## Run

```bash
git clone https://github.com/CurveXR/stremio-arm-linux.git
cd stremio-arm-linux
scripts/setup.sh        # bundles libtorrent (no root), adds a menu entry, checks codecs
./stremio-native
```

Requires Python 3.10+, GTK 4.10+, libadwaita 1.5+, GStreamer 1.22+, `python3-requests` and
`python3-numpy` (all stock on Ubuntu 24.04). `scripts/setup.sh` puts the libtorrent wheel in
`vendor/`. If the system package `python3-libtorrent` is installed, the app uses that instead.

Install a desktop launcher (no root):

```bash
./install-desktop.sh
```

## Remote control (voice assistants, scripts)

The app exposes two D-Bus interfaces:

- **MPRIS** (`org.mpris.MediaPlayer2.stremio_native`): play/pause, next and previous episode,
  seeking, volume and metadata. Media keys, `playerctl` and voice assistants' media commands work
  without any extra setup.
- **`io.github.stremio_native.Control`**, wrapped by a command-line tool. Commands start the app
  if it isn't running, and print one line meant to be spoken. The exit code is 1 when the
  request couldn't be done.

```bash
./stremio-native ctl play "it's always sunny"        # resume, or the next episode, with the best stream
./stremio-native ctl play "always sunny season 18 episode 3"
./stremio-native ctl pause | resume | toggle | stop | next | previous | skip-intro | forward | rewind
./stremio-native ctl fullscreen | exit-fullscreen | mute | unmute | volume-up | volume-down | subtitles-off
./stremio-native ctl seek -30
./stremio-native ctl download "the bear" season      # episode | next3 | season
./stremio-native ctl search "dune"                    # JSON
./stremio-native ctl status                           # JSON: what's playing, position, intro, downloads
```

## Codecs

The built-in player decodes whatever GStreamer can. The stock install has H.264 (openh264),
HEVC (libde265), AV1 (aom) and AAC. For the best coverage (AC3/EAC3/DTS audio, MPEG-4 Part 2,
VC-1, …):

```bash
sudo apt install gstreamer1.0-libav gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
```

## Files

- Profile (settings, addons, library, auth key): `~/.config/stremio-native/profile.json`
- Image cache: `~/.cache/stremio-native/images`
- Torrent data: `~/.cache/stremio-native/torrents`, deleted when you leave the player or quit.

## Layout

```
stremio_native/
  core/      addon protocol, account API, profile store, torrent engine, subtitles
  ui/        window, board/discover/library/search, details, player, addons, preferences
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
