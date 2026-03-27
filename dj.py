"""DJ Agent — an interactive Spotify DJ from the terminal.

Controls the Spotify desktop app via AppleScript (macOS) and uses the
Spotify Web API for search and queue management. Supports natural language
commands via LLM intent parsing (requires DEDALUS_API_KEY).

Usage:
    python dj.py
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from dedalus_labs import AsyncDedalus
from dotenv import load_dotenv


load_dotenv()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
MODEL = os.getenv("MODEL", "anthropic/claude-sonnet-4-20250514")

SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
SPOTIFY_SCOPES = "user-modify-playback-state user-read-playback-state"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".spotify_user_token")

NORMALIZE_PROMPT = """\
You are a command normalizer for a Spotify DJ agent.
Convert the user's natural language into exactly ONE of these commands:

  play <song or artist or description>
  pause
  resume
  next
  prev
  now playing
  search <query>
  search <N> <query>
  queue <song>
  queue all
  queue <space-separated numbers like 1 3 5>
  remove <space-separated numbers like 1 3 5>
  show queue
  clear queue
  volume <0-100>
  shuffle on
  shuffle off
  repeat on
  repeat off
  devices
  none

STRICT RULES — you MUST follow every one:
1. Output ONLY a single command from the list above. NEVER output explanations, apologies,
   or sentences. If the input is not music-related, output exactly: none
2. "play <query>" = hear/play something. "search <query>" = find/browse music.
3. "queue <numbers>" = add specific search results to queue by their number.
   "remove <numbers>" = remove specific tracks from queue by their number.
4. "play next", "next song", "skip" = next
5. If the user asks to "make a queue" or "build a playlist" with a genre/mood but no specific
   artist, use "search <query>" with the genre/mood as query so they can pick songs.
6. The <query> must be a clean Spotify search query — artist names, song titles, or genres ONLY.
   NEVER include filler words like "songs", "tracks", "popular", "best", "top", "banger", "hits".
   Examples:
     "find me 5 popular OneRepublic songs" -> search 5 OneRepublic
     "play the most played song by Drake" -> play Drake
     "make a queue with 10 chill songs" -> search chill
     "show me some 2010s bangers" -> search 2010s
7. If the user wants to play the #1 result from the last search AND also a specific song,
   use "play <song>". The search results context is provided — reference them by number if needed.
