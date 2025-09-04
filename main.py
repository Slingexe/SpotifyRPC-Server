from __future__ import annotations

import os
import time
import typing as t
import requests
from dataclasses import dataclass
from spotipy import Spotify, SpotifyOAuth, SpotifyException
from dotenv import load_dotenv
from server import NowPlayingServer, TrackInfo


# =========================
# Env & Utilities
# =========================

def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, str(default)).strip().lower()
    return val in {"1", "true", "t", "yes", "y", "on"}

def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

def secrets_if(mask: bool, value: t.Any) -> str:
    return str(value) if mask else "***"


@dataclass(frozen=True)
class Config:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str
    timeout: int
    debug: bool
    print_secrets: bool

    @staticmethod
    def from_env() -> "Config":
        load_dotenv()
        return Config(
            spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
            spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
            spotify_redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", ""),
            timeout=max(1, env_int("TIMEOUT", 5)),
            debug=env_bool("DEBUG", False),
            print_secrets=env_bool("PRINT_SECRETS", False),
        )


# =========================
# Feeder (Spotify -> NowPlayingServer)
# =========================

class NowPlayingFeeder:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._http = requests.Session()

        self.sp: Spotify = self._wait_for_spotify_auth()
        self.server = NowPlayingServer()
        self.server.start()

        self._last_track_uri: str | None = None
        self._last_is_playing: bool | None = None
        self._last_metadata: dict[str, t.Any] = {}

        # Shared mutable TrackInfo instance
        self.server_data = TrackInfo(
            is_offline=True, is_playing=False,
            ratelimit=False, rl_time=0,
            title="", artist="", uri="", artURL="",
            duration=0, progress=0,
            context_type="", context_uri="", context_name=""
        )
        self._publish_server_state()  # ensure initial state visible

    # ---------- Logging ----------

    def log(self, *args: t.Any, **kwargs: t.Any) -> None:
        if self.cfg.debug:
            print("[DEBUG]", *args, **kwargs)

    def log_env(self) -> None:
        c = self.cfg
        mask = c.print_secrets
        self.log("✅ Loaded environment variables:")
        self.log(f"  SPOTIFY_CLIENT_ID:      {secrets_if(mask, c.spotify_client_id)}")
        self.log(f"  SPOTIFY_CLIENT_SECRET:  {secrets_if(mask, c.spotify_client_secret)}")
        self.log(f"  SPOTIFY_REDIRECT_URI:   {secrets_if(mask, c.spotify_redirect_uri)}")
        self.log(f"  TIMEOUT:                {c.timeout}")
        self.log(f"  DEBUG:                  {c.debug}")
        self.log(f"  PRINT_SECRETS:          {c.print_secrets}")

    # ---------- Spotify ----------

    def _wait_for_spotify_auth(self) -> Spotify:
        """Loop until we can authenticate + make a basic call (covers network flaps)."""
        while True:
            try:
                auth = SpotifyOAuth(
                    client_id=self.cfg.spotify_client_id,
                    client_secret=self.cfg.spotify_client_secret,
                    redirect_uri=self.cfg.spotify_redirect_uri,
                    scope="user-read-playback-state",
                )
                sp = Spotify(auth_manager=auth, retries=0)
                self._spotify_api_call(sp.current_playback)  # probe
                print("✅ Spotify authenticated and reachable.")
                return sp
            except (SpotifyException, requests.exceptions.RequestException) as e:
                print("⛔ Spotify not reachable — waiting for internet...")
                self.log("Auth/connect error:", e)
                time.sleep(5)

    def _spotify_api_call(self, func: t.Callable, *args: t.Any, **kwargs: t.Any) -> t.Any:
        """Run a Spotify API call and handle 429 globally, returning the result."""
        while True:
            try:
                return func(*args, **kwargs)
            except SpotifyException as e:
                if e.http_status == 429:
                    retry_after = int(e.headers.get("Retry-After", 30))
                    self.log(f"⚠️ Spotify rate limit — sleeping {retry_after}s")

                    # Expose rate-limit state to server
                    self._set_ratelimit_state(retry_after)
                    time.sleep(retry_after)

                    # Clear RL state when resuming
                    self._clear_server_data()
                else:
                    raise

    # ---------- Server state helpers ----------

    def _publish_server_state(self) -> None:
        try:
            self.server.update(TrackInfo=self.server_data)
        except Exception as e:
            self.log("Server update failed:", e)

    def _clear_server_data(self) -> None:
        """Reset server data to offline state."""
        sd = self.server_data
        sd.is_offline = True
        sd.is_playing = False
        sd.ratelimit = False
        sd.rl_time = 0
        sd.title = ""
        sd.artist = ""
        sd.uri = ""
        sd.artURL = ""
        sd.duration = 0
        sd.progress = 0
        sd.context_type = ""
        sd.context_uri = ""
        sd.context_name = ""
        self._publish_server_state()
        self.log("Updated Now Playing server to offline state.")

    def _set_ratelimit_state(self, retry_after: int) -> None:
        sd = self.server_data
        sd.is_offline = False
        sd.is_playing = False
        sd.ratelimit = True
        sd.rl_time = int(retry_after)
        self._publish_server_state()
        self.log("Updated Now Playing server with rate limit info.")

    # ---------- Fetch & Normalize ----------

    def _fetch_playback(self) -> dict[str, t.Any] | None:
        """
        Returns None for 'nothing', or a normalized dict:
        {
          "is_playing": bool,
          "uri": str,
          "title": str,
          "artist": str,
          "duration": int (sec),
          "progress": int (sec),
          "album_name": str,
          "album_img": str,
          "context_type": str,
          "context_uri": str,
          "context_name": str
        }
        """
        pb = self._spotify_api_call(self.sp.current_playback)
        if not pb or pb.get("progress_ms") is None or not pb.get("item"):
            return None

        item = pb["item"]
        title = item.get("name", "Unknown")
        artist = ", ".join(a.get("name", "Unknown") for a in item.get("artists", []))
        duration = (item.get("duration_ms") or 0) // 1000
        progress = (pb.get("progress_ms") or 0) // 1000
        album_img = ((item.get("album", {}) or {}).get("images") or [{}])[0].get("url", "")
        context = pb.get("context") or {}
        context_type = context.get("type") or ""
        context_uri = context.get("uri") or ""
        context_name = ""

        # Best-effort context name resolution
        try:
            if context_type == "playlist" and context_uri:
                playlist_id = context_uri.split(":")[-1]
                playlist = self._spotify_api_call(self.sp.playlist, playlist_id)
                context_name = playlist.get("name", "")
            elif context_type == "album" and context_uri:
                album_id = context_uri.split(":")[-1]
                album = self._spotify_api_call(self.sp.album, album_id)
                context_name = album.get("name", "")
            elif context_uri and ":collection" in context_uri:
                context_type = "user_collection"
                context_name = "Liked Songs"
        except Exception as e:
            self.log("Context lookup failed:", e)

        return {
            "is_playing": bool(pb.get("is_playing", False)),
            "uri": item.get("uri", ""),
            "title": title,
            "artist": artist,
            "duration": duration,
            "progress": progress,
            "album_name": (item.get("album", {}) or {}).get("name", ""),
            "album_img": album_img or "",
            "context_type": context_type,
            "context_uri": context_uri,
            "context_name": context_name,
        }

    # ---------- One tick ----------

    def tick(self) -> None:
        try:
            data = self._fetch_playback()

            if not data:
                if self._last_track_uri:
                    self.log("Clearing server data (nothing playing).")
                    self._last_track_uri = None
                    self._last_is_playing = None
                    self._last_metadata = {}
                    self._clear_server_data()
                return

            is_playing = data["is_playing"]
            self.log(
                f"Current track: {data['title']} by {data['artist']} "
                f"({ 'playing' if is_playing else 'paused' })"
            )
            self.log(
                f"Track details: duration={data['duration']}s, "
                f"progress={data['progress']}s, album_img={data['album_img']}"
            )

            # Update shared TrackInfo
            sd = self.server_data
            sd.is_offline = False
            sd.is_playing = is_playing
            sd.ratelimit = False
            sd.rl_time = 0
            sd.title = data["title"]
            sd.artist = data["artist"]
            sd.uri = data["uri"]
            sd.artURL = data["album_img"]
            sd.duration = int(data["duration"])
            sd.progress = int(data["progress"])
            sd.context_type = data["context_type"]
            sd.context_uri = data["context_uri"]
            sd.context_name = data["context_name"]

            self._publish_server_state()
            self.log("Updated Now Playing server with current track info.")

            # local cache for paused-state transitions if you add them later
            self._last_track_uri = data["uri"]
            self._last_is_playing = is_playing
            self._last_metadata = data

        except SpotifyException as e:
            if e.http_status == 429:
                retry_after = int(e.headers.get("Retry-After", 5))
                self.log(f"⚠️ Spotify rate limit — sleeping {retry_after}s.")
                self._set_ratelimit_state(retry_after)
                time.sleep(retry_after)
            else:
                self.log("🔁 Spotify API error — re-authenticating:", e)
                self.sp = self._wait_for_spotify_auth()
                self._clear_server_data()

        except requests.exceptions.RequestException as e:
            self.log("🔁 Network error — re-authenticating Spotify:", e)
            self.sp = self._wait_for_spotify_auth()
            self._clear_server_data()

        except Exception as e:
            self.log("❌ Unhandled error during update:", e)

    # ---------- Clean shutdown ----------

    def shutdown(self) -> None:
        try:
            # If your NowPlayingServer exposes a stop(), call it here.
            if hasattr(self.server, "stop"):
                self.server.stop()
        except Exception:
            pass


# =========================
# Entrypoint
# =========================

if __name__ == "__main__":
    cfg = Config.from_env()
    feeder = NowPlayingFeeder(cfg)
    feeder.log_env()

    print("🎧 Spotify API Server for RPC")
    try:
        while True:
            feeder.tick()
            time.sleep(cfg.timeout)
    except KeyboardInterrupt:
        feeder.log("Shutting down...")
        feeder.shutdown()
