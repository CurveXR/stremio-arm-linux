"""Built-in torrent streaming engine (optional – needs the libtorrent Python bindings).

Torrents are fetched on demand: an HTTP server on 127.0.0.1 serves the selected file with
Range support, and each read sets piece deadlines just ahead of the playback position so
libtorrent downloads what the player needs next.
"""

import mimetypes
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .util import cache_dir

try:
    import libtorrent as lt
except ImportError:  # pragma: no cover - depends on the system
    lt = None

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v", ".ts", ".wmv", ".flv", ".mpg", ".mpeg")
EXTRA_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
]
READAHEAD_BYTES = 24 * 1024 * 1024
METADATA_TIMEOUT = 90


def available():
    return lt is not None


class _Torrent:
    def __init__(self, handle):
        self.handle = handle
        self.file_idx = None
        self.last_access = time.monotonic()

    def info(self):
        return self.handle.torrent_file() if self.handle.status().has_metadata else None


class TorrentEngine:
    def __init__(self):
        if lt is None:
            raise RuntimeError("libtorrent is not installed")
        self.save_path = cache_dir("torrents")
        self.session = lt.session({
            "listen_interfaces": "0.0.0.0:42069,[::]:42069",
            "enable_dht": True,
            "enable_lsd": True,
            "alert_mask": 0,
            "user_agent": "StremioNative/0.1 libtorrent/" + lt.__version__,
            "connections_limit": 200,
        })
        self.torrents = {}
        self.lock = threading.Lock()
        handler = type("Handler", (_StreamHandler,), {"engine": self})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="torrent-http").start()

    # ---- public API --------------------------------------------------------

    def add(self, info_hash, sources=()):
        info_hash = info_hash.lower()
        with self.lock:
            t = self.torrents.get(info_hash)
            if t:
                return t
            trackers = [s[len("tracker:"):] for s in sources or () if s.startswith("tracker:")]
            uri = f"magnet:?xt=urn:btih:{info_hash}" + "".join(
                f"&tr={tr}" for tr in dict.fromkeys(trackers + EXTRA_TRACKERS))
            atp = lt.parse_magnet_uri(uri)
            atp.save_path = self.save_path
            t = self.torrents[info_hash] = _Torrent(self.session.add_torrent(atp))
            return t

    def stream_url(self, info_hash, file_idx=None, sources=()):
        self.add(info_hash, sources)
        idx = -1 if file_idx is None else int(file_idx)
        return f"http://127.0.0.1:{self.port}/{info_hash.lower()}/{idx}"

    def stats(self, info_hash):
        t = self.torrents.get(info_hash.lower())
        if not t:
            return None
        s = t.handle.status()
        return {
            "peers": s.num_peers,
            "seeds": s.num_seeds,
            "down": s.download_rate,
            "up": s.upload_rate,
            "has_metadata": s.has_metadata,
            "name": s.name,
        }

    def remove(self, info_hash, delete_files=True):
        with self.lock:
            t = self.torrents.pop(info_hash.lower(), None)
        if t:
            flags = lt.options_t.delete_files if delete_files else 0
            self.session.remove_torrent(t.handle, flags)

    def shutdown(self):
        for ih in list(self.torrents):
            self.remove(ih)
        self.httpd.shutdown()
        shutil.rmtree(self.save_path, ignore_errors=True)

    # ---- helpers used by the HTTP handler ----------------------------------

    def wait_metadata(self, t):
        deadline = time.monotonic() + METADATA_TIMEOUT
        while time.monotonic() < deadline:
            ti = t.info()
            if ti:
                return ti
            time.sleep(0.2)
        raise TimeoutError("No peers sent torrent metadata")

    def select_file(self, t, ti, idx):
        fs = ti.files()
        if idx < 0 or idx >= fs.num_files():
            candidates = [i for i in range(fs.num_files()) if fs.file_path(i).lower().endswith(VIDEO_EXT)]
            idx = max(candidates or range(fs.num_files()), key=fs.file_size)
        if t.file_idx != idx:
            t.file_idx = idx
            prio = [0] * fs.num_files()
            prio[idx] = 4
            t.handle.prioritize_files(prio)
            # Containers keep their index at either end – fetch both first.
            first, last = self._piece_range(ti, idx, 0, fs.file_size(idx) - 1)
            for p in (first, first + 1, last - 1, last):
                if first <= p <= last:
                    t.handle.set_piece_deadline(p, 0)
        return idx

    @staticmethod
    def _piece_range(ti, idx, start, end):
        off, plen = ti.files().file_offset(idx), ti.piece_length()
        return (off + start) // plen, (off + end) // plen

    def ensure(self, t, ti, idx, pos, end):
        """Block until the piece holding byte `pos` is on disk; request readahead too."""
        fs, plen = ti.files(), ti.piece_length()
        piece = (fs.file_offset(idx) + pos) // plen
        stop = min((fs.file_offset(idx) + min(end, pos + READAHEAD_BYTES)) // plen, ti.num_pieces() - 1)
        h = t.handle
        try:
            for i, p in enumerate(range(piece, stop + 1)):
                if not h.have_piece(p):
                    h.set_piece_deadline(p, 200 + i * 150)
            while not h.have_piece(piece):
                if t not in self.torrents.values():
                    raise ConnectionAbortedError("torrent removed")
                time.sleep(0.05)
        except RuntimeError as e:  # handle invalidated by remove() mid-read
            raise ConnectionAbortedError(str(e)) from e
        t.last_access = time.monotonic()
        return (piece + 1) * plen - fs.file_offset(idx)  # first byte of the next piece


class _StreamHandler(BaseHTTPRequestHandler):
    engine: TorrentEngine
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head):
        m = re.fullmatch(r"/([0-9a-f]{40})/(-?\d+)", self.path.split("?")[0])
        t = m and self.engine.torrents.get(m.group(1))
        if not t:
            self.send_error(404)
            return
        try:
            ti = self.engine.wait_metadata(t)
        except TimeoutError as e:
            self.send_error(504, str(e))
            return
        idx = self.engine.select_file(t, ti, int(m.group(2)))
        fs = ti.files()
        size = fs.file_size(idx)
        path = os.path.join(self.engine.save_path, fs.file_path(idx))

        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else end
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.lower().endswith(".mkv"):
            ctype = "video/x-matroska"
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if head:
            return

        pos = start
        try:
            while pos <= end:
                avail_until = self.engine.ensure(t, ti, idx, pos, end)
                chunk_end = min(end, avail_until - 1)
                with open(path, "rb") as f:
                    f.seek(pos)
                    while pos <= chunk_end:
                        data = f.read(min(256 * 1024, chunk_end - pos + 1))
                        if not data:
                            raise IOError("short read")
                        self.wfile.write(data)
                        pos += len(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
