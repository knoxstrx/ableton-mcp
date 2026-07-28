# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import os
import socket
import json
import threading
import time
import traceback

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication
DEFAULT_PORT = 9877
# Bind to loopback only. The MCP server always runs on the same machine as Live,
# so there is no reason to expose this control socket to the LAN.
HOST = "127.0.0.1"

# Payload guard for get_device_parameters. A VST like Vital exposes several
# hundred parameters and a whole-track read multiplies that by the device
# count. This is NOT a display limit — the server does its own filtering and
# formatting — it only stops one pathological plugin from producing a
# multi-megabyte socket frame. Truncation is always reported back
# (parameter_count vs parameters_returned, plus a truncated flag).
MAX_PARAMETERS_PER_DEVICE = 512
# How deep to follow rack/drum-rack chains when include_chains is requested.
MAX_CHAIN_DEPTH = 2

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonMCP Remote Script initializing...")
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False
        
        # Cache the song reference for easier access
        self._song = self.song()
        
        # Start the socket server
        self.start_server()
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False
        
        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        buffer = ''  # Changed from b'' to '' for Python 2
        
        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)
                    
                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break
                    
                    # Accumulate data in buffer with explicit encoding/decoding
                    try:
                        # Python 3: data is bytes, decode to string
                        buffer += data.decode('utf-8')
                    except AttributeError:
                        # Python 2: data is already string
                        buffer += data
                    
                    try:
                        # Try to parse command from buffer
                        command = json.loads(buffer)  # Removed decode('utf-8')
                        buffer = ''  # Clear buffer after successful parse
                        
                        self.log_message("Received command: " + str(command.get("type", "unknown")))
                        
                        # Process the command and get response
                        response = self._process_command(command)
                        
                        # Send the response with explicit encoding
                        try:
                            # Python 3: encode string to bytes
                            client.sendall(json.dumps(response).encode('utf-8'))
                        except AttributeError:
                            # Python 2: string is already bytes
                            client.sendall(json.dumps(response))
                    except ValueError:
                        # Incomplete data, wait for more
                        continue
                        
                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())
                    
                    # Send error response if possible
                    error_response = {
                        "status": "error",
                        "message": str(e)
                    }
                    try:
                        # Python 3: encode string to bytes
                        client.sendall(json.dumps(error_response).encode('utf-8'))
                    except AttributeError:
                        # Python 2: string is already bytes
                        client.sendall(json.dumps(error_response))
                    except:
                        # If we can't send the error, the connection is probably dead
                        break
                    
                    # For serious errors, break the loop
                    if not isinstance(e, ValueError):
                        break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")
    
    def _process_command(self, command):
        """Process a command from the client and return a response"""
        command_type = command.get("type", "")
        params = command.get("params", {})
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Name-based addressing. Resolved ONCE, here, before any handler
            # runs — so every track-addressed command gets it for free and
            # there is exactly one place that decides "which track did you
            # mean". See _resolve_track_index.
            if isinstance(params, dict) and params.get("track_name"):
                resolved_index = self._resolve_track_index(
                    params.get("track_name"), params.get("track_type", "track"))
                if resolved_index is not None:
                    params["track_index"] = resolved_index

            # Route the command to the appropriate handler
            if command_type == "get_session_info":
                response["result"] = self._get_session_info()
            elif command_type == "get_track_info":
                track_index = params.get("track_index", 0)
                response["result"] = self._get_track_info(track_index)
            # Commands that modify Live's state should be scheduled on the main thread
            elif command_type in ["create_midi_track", "set_track_name",
                                 "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
                                 "set_tempo", "fire_clip", "stop_clip",
                                 "start_playback", "stop_playback", "load_browser_item",
                                 # Arrangement view – must run on the main thread
                                 "switch_to_arrangement_view", "set_current_song_time",
                                 "duplicate_session_clip_to_arrangement"]:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = None
                        if command_type == "create_midi_track":
                            index = params.get("index", -1)
                            result = self._create_midi_track(index)
                        elif command_type == "set_track_name":
                            track_index = params.get("track_index", 0)
                            name = params.get("name", "")
                            result = self._set_track_name(track_index, name)
                        elif command_type == "create_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            length = params.get("length", 4.0)
                            result = self._create_clip(track_index, clip_index, length)
                        elif command_type == "create_audio_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            path = params.get("path", "")
                            result = self._create_audio_clip(track_index, clip_index, path)
                        elif command_type == "add_notes_to_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_clip(track_index, clip_index, notes)
                        elif command_type == "set_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_clip_name(track_index, clip_index, name)
                        elif command_type == "set_tempo":
                            tempo = params.get("tempo", 120.0)
                            result = self._set_tempo(tempo)
                        elif command_type == "fire_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._fire_clip(track_index, clip_index)
                        elif command_type == "stop_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._stop_clip(track_index, clip_index)
                        elif command_type == "start_playback":
                            result = self._start_playback()
                        elif command_type == "stop_playback":
                            result = self._stop_playback()
                        elif command_type == "load_instrument_or_effect":
                            track_index = params.get("track_index", 0)
                            uri = params.get("uri", "")
                            result = self._load_instrument_or_effect(track_index, uri)
                        elif command_type == "load_browser_item":
                            track_index = params.get("track_index", 0)
                            item_uri = params.get("item_uri", "")
                            result = self._load_browser_item(track_index, item_uri)
                        # ── Arrangement view commands ──────────────────────────────
                        elif command_type == "switch_to_arrangement_view":
                            result = self._switch_to_arrangement_view()
                        elif command_type == "set_current_song_time":
                            time_val = params.get("time", 0.0)
                            result = self._set_current_song_time(time_val)
                        elif command_type == "duplicate_session_clip_to_arrangement":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            destination_time = params.get("destination_time", 0.0)
                            result = self._duplicate_session_clip_to_arrangement(
                                track_index, clip_index, destination_time)

                        # Put the result in the queue
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})
                
                # Schedule the task to run on the main thread
                try:
                    self.schedule_message(0, main_thread_task)
                except AssertionError:
                    # If we're already on the main thread, execute directly
                    main_thread_task()
                
                # Wait for the response with a timeout. Some commands (notably
                # create_audio_clip, which decodes/imports the audio file on
                # the main thread) can take longer than the default 10s on
                # larger files — give them more headroom.
                long_running_commands = {"create_audio_clip": 60.0}
                queue_timeout = long_running_commands.get(command_type, 10.0)
                try:
                    task_response = response_queue.get(timeout=queue_timeout)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            elif command_type == "get_browser_item":
                uri = params.get("uri", None)
                path = params.get("path", None)
                response["result"] = self._get_browser_item(uri, path)
            elif command_type == "get_browser_categories":
                category_type = params.get("category_type", "all")
                response["result"] = self._get_browser_categories(category_type)
            elif command_type == "get_browser_items":
                path = params.get("path", "")
                item_type = params.get("item_type", "all")
                response["result"] = self._get_browser_items(path, item_type)
            # Add the new browser commands
            elif command_type == "get_browser_tree":
                category_type = params.get("category_type", "all")
                response["result"] = self.get_browser_tree(category_type)
            elif command_type == "get_browser_items_at_path":
                path = params.get("path", "")
                response["result"] = self.get_browser_items_at_path(path)
            # Read-only arrangement command – no main-thread scheduling required
            elif command_type == "get_arrangement_clips":
                track_index = params.get("track_index", 0)
                response["result"] = self._get_arrangement_clips(track_index)
            # Read-only note read – no main-thread scheduling required
            elif command_type == "get_clip_notes":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                response["result"] = self._get_clip_notes(track_index, clip_index)
            # Read-only whole-session read – no main-thread scheduling required
            elif command_type == "get_session_overview":
                response["result"] = self._get_session_overview()
            # Read-only device/parameter read – no main-thread scheduling required
            elif command_type == "get_device_parameters":
                response["result"] = self._get_device_parameters(
                    params.get("track_index", 0),
                    params.get("device_index", -1),
                    params.get("track_type", "track"),
                    bool(params.get("include_chains", False)))
            else:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        return response
    
    # Command implementations
    
    def _safe_song_property(self, attr, cast, default):
        """Read self._song.<attr> with cast, returning default on common failures.
        Catches only narrow exceptions so genuine bugs still surface."""
        try:
            return cast(getattr(self._song, attr))
        except (AttributeError, TypeError, ValueError):
            return default

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                },
                # Transport / playback state — lets clients render a live
                # playhead without polling separately. Each property is read
                # via _safe_song_property so an attribute missing on a given
                # Live version falls back to its default rather than breaking
                # the response shape.
                "is_playing":        self._safe_song_property("is_playing",        bool,  False),
                "current_song_time": self._safe_song_property("current_song_time", float, 0.0),
                "song_length":       self._safe_song_property("song_length",       float, 0.0),
                "loop":              self._safe_song_property("loop",              bool,  False),
                "loop_start":        self._safe_song_property("loop_start",        float, 0.0),
                "loop_length":       self._safe_song_property("loop_length",       float, 0.0),
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index):
        """Get information about a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(track.clip_slots):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    clip_info = {
                        "name": clip.name,
                        "length": clip.length,
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording
                    }
                
                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            result = {
                "index": track_index,
                "name": track.name,
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "mute": track.mute,
                "solo": track.solo,
                "arm": track.arm,
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise
    
    def _safe_attr(self, obj, attr, default):
        """Read obj.<attr>, returning default if the property doesn't exist or
        isn't readable on this object (e.g. arm on a group track)."""
        try:
            return getattr(obj, attr)
        except (AttributeError, TypeError, RuntimeError):
            return default

    def _safe_color(self, obj):
        """A Live colour as a plain int (0xRRGGBB), or None where unreadable.

        Kept as the raw int on purpose: naming a colour is presentation, and
        presentation lives in server.py, which reloads without a Live restart.
        """
        try:
            return int(self._safe_attr(obj, "color", None))
        except (TypeError, ValueError):
            return None

    def _read_scenes(self):
        """Scene names, in order.

        Scenes are how an arrangement gets scaffolded (IDEA / GROOVE / BREAK /
        PEAK in the template) and nothing read them before, so checking they
        were named right was a by-eye job. Live returns "" for an unnamed
        scene and shows a number instead; that empty string is reported as-is
        rather than faked into a name.
        """
        scenes = []
        try:
            scene_list = list(self._song.scenes)
        except (AttributeError, TypeError, RuntimeError):
            return scenes

        for index, scene in enumerate(scene_list):
            scenes.append({
                "index": index,
                "name": str(self._safe_attr(scene, "name", "")),
                "color": self._safe_color(scene),
                "is_empty": bool(self._safe_attr(scene, "is_empty", True))
            })
        return scenes

    def _get_session_overview(self):
        """One batch read of the whole session: every track, its group
        membership, devices and filled clip slots, plus returns and master.

        This exists so a client never has to call get_track_info once per track
        just to find which index 'BASS' is at — one socket round-trip instead
        of N, and the answer is always current, so a stale hardcoded index map
        gets corrected on sight.
        """
        try:
            tracks = []

            # Materialise the track list once and keep a strong reference to it.
            # Live hands out a FRESH Python wrapper on every property access, so
            # id() is unstable across accesses — and worse, once a temporary
            # wrapper is collected its id gets recycled, so an id()-keyed map
            # returns confidently WRONG parents (a child reporting a sibling's
            # index) instead of no answer. Compare the objects instead.
            track_list = list(self._song.tracks)

            def index_of(other):
                if other is None:
                    return None
                for candidate_index, candidate in enumerate(track_list):
                    if candidate == other:
                        return candidate_index
                return None

            for index, track in enumerate(track_list):
                is_group = bool(self._safe_attr(track, "is_foldable", False))
                has_midi = bool(self._safe_attr(track, "has_midi_input", False))

                group = self._safe_attr(track, "group_track", None)
                group_index = index_of(group)

                clips = []
                for slot_index, slot in enumerate(track.clip_slots):
                    # Only filled slots — a wall of empty slots is exactly the
                    # noise this tool exists to avoid.
                    if not slot.has_clip:
                        continue
                    clip = slot.clip
                    clips.append({
                        "index": slot_index,
                        "name": clip.name,
                        "length": round(float(clip.length), 3),
                        "is_midi_clip": bool(self._safe_attr(clip, "is_midi_clip", has_midi))
                    })

                tracks.append({
                    "index": index,
                    "name": track.name,
                    "type": "group" if is_group else ("midi" if has_midi else "audio"),
                    "is_group": is_group,
                    "group_index": group_index,
                    "mute": bool(self._safe_attr(track, "mute", False)),
                    "solo": bool(self._safe_attr(track, "solo", False)),
                    "arm": bool(self._safe_attr(track, "arm", False)),
                    "color": self._safe_color(track),
                    "devices": [d.name for d in track.devices],
                    "clip_count": len(clips),
                    "clips": clips
                })

            returns = []
            for index, track in enumerate(self._song.return_tracks):
                returns.append({
                    "index": index,
                    "name": track.name,
                    "color": self._safe_color(track),
                    "devices": [d.name for d in track.devices]
                })

            master = self._song.master_track
            return {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(tracks),
                "return_track_count": len(returns),
                "tracks": tracks,
                "return_tracks": returns,
                "scenes": self._read_scenes(),
                "master_track": {
                    "name": master.name,
                    "devices": [d.name for d in master.devices]
                }
            }
        except Exception as e:
            self.log_message("Error getting session overview: " + str(e))
            raise

    # ── Device parameters ────────────────────────────────────────────────────
    # get_session_overview answers "what devices are on this track"; these
    # answer "and what are they set to". Deliberately thin: raw values plus the
    # display strings Live itself would show, no interpretation. All filtering,
    # formatting and comparison happens server-side, because server.py reloads
    # with an LM Studio restart while this file costs a full Live restart.

    def _safe_float(self, obj, attr, default):
        """Read obj.<attr> as a float, or default if it's missing/not numeric."""
        try:
            return float(getattr(obj, attr))
        except (AttributeError, TypeError, ValueError, RuntimeError):
            return default

    # ── Track addressing by name ─────────────────────────────────────────────
    # Indices are fragile: adding one group track to the template moved BASS
    # from 4 to 7 and silently invalidated every hardcoded map pointing at it.
    # Names survive reordering, so every track-addressed command accepts an
    # optional track_name that wins over track_index.
    #
    # The resolver NEVER guesses. Two tracks called PERC is exactly the
    # wrong-track write this is meant to prevent, so ambiguity raises with the
    # full list of names rather than picking the first hit — the same principle
    # as the overview renderer refusing to indent under a non-group.

    def _track_collection(self, track_type):
        """The Live track list a track_type refers to, plus a label for errors."""
        kind = str(track_type or "track").lower()
        if kind in ("master", "master_track"):
            return None, "master"
        if kind in ("return", "return_track", "returns"):
            return list(self._song.return_tracks), "return track"
        if kind in ("track", "tracks", "regular", ""):
            return list(self._song.tracks), "track"
        raise ValueError(
            "Unknown track_type '{0}' — use 'track', 'return' or 'master'".format(track_type))

    def _resolve_track_index(self, track_name, track_type="track"):
        """Turn a track NAME into its index.

        Exact (case-insensitive) match wins. Failing that, a UNIQUE substring
        match wins, so "reverb short" finds "A-Reverb Short". Anything ambiguous
        or unmatched raises with every available name attached, because the
        useful error here is the one that shows the caller what it could have
        said instead.

        Returns None for track_type "master" — the master has no index, and the
        caller's track_index is ignored for it anyway.
        """
        collection, label = self._track_collection(track_type)
        if collection is None:
            return None

        wanted = str(track_name).strip().lower()
        if not wanted:
            raise ValueError("track_name is empty")

        names = [str(self._safe_attr(track, "name", "")) for track in collection]
        catalogue = ", ".join('{0}:"{1}"'.format(i, n) for i, n in enumerate(names))

        exact = [i for i, n in enumerate(names) if n.strip().lower() == wanted]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError(
                "Track name '{0}' is ambiguous — {1}s {2} all have that name. "
                "Rename one, or use track_index.".format(
                    track_name, label, ", ".join(str(i) for i in exact)))

        partial = [i for i, n in enumerate(names) if wanted in n.strip().lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise ValueError(
                "Track name '{0}' matches {1} {2}s: {3}. Be more specific.".format(
                    track_name, len(partial), label,
                    ", ".join('{0}:"{1}"'.format(i, names[i]) for i in partial)))

        raise ValueError(
            "No {0} named '{1}'. The set has: {2}".format(label, track_name, catalogue))

    def _resolve_track(self, track_index, track_type):
        """Address a regular track, a return track or the master.

        Returns (track, kind, index, name). The audit this was built for has to
        reach returns and the master, and Live keeps those in three separate
        collections, so the caller has to say which one it means.
        """
        kind = str(track_type or "track").lower()

        if kind in ("master", "master_track"):
            master = self._song.master_track
            return master, "master", None, master.name

        if kind in ("return", "return_track", "returns"):
            returns = list(self._song.return_tracks)
            if track_index < 0 or track_index >= len(returns):
                raise IndexError(
                    "Return track index {0} out of range — the set has {1} return track(s)".format(
                        track_index, len(returns)))
            return returns[track_index], "return", track_index, returns[track_index].name

        if kind in ("track", "tracks", "regular", ""):
            tracks = list(self._song.tracks)
            if track_index < 0 or track_index >= len(tracks):
                raise IndexError(
                    "Track index {0} out of range — the set has {1} track(s)".format(
                        track_index, len(tracks)))
            return tracks[track_index], "track", track_index, tracks[track_index].name

        raise ValueError(
            "Unknown track_type '{0}' — use 'track', 'return' or 'master'".format(track_type))

    def _read_parameter(self, param, index):
        """One DeviceParameter as plain data.

        display_value comes from str_for_value(value), which is what Live shows
        on screen ("120 Hz", "-6.0 dB", "Soft") — the raw value alone is
        meaningless for anything non-linear. value is kept alongside it for
        machine comparison, and min/max/is_quantized say what that value means.
        """
        value = self._safe_float(param, "value", 0.0)

        display = None
        try:
            display = str(param.str_for_value(param.value))
        except (AttributeError, TypeError, ValueError, RuntimeError):
            # Some plugin parameters don't implement it; the numeric value and
            # the range still tell the caller something.
            display = None

        return {
            "index": index,
            "name": str(self._safe_attr(param, "name", "")),
            "original_name": str(self._safe_attr(param, "original_name", "")),
            # round() kills float noise like 0.30000000000000004
            "value": round(value, 6),
            "display_value": display,
            "min": round(self._safe_float(param, "min", 0.0), 6),
            "max": round(self._safe_float(param, "max", 0.0), 6),
            "is_quantized": bool(self._safe_attr(param, "is_quantized", False))
        }

    def _read_mixer(self, track):
        """The mixer strip: fader, pan and sends.

        These are DeviceParameters like any other, but they hang off
        track.mixer_device instead of track.devices — so a devices-only read
        silently misses the two things a mix audit asks first: how loud is this
        track, and how much of it is going to the reverb.
        """
        mixer = self._safe_attr(track, "mixer_device", None)
        if mixer is None:
            return None

        entries = []
        for index, attr in enumerate(("volume", "panning")):
            param = self._safe_attr(mixer, attr, None)
            if param is not None:
                entries.append(self._read_parameter(param, index))

        try:
            send_list = list(mixer.sends)
        except (AttributeError, TypeError, RuntimeError):
            # Master has no sends; a return track only has sends to returns
            # after it. Both are normal, not errors.
            send_list = []

        sends = []
        for index, send in enumerate(send_list):
            entry = self._read_parameter(send, index)
            entry["return_index"] = index
            sends.append(entry)

        return {
            "parameters": entries,
            "send_count": len(sends),
            "sends": sends
        }

    def _read_drum_pads(self, device):
        """The note → pad-name map of a Drum Rack.

        This is the piece that makes an AI able to WRITE a drum part: knowing a
        rack has 10 pads is useless, knowing "Bongo" answers to note 39 is the
        whole game. Deliberately NOT behind include_chains — the map is small
        and high-value, whereas the devices inside each pad are neither.

        Live exposes all 128 pads whether or not anything is loaded, so pads
        with no chain are skipped; an empty pad is not a playable note.
        """
        pads = []
        try:
            pad_list = list(device.drum_pads)
        except (AttributeError, TypeError, RuntimeError):
            return pads

        for pad in pad_list:
            try:
                if not list(self._safe_attr(pad, "chains", [])):
                    continue
            except (TypeError, RuntimeError):
                continue
            pads.append({
                "note": int(self._safe_float(pad, "note", -1)),
                "name": str(self._safe_attr(pad, "name", "")),
                "mute": bool(self._safe_attr(pad, "mute", False)),
                "solo": bool(self._safe_attr(pad, "solo", False))
            })

        pads.sort(key=lambda entry: entry["note"])
        return pads

    def _read_device(self, device, index, include_chains, depth):
        """One device: its identity, on/off state, and its parameters."""
        try:
            all_params = list(device.parameters)
        except (AttributeError, TypeError, RuntimeError):
            all_params = []

        parameter_count = len(all_params)
        truncated = parameter_count > MAX_PARAMETERS_PER_DEVICE
        if truncated:
            all_params = all_params[:MAX_PARAMETERS_PER_DEVICE]

        parameters = [self._read_parameter(param, p_index)
                      for p_index, param in enumerate(all_params)]

        # Device.is_active is the honest answer — it's False both when the
        # device is switched off AND when it sits in a deactivated rack chain.
        # Where it isn't exposed, fall back to the standard 'Device On' switch,
        # which every stock device carries as its first parameter.
        is_active = self._safe_attr(device, "is_active", None)
        if is_active is None:
            for param in parameters:
                if param["name"] == "Device On":
                    is_active = param["value"] > 0.5
                    break

        can_have_chains = bool(self._safe_attr(device, "can_have_chains", False))
        chain_list = []
        if can_have_chains:
            try:
                chain_list = list(device.chains)
            except (AttributeError, TypeError, RuntimeError):
                chain_list = []

        can_have_drum_pads = bool(self._safe_attr(device, "can_have_drum_pads", False))
        drum_pads = self._read_drum_pads(device) if can_have_drum_pads else []

        info = {
            "index": index,
            "name": str(self._safe_attr(device, "name", "")),
            "class_name": str(self._safe_attr(device, "class_name", "")),
            "type": self._get_device_type(device),
            "is_active": None if is_active is None else bool(is_active),
            "can_have_chains": can_have_chains,
            "can_have_drum_pads": can_have_drum_pads,
            "chain_count": len(chain_list),
            "drum_pad_count": len(drum_pads),
            "drum_pads": drum_pads,
            "parameter_count": parameter_count,
            "parameters_returned": len(parameters),
            "truncated": truncated,
            "parameters": parameters
        }

        if chain_list and include_chains:
            if depth < MAX_CHAIN_DEPTH:
                chains = []
                for chain_index, chain in enumerate(chain_list):
                    try:
                        chain_devices = list(chain.devices)
                    except (AttributeError, TypeError, RuntimeError):
                        chain_devices = []
                    chains.append({
                        "index": chain_index,
                        "name": str(self._safe_attr(chain, "name", "")),
                        "devices": [self._read_device(d, d_index, include_chains, depth + 1)
                                    for d_index, d in enumerate(chain_devices)]
                    })
                info["chains"] = chains
            else:
                # Say so rather than silently returning a rack as a leaf.
                info["chains_omitted"] = "depth limit ({0})".format(MAX_CHAIN_DEPTH)

        return info

    def _get_device_parameters(self, track_index, device_index, track_type, include_chains):
        """Read device settings off a track, a return track or the master.

        device_index < 0 reads EVERY device on the track in one call — the same
        N-round-trips problem get_session_overview was built to kill, applied to
        an audit that has to walk a whole set.

        Rack chains are NOT expanded unless include_chains is set: a Drum Rack
        with sixteen loaded pads would otherwise bury the track's own devices.
        chain_count is always reported so the caller knows there's more inside.

        A whole-track read also carries the mixer strip (fader, pan, sends).
        Asking for one device doesn't — the mixer isn't part of that answer.
        """
        try:
            track, kind, resolved_index, track_name = self._resolve_track(
                track_index, track_type)

            device_list = list(track.devices)
            whole_track = device_index is None or device_index < 0

            if whole_track:
                selected = list(enumerate(device_list))
            else:
                if device_index >= len(device_list):
                    raise IndexError(
                        "Device index {0} out of range — {1} '{2}' has {3} device(s)".format(
                            device_index, kind, track_name, len(device_list)))
                selected = [(device_index, device_list[device_index])]

            devices = [self._read_device(device, index, include_chains, 0)
                       for index, device in selected]

            result = {
                "track_type": kind,
                "track_index": resolved_index,
                "track_name": track_name,
                "device_count": len(device_list),
                "devices_returned": len(devices),
                "devices": devices
            }
            if whole_track:
                result["mixer"] = self._read_mixer(track)
            return result
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise
    
    
    def _set_track_name(self, track_index, name):
        """Set the name of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise
    
    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _create_audio_clip(self, track_index, clip_index, path):
        """Create an audio clip in the specified audio track clip slot by importing a file.

        Requires Ableton Live 12.0.5 or newer (the underlying
        ClipSlot.create_audio_clip Live API was introduced in 12.0.5 — it is
        not available in earlier 12.0.x releases).
        """
        try:
            if not path:
                raise ValueError("Audio file path is required")

            if not os.path.isabs(path):
                raise ValueError("Audio file path must be absolute (got: %s)" % path)

            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            # Must be an audio track. Audio tracks expose audio input; MIDI
            # tracks don't. Reject MIDI / return tracks up front so the caller
            # gets a clear error instead of a Live API exception.
            if getattr(track, "has_midi_input", False) or not getattr(track, "has_audio_input", True):
                raise ValueError("Track %d is not an audio track" % track_index)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")

            if not hasattr(clip_slot, "create_audio_clip"):
                raise Exception(
                    "ClipSlot.create_audio_clip is unavailable in this Ableton Live "
                    "version. Requires Live 12.0.5 or newer."
                )

            clip_slot.create_audio_clip(path)

            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length,
                "is_audio_clip": clip_slot.clip.is_audio_clip
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio clip: " + str(e))
            raise

    def _get_clip_notes(self, track_index, clip_index):
        """Read the MIDI notes out of a Session clip.

        Uses clip.get_notes_extended() (Live 11+), NOT the deprecated
        get_notes(). Note dicts mirror the _add_notes_to_clip write format
        exactly — pitch, start_time, duration, velocity, mute — so a pattern
        can be read, edited and written straight back.

        Live's MidiNote also carries probability / velocity_deviation /
        release_velocity, but the write path (set_notes) cannot set them, so
        surfacing them here would produce notes that don't round-trip. Left out
        deliberately.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception("No clip in slot {0} on track {1} ({2})".format(
                    clip_index, track_index, track.name))

            clip = clip_slot.clip

            if not clip.is_midi_clip:
                raise Exception(
                    "Clip '{0}' is an audio clip — it has no MIDI notes".format(clip.name))

            if not hasattr(clip, "get_notes_extended"):
                raise Exception(
                    "Clip.get_notes_extended is unavailable in this Ableton Live "
                    "version. Requires Live 11 or newer."
                )

            # Read a window nothing can hide behind. Deriving it from
            # length/end_marker/loop_end looks smarter but fails at exactly the
            # case it's meant to catch: when the loop brace is dragged shorter
            # than the material, all three markers agree on the SHORT value and
            # the notes past them go silently missing. get_notes_extended just
            # range-filters, so an absurd span costs nothing.
            NOTE_TIME_SPAN = 1000000.0  # beats — ~138 hours at 120 BPM

            # Signature: get_notes_extended(from_pitch, pitch_span, from_time, time_span).
            # NOTE the argument order differs from the old get_notes().
            notes = []
            for note in clip.get_notes_extended(0, 128, 0.0, NOTE_TIME_SPAN):
                notes.append({
                    "pitch": int(note.pitch),
                    # round() kills float noise like 2.0000000000000004
                    "start_time": round(float(note.start_time), 6),
                    "duration": round(float(note.duration), 6),
                    "velocity": round(float(note.velocity), 3),
                    "mute": bool(note.mute)
                })

            # Live's own note order isn't guaranteed; sort so the same clip
            # always reads back identically.
            notes.sort(key=lambda n: (n["start_time"], n["pitch"]))

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "clip_length": float(clip.length),
                "signature_numerator": clip.signature_numerator,
                "signature_denominator": clip.signature_denominator,
                "note_count": len(notes),
                "notes": notes
            }
        except Exception as e:
            self.log_message("Error getting clip notes: " + str(e))
            raise

    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise
    
    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise
    
    
    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise
    
    # ── Arrangement view implementations ──────────────────────────────────────

    def _switch_to_arrangement_view(self):
        """Switch Ableton's main window to the Arrangement view"""
        try:
            self.application().view.show_view("Arranger")
            return {"view": "Arranger"}
        except Exception as e:
            self.log_message("Error switching to arrangement view: " + str(e))
            raise

    def _set_current_song_time(self, time_val):
        """Move the arrangement playhead to a position in beats"""
        try:
            self._song.current_song_time = float(time_val)
            return {"current_song_time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting current song time: " + str(e))
            raise

    def _get_arrangement_clips(self, track_index):
        """Return all clips placed in the Arrangement timeline for a track.

        Each clip dict contains:
          name, start_time, end_time, length, color,
          is_midi_clip, is_audio_clip, is_playing
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            clips = []

            # track.arrangement_clips is available in Live 11 / 12
            for clip in track.arrangement_clips:
                clips.append({
                    "name": clip.name,
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                    "length": clip.length,
                    "color": clip.color,
                    "is_midi_clip": clip.is_midi_clip,
                    "is_audio_clip": clip.is_audio_clip,
                    "is_playing": clip.is_playing
                })

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_count": len(clips),
                "clips": clips
            }
        except Exception as e:
            self.log_message("Error getting arrangement clips: " + str(e))
            raise

    def _duplicate_session_clip_to_arrangement(self, track_index, clip_index, destination_time):
        """Copy a Session-view clip into the Arrangement timeline.

        Uses the real Live API:
          track.duplicate_clip_to_arrangement(clip, destination_time)

        Available in Live 11 / 12.  destination_time is in beats from the
        start of the arrangement.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception(
                    "No clip in slot " + str(clip_index) +
                    " on track " + str(track_index)
                )

            clip = clip_slot.clip

            # Duplicate to arrangement at the requested beat position
            track.duplicate_clip_to_arrangement(clip, float(destination_time))

            return {
                "success": True,
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip.name,
                "destination_time": destination_time
            }
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise

    # ── Browser implementations ───────────────────────────────────────────────

    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = path.split("/")
                
                # Determine the root based on the first part
                current_item = None
                if path_parts[0].lower() == "instruments":
                    current_item = app.browser.instruments
                elif path_parts[0].lower() == "sounds":
                    current_item = app.browser.sounds
                elif path_parts[0].lower() == "drums":
                    current_item = app.browser.drums
                elif path_parts[0].lower() == "audio_effects":
                    current_item = app.browser.audio_effects
                elif path_parts[0].lower() == "midi_effects":
                    current_item = app.browser.midi_effects
                else:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_browser_item(self, track_index, item_uri):
        """Load a browser item onto a track by its URI"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            
            # Load the item
            app.browser.load_item(item)
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    # Substring markers that point a URI at a likely root. If no marker
    # matches we fall back to the default order, so this is purely an
    # optimisation — never a correctness change.
    _URI_ROOT_HINTS = (
        ('plugins',       ('vst:', 'vst3:', 'au:', 'query:plugins', 'plugin#')),
        ('max_for_live',  ('max for live', 'maxforlive', 'm4l', 'query:max')),
        ('user_library',  ('user library', 'userlibrary', 'query:user library', 'query:user-library')),
        ('packs',         ('query:packs', '/packs/')),
        ('samples',       ('query:samples', 'sample:', '/samples/')),
        ('drums',         ('query:drums', '/drums/')),
        ('instruments',   ('query:instruments', '/instruments/')),
        ('sounds',        ('query:sounds', '/sounds/')),
        ('audio_effects', ('query:audio effects', 'audioeffects', '/audio_effects/')),
        ('midi_effects',  ('query:midi effects', 'midieffects', '/midi_effects/')),
    )

    def _order_roots_by_uri(self, roots, uri):
        """Reorder ``roots`` so the URI's likely root is walked first."""
        if not isinstance(uri, (bytes, str)) or not uri:
            return roots
        lowered = uri.lower()
        for attr, markers in self._URI_ROOT_HINTS:
            if any(m in lowered for m in markers):
                head = [(a, r) for (a, r) in roots if a == attr]
                tail = [(a, r) for (a, r) in roots if a != attr]
                return head + tail
        return roots

    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI.

        Top-level lookups are memoised on ``self._uri_cache`` so repeated
        loads of the same URI don't re-walk the entire browser tree.
        """
        if current_depth == 0:
            cache = getattr(self, '_uri_cache', None)
            if cache is None:
                self._uri_cache = cache = {}
            if uri in cache:
                return cache[uri]
            result = self._walk_browser_for_uri(browser_or_item, uri, max_depth, 0)
            if result is not None:
                cache[uri] = result
            return result
        return self._walk_browser_for_uri(browser_or_item, uri, max_depth, current_depth)

    def _walk_browser_for_uri(self, browser_or_item, uri, max_depth, current_depth):
        """Recursive walk used by :py:meth:`_find_browser_item_by_uri`."""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item

            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None

            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                roots = [
                    ('instruments', browser_or_item.instruments),
                    ('sounds', browser_or_item.sounds),
                    ('drums', browser_or_item.drums),
                    ('audio_effects', browser_or_item.audio_effects),
                    ('midi_effects', browser_or_item.midi_effects),
                ]
                for extra_attr in ('plugins', 'max_for_live', 'user_library', 'packs', 'samples'):
                    if hasattr(browser_or_item, extra_attr):
                        try:
                            roots.append((extra_attr, getattr(browser_or_item, extra_attr)))
                        except (AttributeError, RuntimeError) as e:
                            self.log_message("Could not access browser.{0}: {1}".format(extra_attr, str(e)))

                for _attr, category in self._order_roots_by_uri(roots, uri):
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item

                return None

            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item

            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Helper methods
    
    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"
    
    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # Helper function to process a browser item and its children
            def process_item(item, depth=0):
                if not item:
                    return None
                
                result = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": hasattr(item, 'children') and bool(item.children),
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }
                
                
                return result
            
            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = path.split("/")
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0].lower()
            current_item = None
            
            # Check standard categories first
            if root_category == "instruments" and hasattr(app.browser, 'instruments'):
                current_item = app.browser.instruments
            elif root_category == "sounds" and hasattr(app.browser, 'sounds'):
                current_item = app.browser.sounds
            elif root_category == "drums" and hasattr(app.browser, 'drums'):
                current_item = app.browser.drums
            elif root_category == "audio_effects" and hasattr(app.browser, 'audio_effects'):
                current_item = app.browser.audio_effects
            elif root_category == "midi_effects" and hasattr(app.browser, 'midi_effects'):
                current_item = app.browser.midi_effects
            else:
                # Try to find the category in other browser attributes
                found = False
                for attr in browser_attrs:
                    if attr.lower() == root_category:
                        try:
                            current_item = getattr(app.browser, attr)
                            found = True
                            break
                        except Exception as e:
                            self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
                
                if not found:
                    # If we still haven't found the category, return available categories
                    return {
                        "path": path,
                        "error": "Unknown or unavailable category: {0}".format(root_category),
                        "available_categories": browser_attrs,
                        "items": []
                    }
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)
            
            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            
            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
