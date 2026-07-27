# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import math
import os
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None
    
    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Ableton: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(15.0)  # Increased timeout for operations that might take longer
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response"""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement"
        ]

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {"create_audio_clip": 65.0}
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type
            if command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        yield {}
    finally:
        global _ableton_connection
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
def get_session_info(ctx: Context) -> str:
    """Get detailed information about the current Ableton session"""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"

@mcp.tool()
def get_track_info(ctx: Context, track_index: int) -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"

# ── Session overview rendering ────────────────────────────────────────────────
# get_session_overview answers "which index is BASS at?" in one socket call
# instead of N get_track_info calls. The map below is the payload that matters —
# rendered as text, because an indented map is far cheaper to read (for a human
# or a small model) than the equivalent JSON. Pure functions; no Live involved.

def _fmt_num(value) -> str:
    """4.0 -> '4', 121.5 -> '121.5'. Keeps the map free of trailing zeros."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else str(round(number, 3))


def _return_label(index: int) -> str:
    """Live labels return tracks A, B, C… — match what's on screen."""
    return chr(ord("A") + index) if 0 <= index < 26 else str(index)


def _track_depth(track: Dict[str, Any], by_index: Dict[int, Dict[str, Any]]) -> int:
    """Nesting depth of a track inside group tracks.

    Only counts a parent that actually IS a group track. A bad group_index once
    rendered as perfectly plausible indentation, which hid the bug that produced
    it — the map should stop indenting rather than invent structure.
    """
    depth = 0
    seen = set()
    parent = track.get("group_index")
    while parent is not None and parent in by_index and parent not in seen:
        if not by_index[parent].get("is_group"):
            break
        seen.add(parent)
        depth += 1
        parent = by_index[parent].get("group_index")
    return depth


def _render_session_overview(overview: Dict[str, Any]) -> str:
    """Render the session as an indented track map, one line per track."""
    tracks = overview.get("tracks") or []
    returns = overview.get("return_tracks") or []
    master = overview.get("master_track") or {}

    lines = ["{0} BPM · {1}/{2} · {3} tracks · {4} returns".format(
        _fmt_num(overview.get("tempo", 0)),
        overview.get("signature_numerator", 4),
        overview.get("signature_denominator", 4),
        len(tracks),
        len(returns))]

    if not tracks:
        lines.append("(no tracks)")
    else:
        by_index = {t["index"]: t for t in tracks}
        cells = ["  " * _track_depth(t, by_index) + str(t.get("name", "")) for t in tracks]
        width = min(max(len(c) for c in cells), 28)

        for track, cell in zip(tracks, cells):
            devices = ", ".join(track.get("devices") or []) or "no devices"
            row = "{0:>3}  {1}  {2:<5}  [{3}]".format(
                track.get("index"), cell.ljust(width), track.get("type", "?"), devices)

            clips = track.get("clips") or []
            if clips:
                row += "  clips: " + ", ".join('{0}:"{1}"({2})'.format(
                    c.get("index"), c.get("name", ""), _fmt_num(c.get("length", 0)))
                    for c in clips)

            flags = [name for name in ("mute", "solo", "arm") if track.get(name)]
            if flags:
                row += "  <" + " ".join(flags) + ">"

            lines.append(row)

    if returns:
        lines.append("returns: " + ", ".join('{0} "{1}" [{2}]'.format(
            _return_label(r.get("index", i)), r.get("name", ""),
            ", ".join(r.get("devices") or []) or "no devices")
            for i, r in enumerate(returns)))

    lines.append("master: [{0}]".format(
        ", ".join(master.get("devices") or []) or "no devices"))

    return "\n".join(lines)


@mcp.tool()
def get_session_overview(ctx: Context, summary_only: bool = False) -> str:
    """
    Read the whole session in one call: every track with its index, type, group
    membership, devices and filled clip slots, plus the return tracks and master.

    Use this FIRST, instead of calling get_track_info once per track to find
    which index a track like 'BASS' sits at. It is also the way to catch a
    stale track/index map — what this returns is always the current session.

    Returns an indented track map (group children are indented under their
    group), followed by the same data as JSON for exact values.

    Parameters:
    - summary_only: Set true to return only the track map and skip the JSON detail
    """
    try:
        ableton = get_ableton_connection()
        overview = ableton.send_command("get_session_overview")
        text = _render_session_overview(overview)

        if summary_only:
            return text
        return "{0}\n\n{1}".format(text, json.dumps(overview, indent=2))
    except Exception as e:
        logger.error(f"Error getting session overview: {str(e)}")
        return f"Error getting session overview: {str(e)}"

