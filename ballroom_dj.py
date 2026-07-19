import itertools
import os
import re
import random
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from collections import deque, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from pprint import pprint  # noqa: F401

import numpy as np
import pygame
import pyloudnorm as pyln
from colorama import just_fix_windows_console, Fore, Style
from mutagen import File as MutagenFile
from pydub import AudioSegment
from pydub.silence import detect_nonsilent


# -------------------------------------------
# CONFIG
# -------------------------------------------

os.environ["SDL_AUDIODRIVER"] = "pulse"  # Use PulseAudio backend (Linux audio system)

# Bases for all paths below
PROJECT_DIR = Path(__file__).resolve().parent  # Project directory
HOME = Path.home()  # User home directory

# Text file defining dance categories + songs
SONG_LIST_FILE = HOME / "Music/Tanzmusik.txt"

MUSIC_ROOTS = [  # Folders where audio files are searched recursively
    HOME / "Music/Arni",
    HOME / "Music/NewPipe",
    HOME / "Music/Seal",
]

# Played at the very end (None to disable)
FINAL_SONG = "Teddy Swims - Lose Control (Live)"

# Category in SONG_LIST_FILE whose songs are NOT auto-scheduled, but can be
# manually queued via the "Queue Song" button.  (If a title from this category
# also appears under a real dance, it can still show up there naturally.)
ON_DEMAND_SONGS_CATEGORY = "On-Demand Songs"

MIN_SONG_PAUSE = defaultdict(
    # Default: how many songs must pass before a dance can repeat.
    # Should be smaller than the number of available song types
    lambda: 7,
    # Custom settings: Specify which songs should show up more/less frequently than the default set above ↑
    {
        "Paso Doble": 10,  # Higher = rarer
        "Tango": 8,
        "West Coast Swing": 2,  # Lower = more common
        "Wiener Walzer": 9,
    },
)
SONG_CATEGORY_REPEATS = defaultdict(
    # Default: play 1 song per dance category before switching to the next dance category
    # (i.e. after one ChaChaCha is played, a non-ChaChaCha-song is chosen next)
    lambda: 1,
    # Custom settings: Specify in which song categories multiple songs should be played right after each other.
    {
        # "Tango Argentino": 4,
        "West Coast Swing": 2,
    },
)

TARGET_LOUDNESS = -14.0  # Normalize all songs to this loudness (LUFS)

# How many upcoming songs the background worker keeps fully prepared and ready.
PREPARE_AHEAD = 10

# Directory where prepared (normalized/trimmed) wav files are written.
PREPARED_DIR = Path(tempfile.mkdtemp(prefix="dance_player_"))

TRIM_SILENCE = True  # Automatically remove silence at start/end of songs
SILENCE_THRESHOLD_DB = -50  # What counts as silence (lower = stricter)
SILENCE_CHUNK_MS = 10  # Analysis step size (smaller = more precise)
SILENCE_MIN_LEN_MS = 500  # Minimum silence length to be trimmed

HISTORY_LENGTH = 12  # Number of recent songs shown in UI

# Stores played songs to avoid repeats across runs
PLAYED_LOG_FILE = PROJECT_DIR / ".played_songs.log"

# -------------------------------------------
# PRETTY CONSOLE
# -------------------------------------------

just_fix_windows_console()


def success(text: str) -> str:
    return f"{Fore.GREEN}{text}{Style.RESET_ALL}"


def warning(text: str) -> str:
    return f"{Fore.YELLOW}{text}{Style.RESET_ALL}"


def error(text: str) -> str:
    return f"{Fore.RED}{text}{Style.RESET_ALL}"


def bold(text: str) -> str:
    return f"{Style.BRIGHT}{text}{Style.NORMAL}"


def divider() -> None:
    print("─" * 48)


# -------------------------------------------
# SILENCE TRIMMING
# -------------------------------------------


def trim_silence(audio: AudioSegment) -> AudioSegment:
    if not TRIM_SILENCE:
        return audio

    ranges = detect_nonsilent(
        audio,
        min_silence_len=SILENCE_MIN_LEN_MS,
        silence_thresh=SILENCE_THRESHOLD_DB,
        seek_step=SILENCE_CHUNK_MS,
    )

    if not ranges:
        return audio

    start = ranges[0][0]
    end = ranges[-1][1]

    if start >= end:
        return audio

    return audio[start:end]


# -------------------------------------------
# LOAD SONG LIST
# -------------------------------------------


def load_song_list(path: str) -> dict[str, list[str]]:
    categories = {}
    current = None

    with open(path, encoding="utf8") as f:
        for line in f:
            line = line.strip()

            if line == "---":
                break

            if not line:
                current = None
                continue

            if current is None:
                current = line
                categories[current] = []
            else:
                categories[current].append(line)

    return categories


# -------------------------------------------
# AUDIO FILE SCAN
# -------------------------------------------

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac"}


def normalize_unicode(s: str) -> str:
    if not s:
        return ""
    return unicodedata.normalize("NFKC", s).casefold()


def clean_for_matching(text: str) -> str:
    if not text:
        return ""

    text = str(text).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


