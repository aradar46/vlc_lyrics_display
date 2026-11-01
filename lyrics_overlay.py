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
}

.icon-btn {
    min-width: 20px;
    min-height: 20px;
    padding: 2px;
    margin: 0;
    border-radius: 9999px;
    background-color: transparent;
    border: none;
    box-shadow: none;
    color: rgba(220, 225, 235, 0.45);
}

.icon-btn:hover {
    background-color: rgba(255, 255, 255, 0.15);
    color: #ffffff;
}

.icon-btn:active {
    background-color: rgba(255, 255, 255, 0.25);
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


def simplify_title(title):
    """Drop extras like '(Remastered 2011)', '[Live]', ' - Single Version', 'feat. X'."""
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)
    title = re.sub(r"\s+-\s+.*$", "", title)
    title = re.sub(r"\s+(feat|ft)\.?\s.*$", "", title, flags=re.I)
    return title.strip()


def lrclib(endpoint, params):
    """Call one LRCLIB endpoint, always return a list of results (empty on any error)."""
    params = {k: v for k, v in params.items() if v}
    try:
        r = requests.get(
            f"https://lrclib.net/api/{endpoint}",
            params=params,
            headers={"User-Agent": "VlcLyricsOverlay/1.0 (https://github.com/aradar46/vlc_lyrics_display)"},
            timeout=8,
        )
        if r.ok:
            data = r.json()
            return data if isinstance(data, list) else [data]
    except (requests.RequestException, ValueError):
        pass
    return []