"""

last_search_results: list[dict] = []  # [{name, artist, uri}, ...]
queued_tracks: list[dict] = []       # tracks we explicitly added to Spotify's queue


class SpotifyClient:
    """Spotify API client — client credentials for search, user OAuth for queue."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token: str | None = None
        self.user_token: str | None = None
        self.refresh_token: str | None = None
        self.http = httpx.AsyncClient()
        self._load_cached_token()

    def _load_cached_token(self) -> None:
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
                self.refresh_token = data.get("refresh_token")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_cached_token(self) -> None:
        with open(TOKEN_FILE, "w") as f:
            json.dump({"refresh_token": self.refresh_token}, f)

    # --- Client credentials (search) ---

    async def _get_token(self) -> str:
        if self.token:
            return self.token
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = await self.http.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]
        return self.token

    async def search(self, query: str, search_type: str = "track", limit: int = 5) -> dict:
        token = await self._get_token()
        params = {"q": query, "type": search_type, "limit": min(limit, 10)}
        resp = await self.http.get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if resp.status_code == 401:
            self.token = None
            token = await self._get_token()
            resp = await self.http.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Spotify API {resp.status_code}: {detail}")
        return resp.json()

    # --- User OAuth (queue, playback state) ---

    async def _ensure_user_token(self) -> str:
        if self.user_token:
            return self.user_token
        if self.refresh_token:
            try:
                return await self._refresh_user_token()
            except Exception:
                self.refresh_token = None
        return await self._authorize_user()

    async def _authorize_user(self) -> str:
        auth_code: str | None = None

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal auth_code
                query = parse_qs(urlparse(self.path).query)
                auth_code = query.get("code", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Done! You can close this tab.</h2></body></html>")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 8888), Handler)

        params = urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "scope": SPOTIFY_SCOPES,
            "state": secrets.token_urlsafe(16),
        })
        url = f"https://accounts.spotify.com/authorize?{params}"

        print("\n  Spotify login required for queue features. Opening browser...", flush=True)
        webbrowser.open(url)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, server.handle_request)
        server.server_close()

        if not auth_code:
            raise RuntimeError("Spotify authorization failed — no code received.")

        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = await self.http.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": SPOTIFY_REDIRECT_URI,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.user_token = data["access_token"]
        self.refresh_token = data.get("refresh_token")
        self._save_cached_token()
        print("  Spotify connected!\n", flush=True)
        return self.user_token

    async def _refresh_user_token(self) -> str:
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = await self.http.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.user_token = data["access_token"]
        if "refresh_token" in data:
            self.refresh_token = data["refresh_token"]
            self._save_cached_token()
        return self.user_token

    async def _user_api(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated user API call with auto-refresh on 401."""
        token = await self._ensure_user_token()
        resp = await self.http.request(
            method, f"https://api.spotify.com/v1{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        if resp.status_code == 401:
            self.user_token = None
            token = await self._ensure_user_token()
            resp = await self.http.request(
                method, f"https://api.spotify.com/v1{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        return resp

    async def add_to_queue(self, uri: str) -> bool:
        resp = await self._user_api("POST", "/me/player/queue", params={"uri": uri})
        return 200 <= resp.status_code < 300

    async def get_queue(self) -> dict:
        resp = await self._user_api("GET", "/me/player/queue")
        return resp.json() if resp.status_code == 200 else {}

    async def clear_queue(self, num_queued: int) -> int:
        """Clear queue by skipping through queued tracks, then restoring seamlessly."""
        resp = await self._user_api("GET", "/me/player")
        if resp.status_code != 200:
            return 0
        player = resp.json()
        current_uri = player.get("item", {}).get("uri")
        if not current_uri:
            return 0

        start = time.monotonic()
        progress = player.get("progress_ms", 0)

        # Only skip through explicitly queued tracks (not autoplay)
        skips = num_queued if num_queued > 0 else len((await self.get_queue()).get("queue", []))
        if skips == 0:
            return 0

        # Fire all skips as fast as possible
        for _ in range(skips):
            await self._user_api("POST", "/me/player/next")

        # Restore original track, adjusting position for time elapsed
        elapsed_ms = int((time.monotonic() - start) * 1000)
        await self._user_api(
            "PUT", "/me/player/play",
            json={"uris": [current_uri], "position_ms": progress + elapsed_ms},
        )
        return skips


def _parse_tracks(data: dict) -> list[dict]:
    """Extract track info from Spotify search response."""
    items = data.get("tracks", {}).get("items", [])
    return [
        {
            "name": t.get("name", "Unknown"),
            "artist": ", ".join(a["name"] for a in t.get("artists", [])),
            "uri": t.get("uri", ""),
        }
        for t in items
    ]


def _dedup_tracks(tracks: list[dict]) -> list[dict]:
    """Remove duplicate tracks by normalized name. Keeps first (most popular) occurrence."""
    seen: set[str] = set()
    result = []
    for t in tracks:
        name = re.sub(r"\s*[\(\[].*?[\)\]]", "", t["name"]).strip().lower()
        name = re.sub(r"\s+", " ", name)
        key = f"{name}||{t['artist'].lower()}"
        if key not in seen and name not in seen:
            seen.add(key)
            seen.add(name)
            result.append(t)
    return result


def _format_track_list(tracks: list[dict], header: str = "") -> str:
    """Pretty-print a numbered list of tracks."""
    if not tracks:
        return "  No results found."
    lines = [f"  {header}"] if header else []
    for i, t in enumerate(tracks, 1):
        lines.append(f"  {i}. \"{t['name']}\" - {t['artist']}")
    return "\n".join(lines)


async def _normalize_command(
    client: AsyncDedalus, user_input: str, context: str = "",
) -> str | None:
    """Ask the LLM to convert natural language into a structured command."""
    prompt = NORMALIZE_PROMPT
    if context:
        prompt += f"\nFor reference, the last search results were:\n{context}\n"
    try:
        stream = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_input},
            ],
            stream=True,
        )
        output = ""
        async for chunk in stream:
            if hasattr(chunk, "choices") and chunk.choices:
                delta = chunk.choices[0].delta
                if hasattr(delta, "content") and delta.content:
                    output += delta.content
        return output.strip() or None
    except Exception:
        return None


async def _play_track(spotify: SpotifyClient, query: str) -> None:
    """Search for a track and play it via AppleScript."""
    search_query = query
    if " by " in query.lower():
        parts = query.lower().split(" by ", 1)
        search_query = f"track:{parts[0].strip()} artist:{parts[1].strip()}"
    try:
        data = await spotify.search(search_query, limit=3)
        tracks = _parse_tracks(data)
        if not tracks:
            print(f"  No results for \"{query}\".", flush=True)
            return
        t = tracks[0]
        subprocess.run(
            ["osascript", "-e", f'tell application "Spotify" to open location "{t["uri"]}"'],
            check=False,
        )
        print(f"  Playing \"{t['name']}\" - {t['artist']}", flush=True)
    except Exception as e:
        print(f"  Error: {e}", flush=True)


async def handle_command(
    spotify: SpotifyClient,
    user_input: str,
    dedalus: AsyncDedalus | None = None,
) -> None:
    """Normalize user input via LLM, then execute the resolved command."""

    # Step 1: Always run through LLM to normalize input
    if dedalus:
        context = ""
        if last_search_results:
            context = "\n".join(
                f"{i}. \"{t['name']}\" - {t['artist']}"
                for i, t in enumerate(last_search_results, 1)
            )
        normalized = await _normalize_command(dedalus, user_input, context)
        if normalized:
            # Guard: if LLM returned a sentence instead of a command, reject it
            if normalized.lower().strip() == "none" or len(normalized) > 120:
                print("  Sorry, I can only help with music commands (play, search, queue, etc.).", flush=True)
                return
            print(f"  -> {normalized}", flush=True)
            user_input = normalized

    # Step 2: Execute the (now normalized) command
    await _execute_command(spotify, user_input)


async def _execute_command(spotify: SpotifyClient, user_input: str) -> None:
    """Execute a normalized command."""
    lower = user_input.lower().strip()

    if lower in ("pause", "stop"):
        subprocess.run(["osascript", "-e", 'tell application "Spotify" to pause'], check=False)
        print("  Paused.", flush=True)
        return

    if lower in ("resume",):
        subprocess.run(["osascript", "-e", 'tell application "Spotify" to play'], check=False)
        print("  Resumed.", flush=True)
        return

    if lower in ("skip", "next"):
        subprocess.run(["osascript", "-e", 'tell application "Spotify" to next track'], check=False)
        if queued_tracks:
            playing = queued_tracks.pop(0)
            remaining = f" ({len(queued_tracks)} left in queue)" if queued_tracks else ""
            print(f"  Playing \"{playing['name']}\" - {playing['artist']}{remaining}", flush=True)
        else:
            print("  Skipped to next track.", flush=True)
        return

    if lower in ("prev", "previous"):
        subprocess.run(["osascript", "-e", 'tell application "Spotify" to previous track'], check=False)
        print("  Back to previous track.", flush=True)
        return

    if lower in ("now playing",):
        result = subprocess.run(
            ["osascript", "-e", """
                tell application "Spotify"
                    if player state is playing then
                        set t to name of current track
                        set a to artist of current track
                        set p to player position
                        set d to duration of current track / 1000
                        set pm to (p div 60) as integer
                        set ps to (p mod 60) as integer
                        set dm to (d div 60) as integer
                        set ds to (d mod 60) as integer
                        return "\\\"" & t & "\\\" - " & a & " (" & pm & ":" & text -2 thru -1 of ("0" & ps) & " / " & dm & ":" & text -2 thru -1 of ("0" & ds) & ")"
                    else
                        return "Nothing playing right now."
                    end if
                end tell
            """],
            capture_output=True, text=True, check=False,
        )
        print(f"  {result.stdout.strip()}", flush=True)
        return

    if lower in ("devices",):
        result = subprocess.run(
            ["osascript", "-e", """
                tell application "System Events"
                    set isRunning to (exists (processes whose name is "Spotify"))
                end tell
                if isRunning then
                    tell application "Spotify"
                        set vol to sound volume
                        set shuf to shuffling
                        set rep to repeating
                        set ps to player state as string
                        return "Spotify Desktop — " & ps & " | volume: " & vol & "% | shuffle: " & shuf & " | repeat: " & rep
                    end tell
                else
                    return "Spotify is not running."
                end if
            """],
            capture_output=True, text=True, check=False,
        )
        print(f"  {result.stdout.strip()}", flush=True)
        return

    if lower.startswith("volume "):
        vol = lower.replace("volume ", "").strip()
        subprocess.run(
            ["osascript", "-e", f'tell application "Spotify" to set sound volume to {vol}'],
            check=False,
        )
        print(f"  Volume set to {vol}%.", flush=True)
        return

    if lower.startswith("shuffle "):
        state = "true" if "on" in lower else "false"
        subprocess.run(
            ["osascript", "-e", f'tell application "Spotify" to set shuffling to {state}'],
            check=False,
        )
        label = "on" if state == "true" else "off"
        print(f"  Shuffle {label}.", flush=True)
        return

    if lower.startswith("repeat "):
        state = "false" if "off" in lower else "true"
        subprocess.run(
            ["osascript", "-e", f'tell application "Spotify" to set repeating to {state}'],
            check=False,
        )
        label = "on" if state == "true" else "off"
        print(f"  Repeat {label}.", flush=True)
        return

    if lower in ("show queue",):
        try:
            data = await spotify.get_queue()
            currently = data.get("currently_playing")
            queue_items = data.get("queue", [])

            current_uri = currently.get("uri") if currently else None
            filtered = [t for t in queue_items if t.get("uri") != current_uri]

            if currently:
                name = currently.get("name", "Unknown")
                artist = ", ".join(a["name"] for a in currently.get("artists", []))
                print(f"  Now playing: \"{name}\" - {artist}", flush=True)
            if filtered:
                print("  Up next:", flush=True)
                for i, t in enumerate(filtered[:10], 1):
                    name = t.get("name", "Unknown")
                    artist = ", ".join(a["name"] for a in t.get("artists", []))
                    print(f"    {i}. \"{name}\" - {artist}", flush=True)
            elif not currently:
                print("  Queue is empty.", flush=True)
        except Exception as e:
            print(f"  Error: {e}", flush=True)
        return

    if lower in ("clear queue",):
        count = len(queued_tracks)
        queued_tracks.clear()
        try:
            cleared = await spotify.clear_queue(count)
            print(f"  Cleared {cleared} track(s) from queue." if cleared else "  Queue is already empty.", flush=True)
        except Exception as e:
            print(f"  Error: {e}", flush=True)
        return

    if lower.startswith("search "):
        raw = user_input[7:].strip()
        limit = 10
        parts_raw = raw.split(None, 1)
        if len(parts_raw) == 2 and parts_raw[0].isdigit():
            limit = int(parts_raw[0])
            raw = parts_raw[1]

        search_query = raw
        if " by " in raw.lower():
            parts = raw.lower().split(" by ", 1)
            search_query = f"track:{parts[0].strip()} artist:{parts[1].strip()}"
        try:
            data = await spotify.search(search_query, limit=10)
            tracks = _parse_tracks(data)
            tracks = _dedup_tracks(tracks)[:limit]
            last_search_results.clear()
            last_search_results.extend(tracks)
            print(_format_track_list(tracks), flush=True)
            if tracks:
                print("\n  Tip: \"queue all\" to queue all, or \"queue 1 3 5\" to pick specific tracks.", flush=True)
        except Exception as e:
            print(f"  Search error: {e}", flush=True)
        return

    if lower.startswith("remove "):
        arg = user_input[7:].strip()
        tokens = arg.replace(",", " ").split()
        if tokens and all(tok.isdigit() for tok in tokens):
            if not queued_tracks:
                print("  Queue is empty — nothing to remove.", flush=True)
                return
            indices = sorted([int(tok) - 1 for tok in tokens], reverse=True)
            removed = []
            for idx in indices:
                if 0 <= idx < len(queued_tracks):
                    removed.append(queued_tracks.pop(idx))
            if removed:
                for t in reversed(removed):
                    print(f"  - \"{t['name']}\" - {t['artist']}", flush=True)
                print(f"  Queue now has {len(queued_tracks)} track(s).", flush=True)
            else:
                print(f"  Numbers out of range (1-{len(queued_tracks) + len(removed)}).", flush=True)
        else:
            print("  Usage: remove <number(s)>, e.g. \"remove 2\" or \"remove 1 3 5\"", flush=True)
        return

    if lower in ("queue all",):
        if not last_search_results:
            print("  No search results to queue. Run a search first.", flush=True)
            return
        count = 0
        for t in last_search_results:
            try:
                ok = await spotify.add_to_queue(t["uri"])
                if ok:
                    print(f"  + \"{t['name']}\" - {t['artist']}", flush=True)
                    queued_tracks.append(t)
                    count += 1
                else:
                    print(f"  x Failed to queue \"{t['name']}\"", flush=True)
            except Exception as e:
                print(f"  Error queuing \"{t['name']}\": {e}", flush=True)
        print(f"\n  Added {count} track(s) to queue.", flush=True)
        return

    if lower.startswith("queue "):
        arg = user_input[6:].strip()

        tokens = arg.replace(",", " ").split()
        if tokens and all(tok.isdigit() for tok in tokens):
            if not last_search_results:
                print("  No search results. Run a search first.", flush=True)
                return
            for tok in tokens:
                idx = int(tok) - 1
                if 0 <= idx < len(last_search_results):
                    t = last_search_results[idx]
                    try:
                        ok = await spotify.add_to_queue(t["uri"])
                        if ok:
                            print(f"  + \"{t['name']}\" - {t['artist']}", flush=True)
                            queued_tracks.append(t)
                        else:
                            print(f"  x Failed to queue \"{t['name']}\"", flush=True)
                    except Exception as e:
                        print(f"  Error: {e}", flush=True)
                else:
                    print(f"  #{tok} is out of range (1-{len(last_search_results)}).", flush=True)
            return

        search_query = arg
        if " by " in arg.lower():
            parts = arg.lower().split(" by ", 1)
            search_query = f"track:{parts[0].strip()} artist:{parts[1].strip()}"
        try:
            data = await spotify.search(search_query, limit=1)
            tracks = _parse_tracks(data)
            if tracks:
                ok = await spotify.add_to_queue(tracks[0]["uri"])
                if ok:
                    print(f"  + \"{tracks[0]['name']}\" - {tracks[0]['artist']}", flush=True)
                    queued_tracks.append(tracks[0])
                else:
                    print(f"  Failed to add to queue. Is Spotify playing?", flush=True)
            else:
                print(f"  No results for \"{arg}\".", flush=True)
        except Exception as e:
            print(f"  Error: {e}", flush=True)
        return

    if lower.startswith("play "):
        await _play_track(spotify, user_input[5:].strip())
        return

    # Fallback: treat as play query
    await _play_track(spotify, user_input)


BANNER = r"""
    ____       __       ___                    __
   / __ \     / /      /   | ____ ____  ____  / /_
  / / / /    / /      / /| |/ __ `/ _ \/ __ \/ __/
 / /_/ / _ _/ /      / ___ / /_/ /  __/ / / / /_
/_____/ (_ _ /      /_/  |_\__, /\___/_/ /_/\__/
                          /____/
"""

HELP_TEXT = """  Commands:

    play <song>       Search and play a track
    pause             Pause playback
    resume            Resume playback
    next              Skip to next track
    prev              Go to previous track
    search <query>    Search for tracks
    queue <song>      Add a track to the queue
    exit              Exit DJ Agent

  Or just type naturally — "put on some chill vibes", "what's playing?"
"""


async def main() -> None:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("  Error: SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set. See env.example.")
        sys.exit(1)

    spotify = SpotifyClient(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

    dedalus: AsyncDedalus | None = None
    if os.getenv("DEDALUS_API_KEY"):
        dedalus = AsyncDedalus(timeout=30)

    print(BANNER)
    if dedalus:
        print("  Natural language enabled — just say what you want.")
    print(HELP_TEXT)

    while True:
        try:
            user_input = input("  dj> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            print("  Goodbye!")
            break

        if user_input.lower() in ("help", "h", "?"):
            print(HELP_TEXT)
            continue

        await handle_command(spotify, user_input, dedalus)
        print()


if __name__ == "__main__":
    asyncio.run(main())