class SongEntry:
    @staticmethod
    def get_meta_tag(meta: MutagenFile, tag: str, fallback: str) -> str:
        tag_value = meta.tags.get(tag, "")

        if isinstance(tag_value, (list, tuple)) and tag_value:
            return tag_value[0]
        elif tag_value:
            return tag_value
        return fallback

    def __init__(self, path: Path) -> None:
        self.path = path
        self.artist = ""  # Define fallback values
        self.album = ""
        self.title = path.stem

        try:
            meta = MutagenFile(path, easy=True)

            if meta and meta.tags:
                self.artist = self.get_meta_tag(meta, "artist", self.artist)
                self.album = self.get_meta_tag(meta, "album", self.album)
                self.title = self.get_meta_tag(meta, "title", self.title)

        except Exception:
            pass

        if self.artist:
            self.full = f"{self.artist} - {self.title}"
        else:
            self.full = self.title

        combined = " ".join(
            [
                self.artist,
                self.album,
                self.title,
                self.path.name,
            ]
        )
        cleaned = clean_for_matching(combined)

        self.search_text = normalize_unicode(cleaned)
        self.token_set = set(self.search_text.split())


class SongMatcher:
    def __init__(self, song_names: list[str], audio_files: list[Path]) -> None:
        self.entries = [SongEntry(f) for f in audio_files]
        self.mapping = {}

        unique_songs = set(song_names)
        for song in unique_songs:
            print(f'\nMatching "{bold(song)}"')

            best_entry, best_count, best_prop = self._find_best_match(song)

            if best_entry and best_count >= 1:
                self.mapping[song] = best_entry.path

                print(
                    success(
                        f"✓ {bold(best_entry.full)} "
                        f"({best_entry.path.name}) "
                        f"[{best_count} tokens matched, {best_prop:.0%} of query]"
                    )
                )
            else:
                print(
                    error(
                        f"✗ Weak/No match for '{song}' (best was {best_count} tokens)"
                    )
                )

                if best_entry:
                    print(
                        error(f"   Closest: {best_entry.full} ({best_entry.path.name})")
                    )

                self.mapping[song] = None

        total_entries = len(unique_songs)
        successful_matches = sum(p is not None for p in self.mapping.values())
        failed_matches = total_entries - successful_matches
        print("\nMatching complete!")
        print(success(f"✓ {successful_matches}/{total_entries} successful"))
        if failed_matches > 0:
            print(error(f"✗ {failed_matches}/{total_entries} failed"))

    def _find_best_match(self, query: str) -> tuple[SongEntry | None, int, float]:
        query_clean = clean_for_matching(query)
        query_clean = normalize_unicode(query_clean)

        query_tokens = [t for t in query_clean.split()]

        if not query_tokens:
            return None, 0, 0.0

        best_entries = []
        best_count = -1
        best_prop = 0.0

        for entry in self.entries:
            matched = sum(1 for token in query_tokens if token in entry.token_set)

            if matched == 0:
                continue

            prop = matched / len(query_tokens)

            if matched > best_count or (matched == best_count and prop > best_prop):
                best_entries = [entry]
                best_count = matched
                best_prop = prop

            elif matched == best_count and prop == best_prop:
                best_entries.append(entry)

        if not best_entries:
            return None, 0, 0.0

        if len(best_entries) > 1:
            print(warning("⚠ Multiple equally good matches:"))
            for e in best_entries[:5]:
                print(warning(f"   • {e.path.name}"))
                print(warning(f'       Search Text: "{e.search_text}"'))

        best_entry = min(best_entries, key=lambda e: len(e.search_text))

        return best_entry, best_count, best_prop

    def get(self, song: str) -> Path | None:
        return self.mapping.get(song)


def scan_audio_files(roots: list[str]) -> list[Path]:
    files = []

    for root in roots:
        root_path = Path(root)

        if not root_path.exists():
            print(error(f"Error: directory not found -> {root}"))
            continue

        for p in root_path.rglob("*"):
            if p.suffix.lower() in AUDIO_EXT:
                files.append(p)

    print(f"Found {len(files)} audio files")
    return files


# -------------------------------------------
# NORMALIZE LOUDNESS
# -------------------------------------------


def normalize_audio(path: Path) -> tuple[AudioSegment, float, float]:
    audio = AudioSegment.from_file(path)

    samples = np.array(audio.get_array_of_samples()).astype(np.float32)

    if audio.sample_width == 2:
        samples /= 32768.0
    elif audio.sample_width == 4:
        samples /= 2147483648.0

    if audio.channels == 2:
        samples = samples.reshape((-1, 2))

    meter = pyln.Meter(audio.frame_rate)
    loudness = meter.integrated_loudness(samples)

    if np.isnan(loudness) or np.isinf(loudness):
        print(warning(f"Warning: bad loudness for {path.name} → using original"))
        return audio, loudness, 0.0

    gain_db = TARGET_LOUDNESS - loudness

    return audio.apply_gain(gain_db), loudness, gain_db


# -------------------------------------------
# PLAYLIST ITEM
# -------------------------------------------

# Global, thread-safe (CPython) unique id source for playlist items.  Guarantees
# that every item (including ones inserted at runtime) gets a distinct temp-file
# name, so no two prepared wavs ever collide.
_uid_counter = itertools.count()


@dataclass
class PlaylistItem:
    dance: str
    song: str
    path: Path | None
    uid: int = field(default_factory=lambda: next(_uid_counter))

    # --- preparation state (written by the PreparationWorker) ---
    prepared: bool = False
    prepared_file: Path | None = None
    duration_ms: int = 0
    loudness: float | None = None
    gain_db: float = 0.0

    # --- playback / role state ---
    played: bool = False
    skip: bool = False  # set when the item can't be prepared and must be stepped over
    is_custom: bool = False  # queued via the "Queue Song" button
    is_final: bool = False  # last song; playback stops after it

    def display(self) -> str:
        return f"{self.dance} — {self.song}"


# -------------------------------------------
# DANCE SELECTION  (probability logic — unchanged)
# -------------------------------------------


