# BallroomDJ

A Python script that automatically plays dance music for social dance events.

It randomly selects dances (e.g. Cha Cha, Tango, Waltz, West Coast Swing), chooses songs from predefined lists, and plays the matching audio files from your music library. The program includes loudness normalization, silence trimming, and a small control GUI.

It was designed for mixed dance events that combine **ballroom/Latin dances** with **West Coast Swing (WCS)**.

---

## Features

- **Weighted random dance selection**
  - Prevents the same dance type from repeating too often
  - Recently played dances are temporarily put on cooldown

- **West Coast Swing support**
  - Automatically plays **two WCS songs in a row**

- **Song history**
  - Songs are never repeated during a run
  - Previously played songs can optionally be excluded across runs

- **Fuzzy song matching**
  - Matches the song names in the song list with audio files on disk
  - Uses both **file names** and **audio metadata (artist/title)**

- **Automatic loudness normalization**
  - Normalizes songs to approximately **-14 LUFS** to ensure consistent volume across different tracks

- **Silence trimming**
  - Removes long silence at the beginning/end of tracks

- **Live control window**
  - Pause / resume
  - Skip current song
  - Queue final song
  - Shows:
    - current dance + song
    - next dance + song
    - previous dances + songs
    - current progress bar
    - empty categories

---

## Setup

### Song List Format

Songs are provided in a `.txt` file structured like this:

```
Cha Cha Cha
Song A
Song B
Song C

Tango
Song D
Song E

West Coast Swing
Song F
Song G
```

Rules:

- The **first line** of each block is the **dance category**
- The following lines are **song names**
- Categories are separated by a **blank line**
- The file ends at ... the end of the file ... or the first line containing `---`.

### Music Library

The script scans one or more directories recursively for audio files.

Supported formats:

```
mp3
wav
flac
m4a
ogg
opus
aac
```

Song names from the list are matched **fuzzily** against:
- filename
- artist metadata
- title metadata

---

## Installation

Install dependencies:

```bash
pip install numpy pygame pyloudnorm mutagen pydub
```

You also need **ffmpeg** installed for `pydub`.

---

## Configuration

Edit the configuration section at the top of the script.

Important settings:

| Variable          | Description                                             |
| ----------------- | ------------------------------------------------------- |
| `SONG_LIST_FILE`  | Path to the song list text file                         |
| `MUSIC_ROOTS`     | Directories that contain your music                     |
| `FINAL_SONG`      | Optional closing song                                   |
| `WCS_NAME`        | Name of the WCS category                                |
| `COOLDOWN_ZERO`   | Number of songs before a dance becomes selectable again |
| `TARGET_LOUDNESS` | Loudness normalization target (LUFS)                    |

---

## Running the Script

Start the player:

```bash
python ballroom_dj.py --reset-log
```

The GUI window will appear and the first song will be **paused** until you press play.

---

### Command Line Options: `--reset-log`

Songs are written to `played_songs.log` so they won't repeat in future runs.

`--reset-log` clears the `played_songs.log` file so songs can be played again.

```bash
python ballroom_dj.py --reset-log # Start BallroomDJ with all songs available
```

Usually you want to use this parameter. If your PC crashes mid-event, resume _without_ this command line parameter to avoid replaying previous soungs:

```bash
python ballroom_dj.py # Don't reset log -> continue where you left off last time
```

---

### Controls

The control window provides:

| Button           | Action                              |
| ---------------- | ----------------------------------- |
| **Play / Pause** | Pause or resume playback            |
| **Skip**         | Skip the current song               |
| **Final Song**   | Queue the configured final song     |
| **Exit**         | Stop playback and close the program |

---

## License

AGPL-3.0