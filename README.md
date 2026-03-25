# DJ Agent

A terminal-based Spotify DJ that understands natural language. Controls the Spotify desktop app on macOS via AppleScript, uses the Spotify Web API for search and queue, and an LLM for intent parsing.

```
    ____       __       ___                    __
   / __ \     / /      /   | ____ ____  ____  / /_
  / / / /    / /      / /| |/ __ `/ _ \/ __ \/ __/
 / /_/ / _ _/ /      / ___ / /_/ /  __/ / / / /_
/_____/ (_ _ /      /_/  |_\__, /\___/_/ /_/\__/
                          /____/
```

## How it works

Type anything in the terminal — structured commands or plain English. An LLM normalizes your input into a command, then the agent executes it.

```
  dj> put on some OneRepublic
  -> play OneRepublic
  Playing "Counting Stars" - OneRepublic

  dj> search 5 chill vibes
  -> search 5 chill
  1. "Chill Vibes" - Forrest Frank
  2. "Chill" - KAYTRANADA
  ...

  dj> queue 1 3
  + "Chill Vibes" - Forrest Frank
  + "Chill" - KAYTRANADA

  dj> what's playing right now?
  -> now playing
  "Counting Stars" - OneRepublic (1:42 / 4:17)

  dj> turn it up
  -> volume 80
  Volume set to 80%.
```

## Prerequisites

- **macOS** (uses AppleScript to control Spotify desktop app)
- **Python 3.11+**
- **Spotify Premium** account (required for playback control)
- **Spotify desktop app** installed and logged in
- A [Spotify Developer](https://developer.spotify.com/dashboard) app (for API credentials)
- *(Optional)* A [Dedalus](https://dedalus.dev) API key for natural language support

## Setup

```bash
cd dj-agent
pip install -r requirements.txt
cp env.example .env
# Edit .env with your Spotify credentials
```

### Spotify Developer Dashboard

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
2. Create an app (or use an existing one)
3. Copy the **Client ID** and **Client Secret** into your `.env`
4. Under **Redirect URIs**, add: `http://127.0.0.1:8888/callback`

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SPOTIFY_CLIENT_ID` | Yes | From Spotify Developer Dashboard |
| `SPOTIFY_CLIENT_SECRET` | Yes | From Spotify Developer Dashboard |
| `DEDALUS_API_KEY` | No | Enables natural language input (LLM parsing) |
| `MODEL` | No | LLM model for intent parsing (default: `anthropic/claude-sonnet-4-20250514`) |

## Usage

```bash
python dj.py
```

On first queue operation, your browser will open for Spotify OAuth (one-time login). The token is cached for future sessions.

### Commands

| Command | What it does |
|---------|-------------|
| `play <song>` | Search and play a track |
| `play <song> by <artist>` | Play a specific track |
| `pause` | Pause playback |
| `resume` | Resume playback |
| `next` | Skip to next track |
| `prev` | Go to previous track |
| `now playing` | Show current track with progress |
| `search <query>` | Search for tracks (up to 10 results) |
| `search <N> <query>` | Search with specific result count |
| `queue <song>` | Add a track to Spotify's queue |
| `queue <1 3 5>` | Queue specific tracks from last search |
| `queue all` | Queue all tracks from last search |
| `remove <1 3>` | Remove tracks from queue by number |
| `show queue` | Show Spotify's current queue |
| `clear queue` | Clear all queued tracks |
| `volume <0-100>` | Set volume |
| `shuffle on/off` | Toggle shuffle |
| `repeat on/off` | Toggle repeat |
| `devices` | Show Spotify player status |

With `DEDALUS_API_KEY` set, you can also use natural language:

- *"put on some chill vibes"*
- *"what's this song?"*
- *"queue all of those"*
- *"turn the volume down"*
- *"skip to the next one"*

## Architecture

```
User input
  │
  ▼
LLM Normalizer (Dedalus API)
  │  converts natural language to structured command
  ▼
Command Executor
  ├── Search ──► Spotify Web API (client credentials)
  ├── Queue  ──► Spotify Web API (user OAuth)
  └── Playback ──► AppleScript ──► Spotify Desktop App
```

- **Search**: Direct Spotify API calls with client credentials (no user auth needed)
- **Queue**: Spotify Web API with user OAuth (Authorization Code flow, token cached locally)
- **Playback**: AppleScript commands to the Spotify desktop app (play, pause, skip, volume, etc.)
- **Intent parsing**: LLM converts free-form input into one of ~20 canonical commands