class DanceSelector:
    def __init__(self, categories: dict[str, list[str]]) -> None:
        default_pause = MIN_SONG_PAUSE.default_factory()
        self.songs_passed_since = defaultdict(lambda: default_pause + 1)
        self.history = deque(maxlen=HISTORY_LENGTH)

    def _build_weights(self, available_categories: list[str]) -> list[float]:
        weights = []

        for category in available_categories:
            min_pause = MIN_SONG_PAUSE[category]
            songs_passed_since = self.songs_passed_since[category]

            # No negative weights allowed!
            weight = max(songs_passed_since - min_pause, 0)
            weights.append(weight)

        return weights

    def _weighted_pick(self, categories: list[str], weights: list[float]) -> str:
        if sum(weights) == 0:
            # choose dance closest to its minimum pause
            return max(
                categories, key=lambda c: self.songs_passed_since[c] / MIN_SONG_PAUSE[c]
            )

        return random.choices(categories, weights=weights, k=1)[0]

    def peek(self, available_categories: list[str]) -> str | None:
        if not available_categories:
            return None

        weights = self._build_weights(available_categories)
        choice = self._weighted_pick(available_categories, weights)

        return choice

    def commit(self, choice: str | None) -> None:
        if choice is None:
            return

        self.history.append(choice)

        for category in self.songs_passed_since:
            self.songs_passed_since[category] += 1

        self.songs_passed_since[choice] = 0


# -------------------------------------------
# PLAYLIST BUILDER
# -------------------------------------------


class PlaylistBuilder:
    """Generates the FULL planned playlist up front.

    The On-Demand-Songs category is excluded from auto-scheduling.
    `already_played` (loaded from the log) is used only to seed a local `used`
    set, so the caller's persistent played-log is never touched here."""

    def __init__(
        self,
        categories: dict[str, list[str]],
        matcher: SongMatcher,
        selector: DanceSelector,
        already_played: set[str],
    ) -> None:
        self.categories = categories
        self.matcher = matcher
        self.selector = selector
        self.already_played = already_played

    def _schedulable_dances(self, used: set[str]) -> list[str]:
        """Dances (excluding the on-demand category) that still have an unused song."""
        return [
            d
            for d in self.categories
            if d != ON_DEMAND_SONGS_CATEGORY
            and any(s not in used and s != FINAL_SONG for s in self.categories[d])
        ]

    def _take_song(self, dance: str, used: set[str]) -> str | None:
        """Pick a random unused song of `dance` that actually matches a file.
        Consumes (marks used) any songs it tries, so unmatched ones don't loop."""
        while True:
            candidates = [
                s for s in self.categories[dance] if s not in used and s != FINAL_SONG
            ]
            if not candidates:
                return None

            song = random.choice(candidates)
            used.add(song)

            if self.matcher.get(song) is None:
                print(error(f"Skipping '{song}' (no matched audio file)"))
                continue

            return song

    def build(self) -> tuple[list[PlaylistItem], list[str]]:
        used = set(self.already_played)
        playlist: list[PlaylistItem] = []

        while True:
            available = self._schedulable_dances(used)
            if not available:
                break

            dance = self.selector.peek(available)
            if dance is None:  # defensive fallback; peek shouldn't return None here
                dance = random.choice(available)
            self.selector.commit(dance)

            repeat = SONG_CATEGORY_REPEATS[dance]
            for _ in range(repeat):
                song = self._take_song(dance, used)
                if song is None:
                    break
                playlist.append(
                    PlaylistItem(dance=dance, song=song, path=self.matcher.get(song))
                )

        # Final song lives at the very end of the plan by default, so the normal
        # "play everything, then final, then close" flow needs no special-casing.
        if FINAL_SONG:
            final_path = self.matcher.get(FINAL_SONG)
            if final_path is not None:
                playlist.append(
                    PlaylistItem(
                        dance="Final Dance",
                        song=FINAL_SONG,
                        path=final_path,
                        is_final=True,
                    )
                )

        planned_dances = {it.dance for it in playlist if not it.is_final}
        empty_categories = [
            c
            for c in self.categories
            if c != ON_DEMAND_SONGS_CATEGORY and c not in planned_dances
        ]

        print(success(f"\n✓ Planned playlist: {len(playlist)} songs"))
        return playlist, empty_categories


# -------------------------------------------
# PREPARATION WORKER
# -------------------------------------------