def lyric_candidates(title, artist, album, duration, file_url):
    """Yield distinct synced lyrics for a track, most likely match first.

    Lazy on purpose: the first hit is shown right away, and the retry button
    pulls the next one only when asked.
    """
    seen = set()
    local_path = None
    if file_url.startswith("file://"):
        local_path = urllib.parse.unquote(urllib.parse.urlparse(file_url).path)

    # 1. A .lrc file next to the music file
    if local_path:
        try:
            with open(os.path.splitext(local_path)[0] + ".lrc", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            seen.add(text)
            yield text
        except OSError:
            pass

    # No title tag: fall back to the file name
    if not title and local_path:
        title = clean_title(os.path.basename(local_path))
    if not title:
        return
    short = simplify_title(title) or title

    # 2. LRCLIB, from exact match to loose search
    attempts = [
        ("get", {"track_name": title, "artist_name": artist, "album_name": album, "duration": duration}),
        ("get", {"track_name": title, "artist_name": artist}),
        ("get", {"track_name": short, "artist_name": artist}),
        ("search", {"track_name": title, "artist_name": artist}),
        ("search", {"q": f"{artist} {short}"}),
        ("search", {"track_name": short}),
    ]
    for endpoint, params in attempts:
        if endpoint == "get" and not artist:
            continue  # api/get refuses requests without an artist
        results = lrclib(endpoint, params)
        # Prefer same artist, then closest length
        results.sort(key=lambda r: (
            artist.lower() not in (r.get("artistName") or "").lower(),
            abs((r.get("duration") or 0) - duration) if duration else 0,
        ))
        for r in results:
            text = r.get("syncedLyrics")
            if text and text not in seen:
                seen.add(text)
                yield text


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

        # Top bar with track metadata and close button
        self.top_box = Gtk.CenterBox()
        self.header_label = Gtk.Label(label="Waiting for VLC...")
        self.header_label.add_css_class("track-header")
        self.header_label.set_ellipsize(3)
        self.header_label.set_halign(Gtk.Align.CENTER)
        self.top_box.set_center_widget(self.header_label)

        self.retry_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.retry_btn.add_css_class("icon-btn")
        self.retry_btn.set_tooltip_text("Wrong or missing lyrics? Try another source (r)")
        self.retry_btn.connect("clicked", lambda *_: self.show_next_lyrics())
        self.top_box.set_start_widget(self.retry_btn)

        self.close_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        self.close_btn.add_css_class("icon-btn")
        self.close_btn.set_tooltip_text("Close (Esc / q)")
        self.close_btn.connect("clicked", lambda *_: self.get_application().quit())
        self.top_box.set_end_widget(self.close_btn)

        self.card.append(self.top_box)

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
        # Per song: (artist, title) -> [found lyrics list, generator for more]
        self.lyrics_cache = {}
        self.song_key = None
        self.song_args = None
        self.shown = -1  # which entry of the found list is on screen
        self.searching = set()  # song keys with a lookup running

        # Tick timer: check position & update lyrics every 100ms
        GLib.timeout_add(100, self.tick)

    def on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.get_application().quit()
            return True
        if keyval == Gdk.KEY_r:
            self.show_next_lyrics()
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
        raw_title = metadata.get("xesam:title", "")
        # When a new file is opened, VLC first sends the new track with no
        # title, then fills it in a few ms later. Wait for the full info.
        if not raw_title:
            return True
        title = clean_title(raw_title)
        artist_raw = metadata.get("xesam:artist", [])
        artist = artist_raw[0] if isinstance(artist_raw, list) and artist_raw else str(artist_raw)
        album = metadata.get("xesam:album", "")
        duration = max(0, metadata.get("mpris:length", 0) // 1_000_000)
        file_url = metadata.get("xesam:url", "")
        # Title and artist are part of the ID, so late tag updates count as a change too
        track_id = (metadata.get("mpris:trackid") or file_url, title, artist)

        # Detect track change
        if track_id != self.current_track_id:
            self.current_track_id = track_id
            self.current_index = -2
            self.lyrics = []
            header_text = f"{artist} - {title}" if artist else title
            self.header_label.set_text(header_text)
            self.song_key = (artist.lower(), title.lower())
            self.song_args = (title, artist, album, duration, file_url)
            if self.song_key not in self.lyrics_cache:
                self.lyrics_cache[self.song_key] = ([], lyric_candidates(*self.song_args))
            self.shown = -1
            self.show_next_lyrics()

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

    def show_next_lyrics(self):
        """Show the next lyrics found for this song, searching further if needed."""
        key = self.song_key
        if key is None or key in self.searching:
            return
        found, _ = self.lyrics_cache[key]
        if self.shown + 1 < len(found):
            self.show_lyrics(self.shown + 1)
            return
        self.searching.add(key)
        self.lyrics = []
        self.prev_label.set_text("")
        self.curr_label.set_text("Searching for lyrics...")
        self.next_label.set_text("")
        threading.Thread(target=self.search_more, args=(key,), daemon=True).start()

    def search_more(self, key):
        """Worker thread: pull one more result from the song's lyrics generator."""
        found, source = self.lyrics_cache[key]
        text = next(source, None)
        if text:
            found.append(text)
        GLib.idle_add(self.on_search_done, key)

    def on_search_done(self, key):
        self.searching.discard(key)
        if key != self.song_key:
            return  # song changed while searching
        found, _ = self.lyrics_cache[key]
        if self.shown + 1 < len(found):
            self.show_lyrics(self.shown + 1)
        elif found:
            self.show_lyrics(0)  # tried every source, start over from the best one
        else:
            # Nothing found. Forget it so the next retry searches again from scratch
            # (LRCLIB is sometimes just busy).
            self.lyrics_cache[key] = ([], lyric_candidates(*self.song_args))
            self.curr_label.set_text("No synced lyrics found")
            self.next_label.set_text("Click ⟳ to search again")

    def show_lyrics(self, i):
        found, _ = self.lyrics_cache[self.song_key]
        self.shown = i
        self.lyrics = parse_lrc(found[i])
        self.current_index = -2
        self.retry_btn.set_tooltip_text(f"Lyrics {i + 1} of {len(found)} found. Try another source (r)")

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
