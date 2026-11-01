#!/usr/bin/env python3
"""
Dynamic Synced Lyrics Overlay for VLC (GNOME / Wayland)
Fetches synchronized lyrics from LRCLIB and displays:
- Previous line (dim/small)
- Current line (bold/larger)
- Next line (dim/small)
"""

import sys
import os
import re
import threading
import urllib.parse
import requests
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk, Gio, GLib

CSS_STYLE = b"""
window {
    background-color: transparent;
}

.lyrics-card {
    background-color: rgba(18, 20, 26, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 16px;
    padding: 12px 28px;
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.45);
}

.track-header {
    font-size: 11px;
    color: rgba(180, 190, 210, 0.5);
    margin-bottom: 4px;
}

.prev-line {
    font-size: 13px;
    color: rgba(220, 225, 235, 0.4);
    font-weight: 400;
}

.curr-line {
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
    margin: 4px 0;
}

.next-line {
    font-size: 13px;
    color: rgba(220, 225, 235, 0.4);
    font-weight: 400;
}
"""


def clean_title(title):
    """Strip track numbers like '01 - ' and file extensions."""
    title = re.sub(r"^\d+\s*[-.]\s*", "", title)
    title = re.sub(r"\.(flac|mp3|m4a|ogg|opus|wav)$", "", title, flags=re.I)
    return title.strip()


def parse_lrc(lrc_text):
    """Parses LRC string into sorted list of (timestamp_in_seconds, text)."""
    pattern = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    parsed = []
    for raw_line in lrc_text.splitlines():
        matches = list(pattern.finditer(raw_line))
        if not matches:
            continue
        text = pattern.sub("", raw_line).strip()
        for m in matches:
            minutes = int(m.group(1))
            seconds = float(m.group(2))
            t = minutes * 60 + seconds
            parsed.append((t, text))
    parsed.sort(key=lambda x: x[0])
    return parsed