class PreparationWorker(threading.Thread):
    """Background thread that keeps items [current_index .. current_index+PREPARE_AHEAD]
    normalized, trimmed and exported to disk, so playback never has to wait."""

    def __init__(
        self, controller: "DanceController", prepare_ahead: int = PREPARE_AHEAD
    ) -> None:
        super().__init__(daemon=True)
        self.controller = controller
        self.prepare_ahead = prepare_ahead
        self.wake = threading.Event()
        self._stop = False

    def notify(self) -> None:
        """Wake the worker (call after mutating the playlist / advancing)."""
        self.wake.set()

    def stop(self) -> None:
        self._stop = True
        self.wake.set()

    def run(self) -> None:
        while not self._stop:
            item = self._next_to_prepare()
            if item is None:
                self.wake.wait(timeout=0.3)
                self.wake.clear()
                continue
            self._prepare(item)

    def _next_to_prepare(self) -> PlaylistItem | None:
        with self.controller.lock:
            idx = self.controller.current_index
            playlist = self.controller.playlist
            window_end = min(len(playlist), idx + self.prepare_ahead + 1)
            for i in range(idx, window_end):
                it = playlist[i]
                if not it.prepared and not it.skip:
                    return it
        return None

    def _prepare(self, item: PlaylistItem) -> None:
        # Heavy work happens OUTSIDE the lock.  path/uid are stable for this item.
        try:
            if item.path is None:
                raise FileNotFoundError(f"no matched audio for '{item.song}'")

            audio, loudness, gain_db = normalize_audio(item.path)
            audio = trim_silence(audio)
            duration_ms = len(audio)

            out = PREPARED_DIR / f"prepared_{item.uid}.wav"
            audio.export(out, format="wav")

            item.duration_ms = duration_ms
            item.loudness = loudness
            item.gain_db = gain_db
            item.prepared_file = out
            item.prepared = True  # set LAST: signals "fully ready" to the player
            print(success(f"  ⚙ Prepared: {item.song}"))

        except Exception as exc:
            self._handle_failure(item, exc)

    def _handle_failure(self, item: PlaylistItem, exc: Exception) -> None:
        """A file was unreadable / unmatched.  Try to pull the last still-unprepared
        song of the same dance forward into this slot; otherwise flag it to be skipped."""
        print(error(f"⚠ Could not prepare '{item.song}': {exc}"))
        with self.controller.lock:
            playlist = self.controller.playlist
            try:
                idx = playlist.index(item)
            except ValueError:
                return  # item was removed meanwhile (e.g. final-song truncation)

            if idx <= self.controller.current_index:
                # Too late to reshuffle around the current item — just step over it.
                item.skip = True
                item.prepared = True
                print(warning("↪ Marked current slot to be skipped"))
                return

            donor_idx = None
            for j in range(len(playlist) - 1, idx, -1):
                c = playlist[j]
                if (
                    c.dance == item.dance
                    and not c.played
                    and not c.prepared
                    and not c.skip
                    and not c.is_custom
                    and not c.is_final
                ):
                    donor_idx = j
                    break

            if donor_idx is not None:
                donor = playlist[donor_idx]
                item.song = donor.song
                item.path = donor.path
                item.uid = next(_uid_counter)
                item.prepared = False
                item.prepared_file = None
                del playlist[donor_idx]
                print(warning(f"↪ Replaced with '{item.song}' (moved up from later)"))
            else:
                item.skip = True
                item.prepared = True
                print(warning("↪ No replacement available; skipping slot"))

        self.wake.set()


# -------------------------------------------
# MUSIC PLAYER
# -------------------------------------------


class MusicPlayer:
    def __init__(self) -> None:
        pygame.mixer.quit()
        pygame.mixer.init(
            frequency=44100,
            size=-16,
            channels=2,
        )
        # `paused` is the user's *persistent* preference — it survives across song
        # changes, so a DJ can pause and then skip through several intros silently.
        self.paused = False
        self._skip = False  # set by skip(); lets wait() break out even while paused
        # absolute position in ms (updated on every play/start)
        self.position_offset = 0
        self.current_file: str | None = None

    def play(self, path: str | Path, start: float = 0.0) -> None:
        """Start playback of `path`. If the player is currently paused, the new song
        is loaded but held at the start, so the pause state carries over."""
        self.current_file = str(path)
        pygame.mixer.music.load(self.current_file)
        pygame.mixer.music.play(start=start)
        self.position_offset = int(start * 1000)
        if self.paused:
            pygame.mixer.music.pause()  # keep it paused at the beginning

    def wait(self) -> None:
        while True:
            if self._skip:
                self._skip = False
                break

            if self.paused:
                time.sleep(0.2)
                continue

            if not pygame.mixer.music.get_busy():
                break

            time.sleep(0.2)

    def pause(self) -> None:
        if self.paused:
            pygame.mixer.music.unpause()
            print("▶ Unpaused")
        else:
            pygame.mixer.music.pause()
            print("⏸ Paused")

        self.paused = not self.paused

    def skip(self) -> None:
        # Signal wait() to move on WITHOUT touching `paused`, so the pause
        # preference is preserved for the next song.
        self._skip = True
        pygame.mixer.music.stop()

    def get_pos(self) -> int:
        """Return absolute position in ms from start of song (correct even after seek)."""
        try:
            raw = pygame.mixer.music.get_pos()
            if raw < 0:
                return -1
            return int(self.position_offset + raw)
        except Exception:
            return -1

    def seek(self, seconds: float) -> None:
        """Reliable seek: stop, restart current file from exact position, preserve pause."""
        if self.current_file is None:
            return
        if seconds < 0:
            seconds = 0.0

        was_paused = self.paused
        pygame.mixer.music.stop()

        pygame.mixer.music.load(self.current_file)
        pygame.mixer.music.play(start=seconds)
        self.position_offset = int(seconds * 1000)

        if was_paused:
            pygame.mixer.music.pause()
            print("⏸ Paused (after seek)")
            self.paused = True
        else:
            self.paused = False


# -------------------------------------------
# GUI
# -------------------------------------------


