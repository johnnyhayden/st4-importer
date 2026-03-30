# StageTraxx4 Importer

A Python command-line tool for building StageTraxx4 backup (`.st4b`) files. It supports two import modes:

- **Stem import** — imports WAV stem files, auto-detects metadata from iTunes and lyrics from Genius, detects BPM from click tracks, skips silent stems, and optionally converts WAV to MP3.
- **Stem refresh** — re-processes and replaces audio files in an existing backup without touching any metadata. Useful for refreshing stems after editing them externally.
- **CSV bulk import** — imports a Spotify-export CSV to create song entries (no audio tracks) enriched with lyrics from Genius, using BPM and duration from the CSV.

## What It Does

### Stem Import (`--stems`)

1. **Reads an existing `.st4b` backup** (a ZIP archive containing `backup_data.json` and audio files).
2. **Scans a directory of WAV stems** named in the format `SongName_Artist_XX_StemName.wav` (e.g., `Dreams_FleetwoodMac_01_Click.wav`). The legacy 3-part format `SongName_XX_StemName.wav` is also accepted — if an artist name is entered at the prompt, files are renamed to the 4-part format automatically.
3. **Groups stems by song name**, then for each song:
   - Looks up the **artist and canonical title** via the iTunes Search API.
   - Fetches **lyrics** from the Genius API.
   - **Detects BPM** by analyzing peak intervals in the click track (if present).
   - **Skips silent stems** automatically.
   - **Converts WAV to 192kbps MP3** using ffmpeg (unless `--no-convert` is passed).
   - **Aligns lyrics to the lead vocal** using forced alignment (aeneas), producing LRC-style `[MM:SS.cc]` timestamps for synchronized lyric scrolling in StageTraxx 4.
   - **Assigns each stem to a bus** based on a built-in name-to-bus mapping (Click, Drums, Bass, Guitar, Keys, Pad, Vocals, Cues, etc.).
4. **Detects duplicate songs** already in the backup and prompts to skip or replace.
5. **Writes a new `.st4b` file** with the imported songs merged into the existing backup data.

### CSV Bulk Import (`--csv`)

1. **Reads an existing `.st4b` backup**.
2. **Parses a Spotify-export CSV** with columns like `Track Name`, `Artist Name(s)`, `Duration (ms)`, `Tempo`, etc.
3. **For each row**, creates a song entry (no audio tracks):
   - Uses the CSV artist to look up the **canonical title** via iTunes.
   - Fetches **lyrics** from Genius using the CSV artist.
   - Takes **BPM** directly from the CSV `Tempo` column.
   - Converts **duration** from milliseconds to seconds.
   - Calculates **scroll speed** from lyrics density (characters per second of song duration).
   - Enables the **StageTraxx 4 metronome** (`metronomeMode: 2`, `metronomeType: 1`) so that playing the song produces an audible click at the detected BPM — useful since CSV-imported songs have no audio tracks of their own.
4. **Skips duplicates** automatically (non-interactive) by comparing normalized titles against existing songs.
5. **Writes a new `.st4b` file** with the song entries merged into the existing backup.

### Stem Refresh (`--refresh-stems`)

1. **Reads an existing `.st4b` backup**.
2. **Scans a directory of WAV stems** using the same filename format as `--stems`.
3. **Matches each song** to an existing song in the backup by normalized title.
4. **Matches each stem** to an existing track by stem name (e.g., `Click`, `Drums`, `Vocals_Lead`).
5. **Converts WAV to MP3** (unless `--no-convert`) and **replaces the audio file** at the track's existing path in the archive.
6. **Copies `backup_data.json` unchanged** — no metadata, timestamps, lyrics, or track entries are modified.
7. **Writes a new `.st4b` file** with a timestamp suffix (e.g., `MyBackup_20260330_153045.st4b`) instead of the usual `_imported` suffix.

Stems that don't match any existing song or track are warned and skipped.

## Stem Filename Format

Stem files must follow one of two naming conventions:

| Format | Example | Notes |
|---|---|---|
| `SongName_Artist_XX_StemName.wav` | `Dreams_FleetwoodMac_01_Click.wav` | **Preferred** — artist is read directly from the filename |
| `SongName_XX_StemName.wav` | `Dreams_01_Click.wav` | Legacy — artist will be prompted at import time |

- `SongName` — the song title; underscores are treated as spaces internally
- `Artist` — the artist name with spaces replaced by underscores (e.g. `Tom_Petty`)
- `XX` — zero-padded track number (e.g. `01`, `02`)
- `StemName` — one of the recognized stem names (see below)

### Automatic filename upgrade

