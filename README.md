# BallroomDJ

A Python script that automatically plays dance music for social dance events.

It randomly selects dances (e.g. Cha Cha, Tango, Waltz, West Coast Swing), chooses songs from predefined lists, and plays the matching audio files from your music library. The program includes loudness normalization, silence trimming, and a live control GUI.

It was designed for mixed dance events that combine **ballroom/Latin dances** with **West Coast Swing (WCS)**, but can be used with any set of dance categories.

---

## Features

- **Weighted random dance selection**
  - Prevents the same dance type from repeating too often
  - Recently played dances are temporarily put on cooldown

- **Configurable consecutive dances**
  - Any dance category can be configured to play multiple songs in a row
  - By default, **West Coast Swing** plays **two songs consecutively**

- **Song history**
  - Songs are never repeated during an event
  - Previously played songs are remembered between runs
  - At startup, choose whether to continue the previous event or start a new one, resetting your song history

- **On-demand songs**
  - Songs in the **On-Demand Songs** category are excluded from automatic scheduling
  - They can be queued at any time from the control window

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
  - Queue on-demand songs
  - Queue the final song
  - Seek using the progress bar
  - Shows:
    - current dance + song
    - next dance + song
    - previous dances + songs
    - playback progress
    - empty dance categories

---

## Setup

### Song List Format

Songs are provided in a `.txt` file structured like this:

```text
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

On-Demand Songs
Special Song 1
Special Song 2

---
Other notes that'll be ignored
```

Rules:

- The **first line** of each block is the **dance category**
- The following lines are **song names**
- Categories are separated by a **blank line**
- Songs in the **On-Demand Songs** category are only played when manually queued
- The file ends at the end of the file or the first line containing `---`

### Music Library

The script scans one or more directories recursively for audio files.

Supported formats:

```text
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
- album metadata
- title metadata

---

## Installation

Install dependencies:

```bash
python3.12 -m venv venv  # If necessary
source venv/bin/activate # If necessary
pip install numpy pygame pyloudnorm mutagen pydub colorama ordered-set
```

You also need **ffmpeg** installed for `pydub`.

---

## Configuration

Edit the configuration section at the top of the script.

Important settings:

| Variable                   | Description                                       |
| -------------------------- | ------------------------------------------------- |
| `SONG_LIST_FILE`           | Path to the song list                             |
| `MUSIC_ROOTS`              | Directories containing your music                 |
| `FINAL_SONG`               | Optional closing song                             |
| `ON_DEMAND_SONGS_CATEGORY` | Category containing manually queued songs         |
| `MIN_SONG_PAUSE`           | Minimum number of songs before a dance can repeat |
| `SONG_CATEGORY_REPEATS`    | Number of consecutive songs per dance category    |
| `TARGET_LOUDNESS`          | Loudness normalization target (LUFS)              |
| `PREPARE_AHEAD`            | Number of songs prepared in advance               |

---

## Running the Script

Start the player:

```bash
source venv/bin/activate # If necessary
python ballroom_dj.py
```

If songs from a previous event are found, the program asks whether to:

- **Continue Event** (keep previously played songs excluded)
- **Start New Event** (clear the played-song log)

The first song is loaded **paused** until you press **Play**.

---

## Controls

| Button           | Action                                             |
| ---------------- | -------------------------------------------------- |
| **Play / Pause** | Pause or resume playback                           |
| **Skip**         | Skip the current song                              |
| **Queue Song**   | Queue a song from the **On-Demand Songs** category |
| **Final Song**   | Queue the configured final song                    |
| **Exit**         | Stop playback and close the program                |

---

## License

AGPL-3.0