class ControlWindow:
    def __init__(self, controller: "DanceController") -> None:
        self.controller = controller
        self.root = tk.Tk()

        ttk.Style().theme_use("clam")

        self.root.title("Dance Event Player")
        self.root.geometry("700x400")
        self.root.resizable(True, True)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        main = ttk.Frame(self.root, padding=15)
        main.pack(fill="both", expand=True)

        text_frame = ttk.Frame(main)
        text_frame.pack(fill="x", pady=(0, 15))

        # NEXT
        self.next_label = ttk.Label(
            text_frame,
            text="",
            font=("TkDefaultFont", 11, "italic"),
            anchor="center",
            justify="center",
            wraplength=660,
        )
        self.next_label.pack(fill="x", pady=4)

        # NOW: Dance
        self.current_dance_label = ttk.Label(
            text_frame,
            text="",
            font=("TkDefaultFont", 14),
            anchor="center",
            justify="center",
        )
        self.current_dance_label.pack(fill="x", pady=(10, 2))

        # Song title (big + wrapped)
        self.current_song_label = ttk.Label(
            text_frame,
            text="",
            font=("TkDefaultFont", 14, "bold"),
            anchor="center",
            justify="center",
            wraplength=660,
        )
        self.current_song_label.pack(fill="x", pady=2)

        self.empty_label = ttk.Label(
            text_frame,
            text="",
            font=("TkDefaultFont", 9),
            foreground="gray",
            anchor="center",
            justify="center",
            wraplength=660,
        )
        self.empty_label.pack(fill="x", pady=6)

        self.history_label = ttk.Label(
            text_frame,
            text="",
            font=("TkDefaultFont", 9),
            foreground="gray",
            anchor="center",
            justify="center",
        )
        self.history_label.pack(fill="x", pady=3)

        # Progress bar
        progress_frame = ttk.Frame(main)
        progress_frame.pack(fill="x", pady=(0, 10))

        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(fill="x", padx=5, pady=(0, 5))

        # Click + drag on progress bar = seek (instant visual feedback)
        self.progress.bind("<Button-1>", self._on_progress_click)
        self.progress.bind("<B1-Motion>", self._on_progress_click)

        times_frame = ttk.Frame(progress_frame)
        times_frame.pack(fill="x")

        self.elapsed_label = ttk.Label(times_frame, text="00:00")
        self.elapsed_label.pack(side="left")

        self.total_label = ttk.Label(times_frame, text="00:00")
        self.total_label.pack(side="right")

        button_frame = ttk.Frame(main)
        button_frame.pack(expand=True)

        button_frame.columnconfigure((0, 1, 2, 3), weight=1)

        self.pause_btn = ttk.Button(
            button_frame,
            text="▶ Play",
            command=self.controller.pause,
        )
        self.pause_btn.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        ttk.Button(
            button_frame,
            text="⏭ Skip",
            command=self.controller.skip,
        ).grid(row=0, column=1, sticky="nsew", padx=5, pady=5)

        self.queue_btn = ttk.Button(
            button_frame,
            text="🎵 Queue Song",
            command=self.open_queue_menu,
        )
        self.queue_btn.grid(row=0, column=2, sticky="nsew", padx=5, pady=5)

        ttk.Button(
            button_frame,
            text="❌ Exit",
            command=self.on_close,
        ).grid(row=0, column=3, sticky="nsew", padx=5, pady=5)

        if not self.controller.has_queueable():
            self.queue_btn.config(state="disabled")

        self.current_duration_ms = 0
        self.updating_progress = False

        # ---------- styles ----------
        self.warning_style = ttk.Style()
        self.warning_style.configure(
            "Warning.TButton",
            foreground="white",
            background="#c62828",
        )
        self.warning_style.map(
            "Warning.TButton",
            background=[
                ("active", "#b71c1c"),
                ("pressed", "#8e0000"),
            ],
            foreground=[
                ("active", "white"),
                ("pressed", "white"),
            ],
        )

    # ---------- startup dialog ----------

    def ask_reset_log(self, log_size: int) -> bool:
        dialog = tk.Toplevel(self.root)
        dialog.title("Previous Event")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        result = False

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text=(
                f"A previous event log with {log_size} played songs was found.\n\n"
                "Do you want to continue the event or reset previous songs and start a new event?"
            ),
            justify="center",
            wraplength=360,
        ).pack(pady=(0, 15))

        button_frame = ttk.Frame(frame)
        button_frame.pack()

        def do_continue():
            dialog.destroy()

        def do_reset():
            nonlocal result
            result = True
            dialog.destroy()

        ttk.Button(
            button_frame,
            text="Start New Event",
            style="Warning.TButton",
            command=do_reset,
        ).pack(side="left", padx=(0, 8))

        continue_btn = ttk.Button(
            button_frame,
            text="Continue Event",
            command=do_continue,
        )
        continue_btn.pack(side="left")

        # Safe defaults
        continue_btn.focus_set()
        dialog.bind("<Return>", lambda e: do_continue())
        dialog.bind("<Escape>", lambda e: do_continue())
        dialog.protocol("WM_DELETE_WINDOW", do_continue)

        dialog.update_idletasks()
        x = (
            self.root.winfo_rootx()
            + (self.root.winfo_width() - dialog.winfo_width()) // 2
        )
        y = (
            self.root.winfo_rooty()
            + (self.root.winfo_height() - dialog.winfo_height()) // 2
        )
        dialog.geometry(f"+{x}+{y}")

        self.root.wait_window(dialog)
        return result

    # ---------- queue-song picker ----------

    def open_queue_menu(self) -> None:
        songs = self.controller.get_on_demand_songs()
        final_available = self.controller.final_available()

        if not songs and not final_available:
            messagebox.showinfo("Queue Song", "There are no songs available to queue.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Queue Song")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Choose a song to play next.\nQueued or already-played songs are greyed out.",
            justify="center",
        ).pack(pady=(0, 12))

        LIST_WIDTH = 380
        ROW_H = 34  # approximate height of one ttk button row

        # Everything queueable lives in one scrollable list (scrollbar only appears
        # if the content overflows). The final song is the last entry.
        total_rows = len(songs) + (1 if final_available else 0)
        viewport_h = min(10 * ROW_H, max(3, total_rows) * ROW_H) + (
            10 if (songs and final_available) else 0
        )

        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)

        canvas = tk.Canvas(
            list_frame,
            width=LIST_WIDTH,
            height=viewport_h,
            borderwidth=0,
            highlightthickness=0,
        )
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)

        def _scrollable() -> bool:
            return inner.winfo_reqheight() > canvas.winfo_height()

        def _on_inner(_e=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            if _scrollable():
                vsb.pack(side="right", fill="y")
            else:
                vsb.pack_forget()

        def _on_canvas(e) -> None:
            canvas.itemconfigure(inner_id, width=e.width)

        inner.bind("<Configure>", _on_inner)
        canvas.bind("<Configure>", _on_canvas)

        # Mouse-wheel scrolling, active only while the cursor is over the list.
        def _on_wheel(e) -> None:
            if not _scrollable():
                return
            delta = -1 if getattr(e, "num", None) == 4 or e.delta > 0 else 1
            canvas.yview_scroll(delta, "units")

        def _bind_wheel(_e=None) -> None:
            canvas.bind_all("<MouseWheel>", _on_wheel)
            canvas.bind_all("<Button-4>", _on_wheel)
            canvas.bind_all("<Button-5>", _on_wheel)

        def _unbind_wheel(_e=None) -> None:
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        dialog.bind("<Destroy>", lambda e: _unbind_wheel())

        for song in songs:
            disabled = self.controller.is_song_disabled(song)
            ttk.Button(
                inner,
                text=song,
                state="disabled" if disabled else "normal",
                command=lambda s=song: self._queue_on_demand_and_close(dialog, s),
            ).pack(fill="x", pady=1)

        # Final song: last item in the list, warning-styled, closes the app afterwards.
        if final_available:
            ttk.Button(
                inner,
                text=f"FINAL SONG:   {FINAL_SONG}",
                style="Warning.TButton",
                command=lambda: self._queue_final_and_close(dialog),
            ).pack(fill="x", pady=1)

        ttk.Button(frame, text="Cancel", command=dialog.destroy).pack(pady=(14, 0))

        dialog.update_idletasks()
        x = (
            self.root.winfo_rootx()
            + (self.root.winfo_width() - dialog.winfo_width()) // 2
        )
        y = (
            self.root.winfo_rooty()
            + (self.root.winfo_height() - dialog.winfo_height()) // 2
        )
        dialog.geometry(f"+{x}+{y}")

    def _queue_on_demand_and_close(self, dialog: tk.Toplevel, song: str) -> None:
        dialog.destroy()
        self.controller.queue_on_demand_song(song)

    def _queue_final_and_close(self, dialog: tk.Toplevel) -> None:
        dialog.destroy()
        self.controller.play_final()

    # ---------- main UI update ----------

    def update(
        self,
        current: str,
        next_song: str,
        empty_categories: list[str],
        history: list[tuple[str, str]],
        duration_ms: int = 0,
        final_queued: bool = False,
    ) -> None:
        self.root.after(
            0,
            self._update_ui,
            current,
            next_song,
            empty_categories,
            history,
            duration_ms,
            final_queued,
        )

    def _update_ui(
        self,
        current: str,
        next_song: str,
        empty_categories: list[str],
        history: list[tuple[str, str]],
        duration_ms: int,
        final_queued: bool,
    ) -> None:
        # Next
        if final_queued and FINAL_SONG:
            self.next_label.config(text=f"FINAL SONG:  {FINAL_SONG}")
        elif next_song:
            self.next_label.config(text=f"NEXT:  {next_song}")
        else:
            self.next_label.config(text="")

        # Split current display: NOW: Dance above song title
        if " - " in current:
            dance_part, song_part = current.split(" - ", 1)
            self.current_dance_label.config(text=f"NOW:  {dance_part}")
            self.current_song_label.config(text=song_part)
        else:
            self.current_dance_label.config(text="NOW:")
            self.current_song_label.config(text=current)

        if empty_categories:
            self.empty_label.config(
                text="Empty dance categories: " + ", ".join(empty_categories)
            )
        else:
            self.empty_label.config(text="")

        if history:
            lines = [f"{d} — {s}" for d, s in history]
            self.history_label.config(text="Recent songs:\n" + "\n".join(lines))
        else:
            self.history_label.config(text="")

        self.current_duration_ms = int(duration_ms) if duration_ms else 0
        if self.current_duration_ms > 0:
            total_s = int(round(self.current_duration_ms / 1000.0))
            self.total_label.config(text=f"{total_s // 60:02d}:{total_s % 60:02d}")
            self.progress.config(value=0, maximum=100)
            if not self.updating_progress:
                self.updating_progress = True
                self.root.after(200, self._update_progress)
        else:
            self.total_label.config(text="00:00")
            self.elapsed_label.config(text="00:00")
            self.progress.config(value=0, maximum=100)
            self.updating_progress = False

        self.root.update_idletasks()
        self.root.geometry(f"700x{self.root.winfo_reqheight()}")

    def _update_progress(self) -> None:
        pos = self.controller.player.get_pos()
        if pos is None or pos < 0:
            self.elapsed_label.config(text="00:00")
            if not pygame.mixer.music.get_busy():
                self.progress.config(value=0)
                self.updating_progress = False
                return
        else:
            elapsed_ms = pos
            if self.current_duration_ms > 0:
                pct = min(100.0, (elapsed_ms / self.current_duration_ms) * 100.0)
                self.progress.config(value=pct)

            elapsed_s = int(round(elapsed_ms / 1000.0))
            self.elapsed_label.config(
                text=f"{elapsed_s // 60:02d}:{elapsed_s % 60:02d}"
            )

        if pygame.mixer.music.get_busy() or self.controller.player.paused:
            self.root.after(200, self._update_progress)
        else:
            self.updating_progress = False
            self.progress.config(value=0)
            self.elapsed_label.config(text="00:00")

    def _on_progress_click(self, event) -> None:
        """Left-click / drag on the progress bar to seek, with instant feedback."""
        if self.current_duration_ms <= 0:
            return

        widget_width = self.progress.winfo_width()
        if widget_width <= 0:
            return

        click_x = max(0, min(event.x, widget_width))
        fraction = click_x / widget_width
        new_pos_ms = int(fraction * self.current_duration_ms)
        new_pos_seconds = new_pos_ms / 1000.0

        elapsed_s = new_pos_ms // 1000
        self.elapsed_label.config(text=f"{elapsed_s // 60:02d}:{elapsed_s % 60:02d}")
        self.progress.config(value=fraction * 100)

        self.controller.seek(new_pos_seconds)

    def update_pause_button(self, paused: bool) -> None:
        self.root.after(0, self._update_pause_button, paused)

    def _update_pause_button(self, paused: bool) -> None:
        if paused:
            self.pause_btn.config(text="▶ Continue")
        else:
            self.pause_btn.config(text="⏸ Pause")

    def mark_final_requested(self) -> None:
        # Once the final song is next, don't allow queueing anything in front of it.
        self.queue_btn.config(
            text="🎵 Final Song Queued",
            state="disabled",
        )

    def show_final_preview(self) -> None:
        """Immediately show the final song preview."""
        if FINAL_SONG:
            self.next_label.config(text=f"FINAL SONG:  {FINAL_SONG}")

    def refresh_queue_button(self) -> None:
        """Disable the Queue Song button once nothing is left to queue."""
        if self.controller.final_queued:
            return  # already renamed/disabled by mark_final_requested
        state = "normal" if self.controller.has_queueable() else "disabled"
        self.queue_btn.config(state=state)

    def on_close(self) -> None:
        if messagebox.askyesno("Exit", "Stop playback and close the program?"):
            pygame.mixer.music.stop()
            self.controller.cleanup()
            self.root.destroy()
            sys.exit()


# -------------------------------------------
# MAIN CONTROLLER
# -------------------------------------------


class DanceController:
    def __init__(self) -> None:
        self.lock = threading.RLock()

        self.categories = load_song_list(SONG_LIST_FILE)
        self.audio_files = scan_audio_files(MUSIC_ROOTS)

        all_songs = []
        for cat in self.categories.values():
            all_songs.extend(cat)

        self.matcher = SongMatcher(all_songs, self.audio_files)
        self.selector = DanceSelector(self.categories)
        self.player = MusicPlayer()

        # State the GUI queries during its own construction must exist first.
        self.played: set[str] = set()
        self.final_queued = False

        self.window = ControlWindow(self)

        # ---- played log (unchanged semantics) ----
        log_path = Path(PLAYED_LOG_FILE)
        if log_path.exists():
            try:
                with open(log_path, encoding="utf8") as f:
                    loaded = {line.strip() for line in f if line.strip()}

                if loaded:
                    if self.window.ask_reset_log(len(loaded)):
                        log_path.unlink()
                        print(success("\n✓ Played songs log reset for new event."))
                    else:
                        self.played.update(loaded)
                        print(
                            success(
                                f"\n✓ Loaded {len(loaded)} previously played songs from {PLAYED_LOG_FILE}"
                            )
                        )

            except Exception as e:
                print(error(f"Error: could not load played log: {e}"))

        # ---- build the full plan up front ----
        builder = PlaylistBuilder(
            self.categories,
            self.matcher,
            self.selector,
            self.played,
        )
        self.playlist, self.empty_categories = builder.build()

        self.current_index = 0
        self.current_item: PlaylistItem | None = None
        self.song_history: deque[tuple[str, str]] = deque(maxlen=HISTORY_LENGTH)
        self.first_song = True

        self.worker = PreparationWorker(self)

    # ---------- lifecycle ----------

    def start(self) -> None:
        self.worker.start()
        threading.Thread(target=self.run, daemon=True).start()

    def cleanup(self) -> None:
        self.worker.stop()
        shutil.rmtree(PREPARED_DIR, ignore_errors=True)

    # ---------- on-demand-song helpers (used by GUI) ----------

    def get_on_demand_songs(self) -> list[str]:
        """On-demand songs (in file order) that actually matched an audio file."""
        return [
            s
            for s in self.categories.get(ON_DEMAND_SONGS_CATEGORY, [])
            if self.matcher.get(s) is not None
        ]

    def is_song_disabled(self, song: str) -> bool:
        """Greyed out only once actually played — same rule as regular songs.
        Accidental re-queueing is prevented in queue_on_demand_song() instead."""
        return song in self.played

    def final_available(self) -> bool:
        """Whether the Final Song can still be offered in the queue menu."""
        return (
            FINAL_SONG is not None
            and not self.final_queued
            and self.matcher.get(FINAL_SONG) is not None
        )

    def has_queueable(self) -> bool:
        if self.final_available():
            return True
        return any(not self.is_song_disabled(s) for s in self.get_on_demand_songs())

    # ---------- playback controls (used by GUI) ----------

    def pause(self) -> None:
        self.player.pause()
        self.window.update_pause_button(self.player.paused)

    def skip(self) -> None:
        self.player.skip()
        self.window.update_pause_button(self.player.paused)

    def seek(self, seconds: float) -> None:
        self.player.seek(seconds)

    # ---------- queueing (list edits, no flags) ----------

    def queue_on_demand_song(self, song: str) -> None:
        path = self.matcher.get(song)
        if path is None:
            print(warning(f"Cannot queue '{song}' (no matched audio file)"))
            return

        item = PlaylistItem(
            dance=ON_DEMAND_SONGS_CATEGORY, song=song, path=path, is_custom=True
        )
        removed: list[PlaylistItem] = []
        with self.lock:
            insert_at = min(self.current_index + 1, len(self.playlist))
            self.playlist.insert(insert_at, item)

            # Prevent accidental duplicates: drop any *later* occurrences of the
            # same song (a previously-queued copy, or one scheduled under a real
            # dance). The just-inserted copy at insert_at is kept.
            i = insert_at + 1
            while i < len(self.playlist):
                other = self.playlist[i]
                if other.song == song and not other.is_final:
                    removed.append(self.playlist.pop(i))
                else:
                    i += 1

        for r in removed:
            if r.prepared_file is not None:
                try:
                    Path(r.prepared_file).unlink(missing_ok=True)
                except Exception:
                    pass

        self.worker.notify()
        print(success(f"✚ Queued: {song}"))
        self.window.refresh_queue_button()
        if self.current_item is not None:
            self._push_ui_update(self.current_item)

    def play_final(self) -> None:
        if not FINAL_SONG:
            return
        if not messagebox.askyesno(
            "Final Song",
            f"Play the final song '{FINAL_SONG}' next?\n\nAfterwards, the app will be closed.",
        ):
            return

        with self.lock:
            final_item = next((it for it in self.playlist if it.is_final), None)
            if final_item is None:
                fpath = self.matcher.get(FINAL_SONG)
                if fpath is not None:
                    final_item = PlaylistItem(
                        dance="Final Dance",
                        song=FINAL_SONG,
                        path=fpath,
                        is_final=True,
                    )

            if final_item is not None:
                kept = [
                    it
                    for it in self.playlist[: self.current_index + 1]
                    if not it.is_final
                ]
                self.playlist = kept + [final_item]
                queued = True
            else:
                queued = False

        if not queued:
            messagebox.showwarning(
                "Final Song", "The final song could not be found / matched."
            )
            return

        self.final_queued = True
        self.worker.notify()
        self.window.mark_final_requested()
        self.window.show_final_preview()
        if self.current_item is not None:
            self._push_ui_update(self.current_item)

    # ---------- log ----------

    def _save_played_log(self) -> None:
        try:
            with open(PLAYED_LOG_FILE, "w", encoding="utf8") as f:
                for song in sorted(self.played):
                    f.write(song + "\n")
        except Exception as e:
            print(warning(f"Warning: could not save played log: {e}"))

    # ---------- internal helpers ----------

    def _advance(self) -> None:
        with self.lock:
            prev = (
                self.playlist[self.current_index]
                if self.current_index < len(self.playlist)
                else None
            )
            self.current_index += 1

        if prev is not None and prev.prepared_file is not None:
            try:
                Path(prev.prepared_file).unlink(missing_ok=True)
            except Exception:
                pass

        self.worker.notify()

    def _push_ui_update(self, current_item: PlaylistItem) -> None:
        with self.lock:
            idx = self.current_index
            nxt = self.playlist[idx + 1] if idx + 1 < len(self.playlist) else None

        next_is_final = bool(nxt and nxt.is_final)
        next_display = nxt.display() if nxt else ""

        self.window.update(
            f"{current_item.dance} - {current_item.song}",
            next_display,
            self.empty_categories,
            list(self.song_history),
            current_item.duration_ms,
            final_queued=next_is_final,
        )

    def _log_now_playing(self, item: PlaylistItem) -> None:
        divider()
        label = "Final Song" if item.is_final else "Playing"
        print(f"▶ {label}: {bold(item.dance)}")
        print(f"   Song: {bold(item.song)}")
        if item.path is not None:
            print(f"   File: {item.path.name}")
        dur_s = int(round(item.duration_ms / 1000.0))
        print(f"   Length: {dur_s // 60}:{dur_s % 60:02d}")
        if (
            item.loudness is not None
            and not np.isnan(item.loudness)
            and not np.isinf(item.loudness)
        ):
            print(
                f"   Loudness: {item.loudness:.1f} LUFS → applied {item.gain_db:+.1f} dB"
            )
        divider()

    # ---------- main playback loop ----------

    def run(self) -> None:
        while True:
            with self.lock:
                if self.current_index >= len(self.playlist):
                    break
                item = self.playlist[self.current_index]

            if item.skip:
                self._advance()
                continue

            # Make sure the current item is prepared before playing.
            self.worker.notify()
            while not item.prepared and not item.skip:
                time.sleep(0.05)

            if item.skip:
                self._advance()
                continue

            # First song starts paused (as before) — only happens once. Setting the
            # flag *before* play() means the song loads already-paused (no audio blip),
            # and the persistent-pause model carries it forward.
            if self.first_song:
                self.first_song = False
                self.player.paused = True
                self.window.update_pause_button(True)

            self.current_item = item
            self._log_now_playing(item)
            self._push_ui_update(item)

            self.player.play(item.prepared_file)
            self.player.wait()

            # Finished (or skipped via the Skip button).
            item.played = True
            with self.lock:
                self.played.add(item.song)
            self._save_played_log()
            self.song_history.appendleft((item.dance, item.song))

            is_final = item.is_final
            self._advance()
            if is_final:
                break

        pygame.mixer.music.stop()
        self.cleanup()
        self.window.root.after(0, self.window.root.destroy)


# -------------------------------------------
# RUN
# -------------------------------------------


def main() -> None:
    controller = DanceController()
    controller.start()
    controller.window.root.mainloop()


if __name__ == "__main__":
    main()