When a stem file uses the legacy 3-part format and you provide an artist name at the import prompt, the tool **immediately renames all stem files for that song** on disk to the 4-part format. For example:

```
Dreams_01_Click.wav  →  Dreams_Fleetwood_Mac_01_Click.wav
Dreams_02_Drums.wav  →  Dreams_Fleetwood_Mac_02_Drums.wav
```

This means future imports of the same stems will not prompt for the artist again.

## Assumptions

- **Recognized stem names**: `Click`, `Drums`, `Percussion`, `Bass`, `Guitar`, `Electric_Rhythm`, `Electric_Lead`, `Keys`, `Pad`, `Vocals_BG`, `Vocals_Lead`, `Cues`. Unrecognized names are assigned to the Percussion bus by default.
- **Artist search order**: iTunes and Genius lookups prioritize Fleetwood Mac, Stevie Nicks, Tom Petty, and Tom Petty and the Heartbreakers. This is hardcoded for the current use case and can be edited in the `METADATA_ARTIST_ORDER` and `LYRICS_ARTIST_ORDER` lists.
- **Input backup structure**: The `.st4b` file is expected to be a ZIP archive with a `backup_data.json` at its root, conforming to the StageTraxx4 schema.
- **WAV format**: Input stems must be standard WAV files (16-bit, 24-bit, or 32-bit PCM). The tool uses Python's `wave` module for duration and silence analysis.

## Prerequisites

### Python 3

Requires Python 3.8 or later.

### ffmpeg (for MP3 conversion)

The tool converts WAV stems to 192kbps MP3 by default to reduce backup file size. This requires ffmpeg to be installed and available on your PATH.

**Install with Homebrew (macOS):**

```bash
brew install ffmpeg
```

If ffmpeg is not found, the tool will print a warning and keep the original WAV files instead of converting. You can also explicitly skip conversion with the `--no-convert` flag.

### espeak (for lyric alignment)

When a `Vocals_Lead` stem is present, the tool can align fetched lyrics to the vocal audio using [aeneas](https://github.com/readbeyond/aeneas), producing timestamped LRC lyrics for synchronized scrolling in StageTraxx 4. This requires espeak (a speech synthesis engine) to be installed.

**Install with Homebrew (macOS):**

```bash
brew install espeak
```

If espeak and aeneas are not found, the tool will print a warning and use plain (un-timed) lyrics instead. You can also skip alignment with the `--no-align` flag.

### Python Dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `lyricsgenius` — Python client for the Genius API (used for lyrics lookup)
- `numpy` — required by aeneas
- `aeneas` — forced alignment library for syncing lyrics to audio

## Usage

```bash
python st4_import.py <input.st4b> [options]
```

### Options

| Flag | Description |
|---|---|
| `--stems <dir>` | Path to the stems directory. Required for stem import. |
| `--csv <file>` | Path to a Spotify-export CSV for bulk song import. |
| `--refresh-stems <dir>` | Re-process stems and replace audio files without touching metadata. |
| `-o, --output <file>` | Output `.st4b` file path. Defaults to `<input>_imported.st4b` (or `<input>_<timestamp>.st4b` with `--refresh-stems`). |
| `--dry-run` | Preview what would be imported without writing any files. (Stems mode only.) |
| `--no-convert` | Keep original WAV files instead of converting to MP3. |
| `--no-align` | Skip forced alignment of lyrics to lead vocal. (Stems mode only.) |

At least one of `--stems`, `--csv`, or `--refresh-stems` must be provided.

### Generating a Spotify CSV

The `--csv` mode expects a CSV exported from Spotify. Use [Exportify](https://exportify.net/) to export any of your Spotify playlists as a CSV file with the required columns (`Track Name`, `Artist Name(s)`, `Duration (ms)`, `Tempo`, etc.).

### Examples

Import stems:

```bash
python st4_import.py MyBackup.st4b --stems ./Stems
```

Bulk import songs from a Spotify-export CSV:

```bash
python st4_import.py MyBackup.st4b --csv songs.csv
```

Import both stems and CSV songs in one pass:

```bash
python st4_import.py MyBackup.st4b --stems ./Stems --csv songs.csv
```

Specify a custom output file:

```bash
python st4_import.py MyBackup.st4b --stems ./Stems -o NewBackup.st4b
```

Dry run to preview stem import without modifying anything:

```bash
python st4_import.py MyBackup.st4b --stems ./Stems --dry-run
```

Keep WAV files without converting to MP3:

```bash
python st4_import.py MyBackup.st4b --stems ./Stems --no-convert
```

Refresh stems after editing them (replaces audio only, metadata unchanged):

```bash
python st4_import.py MyBackup.st4b --refresh-stems ./Stems
```
