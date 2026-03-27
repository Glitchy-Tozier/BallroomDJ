import os
import re
import random
import sys
import threading
import time
import unicodedata
from collections import deque, defaultdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from pprint import pprint

import numpy as np
import pygame
import pyloudnorm as pyln
from mutagen import File as MutagenFile
from pydub import AudioSegment
from pydub.silence import detect_nonsilent


# -------------------------------------------
# CONFIG
# -------------------------------------------

os.environ["SDL_AUDIODRIVER"] = "pulse"  # Use PulseAudio backend (Linux audio system)

HOME = Path.home()  # User home directory (base for all paths below)

SONG_LIST_FILE = HOME / "Music/Tanzmusik.txt"  # Text file defining dance categories + songs

MUSIC_ROOTS = [  # Folders where audio files are searched recursively
    HOME / "Music/Arni",
    HOME / "Music/NewPipe",
    HOME / "Music/Seal",
]

FINAL_SONG = "Annie Lennox - I Put A Spell On You"  # Played at the very end (None to disable)

MIN_SONG_PAUSE = defaultdict(
    # Default: how many songs must pass before a dance can repeat.
    # Should be smaller than the number of available song types
    lambda: 5,
    # Custom settings: Specify which songs should show up more/less frequently than the default set above ↑
    {
        "Paso Doble": 12, # Higher = rarer
        "Samba": 8, 
        "Tango": 8,
        #"West Coast Swing": 4,
        "Wiener Walzer": 10,

    }
)
SONG_CATEGORY_REPEATS = defaultdict(
    # Default: play 1 song per dance category before switching to the next dance category
    # (i.e. after one ChaChaCha is played, a non-ChaChaCha-song is chosen next)
    lambda: 1,
    # Custom settings: Specify in which song categories multiple songs should be played right after each other.
    {
        #"Tango Argentino": 4,
        "West Coast Swing": 2,

    }
)

TARGET_LOUDNESS = -14.0  # Normalize all songs to this loudness (LUFS)

TEMP_FILE = "temp_song.wav"  # Temporary file used for playback (processed audio)

TRIM_SILENCE = True  # Automatically remove silence at start/end of songs
SILENCE_THRESHOLD_DB = -50  # What counts as silence (lower = stricter)
SILENCE_CHUNK_MS = 10       # Analysis step size (smaller = more precise)
SILENCE_MIN_LEN_MS = 500    # Minimum silence length to be trimmed

HISTORY_LENGTH = 12  # Number of recent songs shown in UI

PLAYED_LOG_FILE = HOME / "Music/played_songs.log"  # Stores played songs to avoid repeats across runs

# -------------------------------------------
# PRETTY CONSOLE
# -------------------------------------------

def bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


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
    def __init__(self, path: Path) -> None:
        self.path = path
        self.artist = ""
        self.title = path.stem
        self.file_stem = path.stem

        try:
            meta = MutagenFile(path, easy=True)

            if meta and meta.tags:
                artist_tag = meta.tags.get("artist", "")
                title_tag = meta.tags.get("title", "")

                if isinstance(artist_tag, (list, tuple)) and artist_tag:
                    self.artist = artist_tag[0]
                elif artist_tag:
                    self.artist = artist_tag

                if isinstance(title_tag, (list, tuple)) and title_tag:
                    self.title = title_tag[0]
                elif title_tag:
                    self.title = title_tag

        except Exception:
            pass

        if self.artist:
            self.full = f"{self.artist} - {self.title}".strip(" -")
        else:
            self.full = self.title

        combined = " ".join([
            self.artist or "",
            self.title or "",
            self.file_stem or "",
            self.path.name or "",
        ])

        cleaned = clean_for_matching(combined)

        self.search_text = normalize_unicode(cleaned)
        self.token_set = set(self.search_text.split())


class SongMatcher:
    def __init__(self, song_names: list[str], audio_files: list[Path]) -> None:
        print("Building SIMPLE word-count matcher (improved)...")

        self.entries = [SongEntry(f) for f in audio_files]
        self.mapping = {}

        for song in song_names:
            print(f'\nMatching "{song}"')

            best_entry, best_count, best_prop = self._find_best_match(song)

            if best_entry and best_count >= 1:
                self.mapping[song] = best_entry.path

                print(
                    f"✓ {song} → {best_entry.full} "
                    f"({best_entry.path.name}) "
                    f"[{best_count} tokens matched, {best_prop:.0%} of query]"
                )
            else:
                print(f"⚠ Weak/No match for '{song}' (best was {best_count} tokens)")

                if best_entry:
                    print(f"   Closest: {best_entry.full} ({best_entry.path.name})")

                self.mapping[song] = None

        print("Matching complete\n")

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
            print("⚠ Multiple equally good matches:")
            for e in best_entries[:5]:
                print("   ", e.path.name)

        best_entry = min(best_entries, key=lambda e: len(e.search_text))

        return best_entry, best_count, best_prop

    def get(self, song: str) -> Path | None:
        return self.mapping.get(song)