class LyricsOverlay(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("VLC Lyrics")
        self.set_default_size(580, 120)
        self.set_decorated(False)

        # Style provider
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(CSS_STYLE)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Window handle enables dragging the window anywhere on Wayland/GNOME
        self.handle = Gtk.WindowHandle()
        self.set_child(self.handle)

        # Main box
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.card.add_css_class("lyrics-card")
        self.handle.set_child(self.card)

        # Track metadata label
        self.header_label = Gtk.Label(label="Waiting for VLC...")
        self.header_label.add_css_class("track-header")
        self.header_label.set_ellipsize(3)
        self.header_label.set_halign(Gtk.Align.CENTER)
        self.card.append(self.header_label)

        # Previous line
        self.prev_label = Gtk.Label(label="")
        self.prev_label.add_css_class("prev-line")
        self.prev_label.set_ellipsize(3)
        self.prev_label.set_halign(Gtk.Align.CENTER)
        self.card.append(self.prev_label)

        # Current line (larger, highlighted)
        self.curr_label = Gtk.Label(label="VLC Lyrics Overlay")
        self.curr_label.add_css_class("curr-line")
        self.curr_label.set_ellipsize(3)
        self.curr_label.set_halign(Gtk.Align.CENTER)
        self.card.append(self.curr_label)

        # Next line
        self.next_label = Gtk.Label(label="Start playing a song in VLC")
        self.next_label.add_css_class("next-line")
        self.next_label.set_ellipsize(3)
        self.next_label.set_halign(Gtk.Align.CENTER)
        self.card.append(self.next_label)

        # Key shortcuts: Esc / q to quit
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self.on_key_pressed)
        self.add_controller(key_controller)

        # State
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.current_track_id = None
        self.lyrics = []
        self.current_index = -2
        self.active_player_name = None
        self.lyrics_cache = {}

        # Tick timer: check position & update lyrics every 100ms
        GLib.timeout_add(100, self.tick)

    def on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.get_application().quit()
            return True
        return False

    def find_mpris_player(self):
        """Finds running VLC or any MPRIS player."""
        try:
            res = self.bus.call_sync(
                "org.freedesktop.DBus",
                "/",
                "org.freedesktop.DBus",
                "ListNames",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            names = [n for n in res.unpack()[0] if "org.mpris.MediaPlayer2" in n]
            if not names:
                return None
            vlc_players = [n for n in names if "vlc" in n.lower()]
            return vlc_players[0] if vlc_players else names[0]
        except Exception:
            return None

    def get_player_proxy(self, name):
        try:
            return Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                name,
                "/org/mpris/MediaPlayer2",
                "org.mpris.MediaPlayer2.Player",
                None,
            )
        except Exception:
            return None

    def tick(self):
        player_name = self.find_mpris_player()
        if not player_name:
            if self.active_player_name is not None:
                self.header_label.set_text("Waiting for VLC...")
                self.prev_label.set_text("")
                self.curr_label.set_text("No active media player")
                self.next_label.set_text("")
                self.active_player_name = None
                self.current_track_id = None
                self.lyrics = []
            return True

        self.active_player_name = player_name
        proxy = self.get_player_proxy(player_name)
        if not proxy:
            return True

        # Read metadata
        meta_prop = proxy.get_cached_property("Metadata")
        if not meta_prop:
            return True

        metadata = meta_prop.unpack()
        track_id = metadata.get("mpris:trackid") or metadata.get("xesam:url", "")
        raw_title = metadata.get("xesam:title", "")
        title = clean_title(raw_title)
        artist_raw = metadata.get("xesam:artist", [])
        artist = artist_raw[0] if isinstance(artist_raw, list) and artist_raw else str(artist_raw)
        album = metadata.get("xesam:album", "")
        duration = metadata.get("mpris:length", 0) // 1_000_000
        file_url = metadata.get("xesam:url", "")

        # Detect track change
        if track_id != self.current_track_id:
            self.current_track_id = track_id
            self.current_index = -2
            self.lyrics = []
            header_text = f"{artist} - {title}" if artist and title else (title or "Playing...")
            self.header_label.set_text(header_text)
            self.prev_label.set_text("")
            self.curr_label.set_text("Fetching lyrics...")
            self.next_label.set_text("")

            # Check memory cache first
            cache_key = (artist.lower(), title.lower())
            if cache_key in self.lyrics_cache:
                self.on_lyrics_loaded(track_id, self.lyrics_cache[cache_key])
            elif title:
                threading.Thread(
                    target=self.fetch_lyrics,
                    args=(title, artist, album, duration, file_url, track_id),
                    daemon=True,
                ).start()

        # Update position and sync line
        if self.lyrics:
            try:
                pos_res = proxy.call_sync(
                    "org.freedesktop.DBus.Properties.Get",
                    GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", "Position")),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
                pos_sec = pos_res.unpack()[0] / 1_000_000.0
                self.update_current_line(pos_sec)
            except Exception:
                pass

        return True

    def fetch_lyrics(self, title, artist, album, duration, file_url, track_id):
        """Fetch synced lyrics from local .lrc or LRCLIB."""
        synced_text = None

        # 1. Check local .lrc file if playing from local file
        if file_url and file_url.startswith("file://"):
            try:
                local_path = urllib.parse.unquote(urllib.parse.urlparse(file_url).path)
                lrc_path = os.path.splitext(local_path)[0] + ".lrc"
                if os.path.exists(lrc_path):
                    with open(lrc_path, "r", encoding="utf-8", errors="ignore") as f:
                        synced_text = f.read()
            except Exception:
                pass

        # 2. Query LRCLIB
        if not synced_text:
            headers = {"User-Agent": "VlcLyricsOverlay/1.0 (https://github.com/vlc-lyrics-overlay)"}

            # Strategy A: exact api/get
            params = {"track_name": title}
            if artist:
                params["artist_name"] = artist
            if album:
                params["album_name"] = album
            if duration:
                params["duration"] = int(duration)

            try:
                r = requests.get("https://lrclib.net/api/get", params=params, headers=headers, timeout=5)
                if r.ok:
                    data = r.json()
                    if isinstance(data, dict) and data.get("syncedLyrics"):
                        synced_text = data["syncedLyrics"]
            except Exception:
                pass

            # Strategy B: api/get with track and artist only
            if not synced_text and artist:
                try:
                    r = requests.get(
                        "https://lrclib.net/api/get",
                        params={"track_name": title, "artist_name": artist},
                        headers=headers,
                        timeout=5,
                    )
                    if r.ok:
                        data = r.json()
                        if isinstance(data, dict) and data.get("syncedLyrics"):
                            synced_text = data["syncedLyrics"]
                except Exception:
                    pass

            # Strategy C: search fallback
            if not synced_text:
                try:
                    query = f"{artist} {title}".strip()
                    r = requests.get(
                        "https://lrclib.net/api/search",
                        params={"q": query},
                        headers=headers,
                        timeout=5,
                    )
                    if r.ok:
                        data = r.json()
                        if isinstance(data, list):
                            for item in data:
                                if item.get("syncedLyrics"):
                                    synced_text = item["syncedLyrics"]
                                    break
                except Exception:
                    pass

        # Cache result
        cache_key = (artist.lower(), title.lower())
        self.lyrics_cache[cache_key] = synced_text

        GLib.idle_add(self.on_lyrics_loaded, track_id, synced_text)

    def on_lyrics_loaded(self, track_id, synced_text):
        if self.current_track_id != track_id:
            return  # Track already changed

        if not synced_text:
            self.prev_label.set_text("")
            self.curr_label.set_text("No synced lyrics found")
            self.next_label.set_text("")
            return

        self.lyrics = parse_lrc(synced_text)
        self.current_index = -2

    def update_current_line(self, current_time):
        """Find line index corresponding to current_time and update labels."""
        if not self.lyrics:
            return

        idx = -1
        for i, (t, _) in enumerate(self.lyrics):
            if t <= current_time:
                idx = i
            else:
                break

        if idx == self.current_index:
            return

        self.current_index = idx

        prev_text = self.lyrics[idx - 1][1] if idx > 0 else ""
        curr_text = self.lyrics[idx][1] if idx >= 0 else "..."
        next_text = self.lyrics[idx + 1][1] if idx + 1 < len(self.lyrics) else ""

        self.prev_label.set_text(prev_text)
        self.curr_label.set_text(curr_text if curr_text else "♪ ♪ ♪")
        self.next_label.set_text(next_text)


def main():
    app = Gtk.Application(application_id="org.vlc.lyrics.overlay")

    def on_activate(app):
        win = LyricsOverlay(app)
        win.present()

    app.connect("activate", on_activate)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
