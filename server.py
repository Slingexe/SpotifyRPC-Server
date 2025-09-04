from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from pydantic import BaseModel
from typing import Optional
import threading
import uvicorn
import os

class TrackInfo(BaseModel):
    is_offline: bool = True
    is_playing: bool = False
    ratelimit: bool = False
    rl_time: int = 0
    title: str = ""
    artist: str = ""
    uri: str = ""
    artURL: str = ""
    duration: float = 0
    progress: float = 0
    context_type: str = ""
    context_uri: str = ""
    context_name: str = ""

class NowPlayingServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 62011,
        *,
        ssl_certfile: Optional[str] = None,       # e.g. /etc/ssl/myapp/fullchain.pem
        ssl_keyfile: Optional[str] = None,        # e.g. /etc/ssl/myapp/privkey.pem
        ssl_keyfile_password: Optional[str] = None,
        redirect_http_to_https: bool = False,
        log_level: str = "info",
    ):
        self.host = host
        self.port = port
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.ssl_keyfile_password = ssl_keyfile_password
        self.log_level = log_level

        self._data = TrackInfo()
        self.app = FastAPI()

        # CORS
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Optional HTTP->HTTPS redirect (only makes sense when serving HTTPS directly)
        if redirect_http_to_https and self._is_https_enabled:
            self.app.add_middleware(HTTPSRedirectMiddleware)

        # Routes
        @self.app.get("/", response_model=TrackInfo)
        def get_now_playing():
            return self._data

    @property
    def _is_https_enabled(self) -> bool:
        return bool(self.ssl_certfile and self.ssl_keyfile)

    def update(self, *, info: TrackInfo):
        """Update the in-memory now-playing payload."""
        self._data = info

    def start(self):
        # Light validation so it fails loud if files are missing
        ssl_kwargs = {}
        if self._is_https_enabled:
            if not os.path.exists(self.ssl_certfile):
                raise FileNotFoundError(f"ssl_certfile not found: {self.ssl_certfile}")
            if not os.path.exists(self.ssl_keyfile):
                raise FileNotFoundError(f"ssl_keyfile not found: {self.ssl_keyfile}")
            ssl_kwargs = {
                "ssl_certfile": self.ssl_certfile,
                "ssl_keyfile": self.ssl_keyfile,
                "ssl_keyfile_password": self.ssl_keyfile_password,
            }

        thread = threading.Thread(
            target=uvicorn.run,
            kwargs={
                "app": self.app,
                "host": self.host,
                "port": self.port,
                "log_level": self.log_level,
                **ssl_kwargs,
            },
            daemon=True,
        )
        thread.start()