def scan_audio_files(roots: list[str]) -> list[Path]:
    files = []

    for root in roots:
        root_path = Path(root)

        if not root_path.exists():
            print(f"Warning: directory not found -> {root}")
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
        print(f"Warning: bad loudness for {path.name} → using original")
        return audio, loudness, 0.0

    gain_db = TARGET_LOUDNESS - loudness

    return audio.apply_gain(gain_db), loudness, gain_db


# -------------------------------------------
# DANCE SELECTION
# -------------------------------------------

class DanceSelector:
    def __init__(self, categories: dict[str, list[str]]) -> None:
        default_pause = MIN_SONG_PAUSE.default_factory()
        self.songs_passed_since = defaultdict(lambda: default_pause + 1)
        self.history = deque(maxlen=HISTORY_LENGTH)

    def _build_weights(self, available_categories: list[str]) -> list[float]:
        weights = []
        #songs_passed_since_list = [] # For debugging

        for category in available_categories:
            min_pause = MIN_SONG_PAUSE[category]
            songs_passed_since = self.songs_passed_since[category]
            #songs_passed_since_list.append(songs_passed_since) # For debugging

            weight = max(songs_passed_since - min_pause, 0) # No negative weights allowed!
            weights.append(weight)
            
        #pprint([f"{a}: passed_since: {b} -> {c})" for a, b, c in (zip(available_categories, songs_passed_since_list, weights))]) # For debugging
        return weights

    def _weighted_pick(self, categories: list[str], weights: list[float]) -> str:
        if sum(weights) == 0:
            # choose dance closest to its minimum pause
            return max(
                categories,
                key=lambda c: self.songs_passed_since[c] / MIN_SONG_PAUSE[c]
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
        self.paused = False
        self.position_offset = 0   # absolute position in ms (updated on every play/start)

    def play(self, path: str, start: float = 0.0) -> None:
        """Start (or restart) playback. start is in seconds from beginning of song."""
        pygame.mixer.music.load(path)
        pygame.mixer.music.play(start=start)
        self.position_offset = int(start * 1000)

    def wait(self) -> None:
        while True:
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
        """Reliable seek: stop, restart from exact position, preserve pause state.
        get_pos() is now always correct immediately thanks to position_offset."""
        if seconds < 0:
            seconds = 0.0

        was_paused = self.paused
        self.skip()  # stop

        pygame.mixer.music.load(TEMP_FILE)
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

        self.final_btn = ttk.Button(
            button_frame,
            text="🎵 Final Song",
            command=self.controller.play_final,
        )
        self.final_btn.grid(row=0, column=2, sticky="nsew", padx=5, pady=5)

        if FINAL_SONG is None:
            self.final_btn.config(state="disabled")

        ttk.Button(
            button_frame,
            text="❌ Exit",
            command=self.on_close,
        ).grid(row=0, column=3, sticky="nsew", padx=5, pady=5)

        self.current_duration_ms = 0
        self.updating_progress = False

    def update(self, current: str, next_song: str, empty_categories: list[str], history: list[tuple[str, str]], duration_ms: int = 0, final_queued: bool = False) -> None:
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

    def _update_ui(self, current: str, next_song: str, empty_categories: list[str], history: list[tuple[str, str]], duration_ms: int, final_queued: bool) -> None:
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
            self.empty_label.config(text="Empty dance categories: " + ", ".join(empty_categories))
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
            self.total_label.config(text=f"{total_s//60:02d}:{total_s%60:02d}")
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
            self.elapsed_label.config(text=f"{elapsed_s//60:02d}:{elapsed_s%60:02d}")

        if pygame.mixer.music.get_busy() or self.controller.player.paused:
            self.root.after(200, self._update_progress)
        else:
            self.updating_progress = False
            self.progress.config(value=0)
            self.elapsed_label.config(text="00:00")

    def _on_progress_click(self, event) -> None:
        """Handle left-click or drag on the progress bar to seek.
        Provides instant visual feedback + calls the player through the controller.
        Works while paused (including the very first song) and respects final-song queuing."""
        if self.current_duration_ms <= 0:
            return

        widget_width = self.progress.winfo_width()
        if widget_width <= 0:
            return

        click_x = max(0, min(event.x, widget_width))
        fraction = click_x / widget_width
        new_pos_ms = int(fraction * self.current_duration_ms)
        new_pos_seconds = new_pos_ms / 1000.0

        # Instant visual feedback (progress + elapsed time)
        elapsed_s = new_pos_ms // 1000
        self.elapsed_label.config(text=f"{elapsed_s//60:02d}:{elapsed_s%60:02d}")
        self.progress.config(value=fraction * 100)

        # Delegate seek (keeps design consistent with pause/skip)
        self.controller.seek(new_pos_seconds)

    def update_pause_button(self, paused: bool) -> None:
        if paused:
            self.pause_btn.config(text="▶ Continue")
        else:
            self.pause_btn.config(text="⏸ Pause")

    def mark_final_requested(self) -> None:
        self.final_btn.config(
            text="🎵 Final Song Queued",
            state="disabled",
        )
    
    def show_final_preview(self) -> None:
        """Immediately show the final song preview."""
        if FINAL_SONG:
            self.next_label.config(text=f"FINAL SONG:  {FINAL_SONG}")

    def on_close(self) -> None:
        if messagebox.askyesno("Exit", "Stop playback and close the program?"):
            pygame.mixer.music.stop()
            self.root.destroy()
            sys.exit()


# -------------------------------------------
# MAIN CONTROLLER
# -------------------------------------------

class DanceController:
    def __init__(self) -> None:
        self.categories = load_song_list(SONG_LIST_FILE)
        self.audio_files = scan_audio_files(MUSIC_ROOTS)

        all_songs = []
        for cat in self.categories.values():
            all_songs.extend(cat)

        self.matcher = SongMatcher(all_songs, self.audio_files)
        self.selector = DanceSelector(self.categories)
        self.player = MusicPlayer()
        self.window = ControlWindow(self)

        self.played = set()

        if "--reset-log" in sys.argv:
            log_path = Path(PLAYED_LOG_FILE)
            if log_path.exists():
                log_path.unlink()
                print("✓ Played songs log reset for new event.")

        if Path(PLAYED_LOG_FILE).exists():
            try:
                with open(PLAYED_LOG_FILE, encoding="utf8") as f:
                    loaded = {line.strip() for line in f if line.strip()}
                self.played.update(loaded)
                print(f"✓ Loaded {len(loaded)} previously played songs from {PLAYED_LOG_FILE}")
            except Exception as e:
                print(f"Warning: could not load played log: {e}")

        self.force_final = False
        self.next_item = None
        self.song_history = deque(maxlen=HISTORY_LENGTH)
        self.first_song = True

    def get_available_dances(self) -> list[str]:
        """Return only dances that still have unplayed songs."""
        return [
            d for d in self.categories
            if any(s not in self.played and s != FINAL_SONG for s in self.categories[d])
        ]

    def pause(self) -> None:
        self.player.pause()
        self.window.update_pause_button(self.player.paused)

    def skip(self) -> None:
        self.player.skip()

    def seek(self, seconds: float) -> None:
        """Delegate seeking to the MusicPlayer (keeps all audio control in one place)."""
        self.player.seek(seconds)

    def play_final(self) -> None:
        if FINAL_SONG and messagebox.askyesno(
            "Final Song",
            f"Play the final song '{FINAL_SONG}' next?\n\nAfterwards, the app will be closed.",
        ):
            self.force_final = True
            self.window.mark_final_requested() # Update button state
            self.window.show_final_preview()   # Immediately update preview text

    def choose_song(self, dance: str) -> str | None:
        songs = [
            s for s in self.categories[dance]
            if s not in self.played and s != FINAL_SONG
        ]
        if not songs:
            return None
        song = random.choice(songs)
        self.played.add(song)
        return song

    def pick_next(self, available_categories: list[str]) -> tuple[str, str] | None:
        """Robust next picker that guarantees a valid dance+song."""
        if not available_categories:
            return None

        for _ in range(9):  # retry a few times
            dance = self.selector.peek(available_categories)
            if dance is None and available_categories:
                dance = random.choice(available_categories)

            songs = [
                s for s in self.categories[dance]
                if s not in self.played and s != FINAL_SONG
            ]
            if songs:
                song = random.choice(songs)
                return (dance, song)

        # Final fallback
        for dance in available_categories:
            songs = [
                s for s in self.categories[dance]
                if s not in self.played and s != FINAL_SONG
            ]
            if songs:
                return (dance, random.choice(songs))

        return None

    def _save_played_log(self) -> None:
        try:
            with open(PLAYED_LOG_FILE, "w", encoding="utf8") as f:
                for song in sorted(self.played):
                    f.write(song + "\n")
        except Exception as e:
            print(f"Warning: could not save played log: {e}")

    def run(self) -> None:
        while True:
            available_categories = self.get_available_dances()
            empty_categories = [c for c in self.categories if c not in available_categories]

            if not available_categories:
                break

            # Get or refresh next_item
            if self.next_item is None or self.next_item[0] not in available_categories or self.next_item[1] in self.played:
                self.next_item = self.pick_next(available_categories)

            if self.next_item is None:
                dance = self.selector.peek(available_categories)
                self.selector.commit(dance)
            else:
                dance = self.next_item[0]
                self.selector.commit(dance)

            if dance is None or dance not in available_categories:
                available_categories = self.get_available_dances()
                if available_categories:
                    dance = self.selector.peek(available_categories)
                    self.selector.commit(dance)
                else:
                    break

            repeat = SONG_CATEGORY_REPEATS[dance]

            for repeat_idx in range(repeat):
                if self.next_item and self.next_item[0] == dance and self.next_item[1] not in self.played:
                    song = self.next_item[1]
                    self.played.add(song)
                else:
                    song = self.choose_song(dance)

                if song is None:
                    break

                path = self.matcher.get(song)
                if path is None:
                    print(f"Skipping '{song}' (no matched audio file)")
                    continue

                audio, loudness, gain_db = normalize_audio(path)
                audio = trim_silence(audio)
                duration_ms = len(audio)
                audio.export(TEMP_FILE, format="wav")

                # Prepare next preview
                available_after = self.get_available_dances()

                if repeat_idx == repeat - 1:
                    self.next_item = self.pick_next(available_after)
                else:
                    # WCS second song (same dance)
                    songs = [s for s in self.categories[dance] if s not in self.played and s != FINAL_SONG]
                    if songs:
                        self.next_item = (dance, random.choice(songs))
                    else:
                        self.next_item = self.pick_next(available_after)

                next_display = f"{self.next_item[0]} — {self.next_item[1]}" if self.next_item else ""

                self.window.update(
                    f"{dance} - {song}",
                    next_display,
                    empty_categories,
                    list(self.song_history),
                    duration_ms,
                    final_queued=self.force_final,
                )

                divider()
                print(f"▶ Playing: {bold(dance)}")
                print(f"   Song: {bold(song)}")
                print(f"   File: {path.name}")
                dur_s = int(round(duration_ms / 1000.0))
                print(f"   Length: {dur_s//60}:{dur_s%60:02d}")
                if loudness is not None and not np.isnan(loudness) and not np.isinf(loudness):
                    print(f"   Loudness: {loudness:.1f} LUFS → applied {gain_db:+.1f} dB")
                divider()

                self.player.play(TEMP_FILE)

                # Start first song paused (as requested) — only happens once
                if self.first_song:
                    self.player.pause()
                    self.first_song = False

                self.player.wait()

                self.song_history.appendleft((dance, song))
                self._save_played_log()

                if self.force_final:
                    break

            if self.force_final:
                break

        # Final song
        if FINAL_SONG:
            path = self.matcher.get(FINAL_SONG)
            if path:
                audio, loudness, gain_db = normalize_audio(path)
                audio = trim_silence(audio)
                duration_ms = len(audio)
                audio.export(TEMP_FILE, format="wav")

                self.window.update(
                    f"Final Dance - {FINAL_SONG}",
                    "",
                    list(self.categories.keys()),  # keep showing all (now empty) categories
                    list(self.song_history),
                    duration_ms,
                )

                divider()
                print(f"▶ Final Song: {bold(FINAL_SONG)}")
                print(f"   File: {path.name}")
                dur_s = int(round(duration_ms / 1000.0))
                print(f"   Length: {dur_s//60}:{dur_s%60:02d}")
                if loudness is not None and not np.isnan(loudness) and not np.isinf(loudness):
                    print(f"   Loudness: {loudness:.1f} LUFS → applied {gain_db:+.1f} dB")
                divider()

                self.player.play(TEMP_FILE)
                self.player.wait()

                self.played.add(FINAL_SONG)
                self._save_played_log()

        pygame.mixer.music.stop()
        self.window.root.after(0, self.window.root.destroy)


# -------------------------------------------
# RUN
# -------------------------------------------

def main() -> None:
    controller = DanceController()

    threading.Thread(target=controller.run, daemon=True).start()
    controller.window.root.mainloop()


if __name__ == "__main__":
    main()