@mcp.tool()
def create_midi_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
def set_track_name(ctx: Context, track_index: int, name: str) -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0) -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str) -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]]
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

# ── MIDI note summary helpers ─────────────────────────────────────────────────
# get_clip_notes returns the raw notes (round-trippable) *plus* a digest built
# here, so a small local model can coach on a pattern without reading a wall of
# JSON. Pure functions — nothing here touches Live.

# Ableton shows MIDI 60 as C3, so octave = pitch // 12 - 2. Matching Live's own
# display matters: these are the names the user sees in the clip editor.
_PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Krumhansl-Kessler key profiles. Correlating a duration-weighted pitch-class
# histogram against all 24 rotations is the standard cheap key estimator.
_KK_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_KK_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

# Coarsest first, and every straight grid before any triplet grid — otherwise a
# straight pattern gets reported as the triplet grid that happens to contain it.
_GRIDS = (
    ("1/1", 4.0), ("1/2", 2.0), ("1/4", 1.0), ("1/8", 0.5),
    ("1/16", 0.25), ("1/32", 0.125),
    ("1/4T", 2.0 / 3.0), ("1/8T", 1.0 / 3.0), ("1/16T", 1.0 / 6.0),
)
_GRID_TOLERANCE = 1e-3


def _note_name(pitch: int) -> str:
    """MIDI pitch → Ableton-style note name (60 → 'C3')."""
    return "{0}{1}".format(_PITCH_CLASSES[pitch % 12], pitch // 12 - 2)


def _pearson(xs, ys) -> float:
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if denom == 0:
        return 0.0
    return sum(x * y for x, y in zip(dx, dy)) / denom


def _estimate_key(weights: List[float]):
    """Best-fitting major/minor key for a duration-weighted pitch-class histogram.

    Returns None when there isn't enough tonal information to guess — drum
    patterns, one-note basslines. A confidently wrong key would steer the
    coaching, so silence beats a guess.
    """
    if sum(1 for w in weights if w > 0) < 3:
        return None

    scored = []
    for tonic in range(12):
        rotated = weights[tonic:] + weights[:tonic]
        scored.append((_pearson(rotated, _KK_MAJOR), "{0} major".format(_PITCH_CLASSES[tonic])))
        scored.append((_pearson(rotated, _KK_MINOR), "{0} minor".format(_PITCH_CLASSES[tonic])))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    best, runner_up = scored[0], scored[1]
    # Correlation strength, so a caller can tell "this is clearly A minor" from
    # "this is atonal and 24 keys fit equally badly". Verified against real
    # clips: a written-in-key bassline scores ~0.88, random notes ~0.47.
    confidence = round(best[0], 3)
    if confidence >= 0.75:
        strength = "strong"
    elif confidence >= 0.6:
        strength = "moderate"
    else:
        strength = "weak"

    return {
        "key": best[1],
        "confidence": confidence,
        "strength": strength,
        "runner_up": runner_up[1],
        "runner_up_confidence": round(runner_up[0], 3)
    }


def _detect_grid(onsets):
    """Coarsest note grid every onset lands on, or None if they don't agree."""
    for name, step in _GRIDS:
        if all(abs(o / step - round(o / step)) < _GRID_TOLERANCE for o in onsets):
            return name
    return None


def _summarize_notes(clip_info: Dict[str, Any]) -> Dict[str, Any]:
    """Build the human/LLM-readable digest for a get_clip_notes result."""
    notes = clip_info.get("notes") or []
    length = float(clip_info.get("clip_length") or 0.0)
    numerator = clip_info.get("signature_numerator") or 4
    denominator = clip_info.get("signature_denominator") or 4
    # Live measures time in quarter notes, so a 6/8 bar is 3 beats long.
    beats_per_bar = numerator * 4.0 / denominator
    bars = round(length / beats_per_bar, 3) if beats_per_bar else 0.0
    meter = "{0}/{1}".format(numerator, denominator)
    span = "{0} bars ({1} beats, {2})".format(bars, round(length, 3), meter)

    summary = {
        "text": "",
        "note_count": len(notes),
        "length_beats": round(length, 3),
        "length_bars": bars,
        "time_signature": meter
    }

    if not notes:
        summary["text"] = "Empty clip — no notes. {0}.".format(span)
        return summary

    muted_count = sum(1 for n in notes if n.get("mute"))
    # Muted notes shouldn't sway the key or the range — unless every note is
    # muted, in which case describing nothing would be less useful.
    active = [n for n in notes if not n.get("mute")] or notes

    pitches = [int(n["pitch"]) for n in active]
    lowest, highest = min(pitches), max(pitches)

    pitch_weights = [0.0] * 12
    pitch_counts = {}
    for note in active:
        pc = int(note["pitch"]) % 12
        pitch_weights[pc] += max(float(note.get("duration", 0.0)), 0.0)
        name = _PITCH_CLASSES[pc]
        pitch_counts[name] = pitch_counts.get(name, 0) + 1
    # Fall back to counts if every note has zero duration (shouldn't happen,
    # but a zero histogram would make the key estimate meaningless).
    if sum(pitch_weights) == 0:
        for note in active:
            pitch_weights[int(note["pitch"]) % 12] += 1.0

    onsets = sorted(set(round(float(n["start_time"]), 6) for n in active))
    simultaneous = {}
    for onset in [round(float(n["start_time"]), 6) for n in active]:
        simultaneous[onset] = simultaneous.get(onset, 0) + 1
    max_simultaneous = max(simultaneous.values())

    velocities = [float(n.get("velocity", 0)) for n in active]
    grid = _detect_grid(onsets)
    key = _estimate_key(pitch_weights)
    notes_per_bar = round(len(active) / bars, 2) if bars else None

    summary.update({
        "muted_count": muted_count,
        "pitch_range": {
            "lowest": _note_name(lowest),
            "highest": _note_name(highest),
            "lowest_midi": lowest,
            "highest_midi": highest,
            "span_semitones": highest - lowest
        },
        "pitch_classes": dict(sorted(pitch_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "likely_key": key,
        "rhythm": {
            "grid": grid or "off-grid",
            "distinct_onsets": len(onsets),
            "notes_per_bar": notes_per_bar
        },
        "velocity": {
            "min": round(min(velocities), 1),
            "max": round(max(velocities), 1),
            "mean": round(sum(velocities) / len(velocities), 1)
        },
        "polyphony": {
            "max_simultaneous": max_simultaneous,
            "monophonic": max_simultaneous == 1
        }
    })

    parts = [
        "{0} notes".format(len(notes)),
        span,
        "{0}–{1}".format(_note_name(lowest), _note_name(highest)),
    ]
    if key:
        parts.append("{0} ({1}{2})".format(
            key["key"], key["confidence"],
            ", weak" if key["strength"] == "weak" else ""))
    parts.append("{0} grid".format(grid) if grid else "off-grid")
    if notes_per_bar is not None:
        parts.append("{0} notes/bar".format(notes_per_bar))
    parts.append("monophonic" if max_simultaneous == 1 else "up to {0} at once".format(max_simultaneous))
    parts.append("vel {0}–{1}".format(round(min(velocities)), round(max(velocities))))
    if muted_count:
        parts.append("{0} muted".format(muted_count))
    summary["text"] = " · ".join(parts)

    return summary


@mcp.tool()
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    summary_only: bool = False
) -> str:
    """
    Read the MIDI notes in a Session-view clip.

    The read counterpart of add_notes_to_clip: notes come back with exactly the
    keys that tool writes (pitch, start_time, duration, velocity, mute), so a
    pattern can be read, edited, and written straight back.

    The response leads with a "summary" digest — pitch range, note names,
    density, rhythmic grid, velocity spread, likely key — then the raw "notes"
    list. Read the summary when describing or critiquing a pattern; you only
    need the raw notes when you intend to rewrite them.

    Note names follow Ableton's convention (middle C = C3 = MIDI 60), so they
    match what's shown in Live's clip editor.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - summary_only: Set true to skip the raw note list and return only the digest
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_clip_notes", {
            "track_index": track_index,
            "clip_index": clip_index
        })

        summary = _summarize_notes(result)
        notes = result.pop("notes", [])
        # Summary first, so a reader that truncates still gets the digest.
        result["summary"] = summary
        if not summary_only:
            result["notes"] = notes

        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"

@mcp.tool()
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str) -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
def set_tempo(ctx: Context, tempo: float) -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str) -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
def fire_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
def stop_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"

@mcp.tool()
def start_playback(ctx: Context) -> str:
    """Start playing the Ableton session."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
def stop_playback(ctx: Context) -> str:
    """Stop playing the Ableton session."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
def get_browser_tree(ctx: Context, category_type: str = "all") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
def get_browser_items_at_path(ctx: Context, path: str) -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
def switch_to_arrangement_view(ctx: Context) -> str:
    """Switch Ableton's main window to the Arrangement view."""
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
def set_arrangement_time(ctx: Context, time: float) -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
def get_arrangement_clips(ctx: Context, track_index: int) -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()