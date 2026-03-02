# StageTraxx4 Stem Importer

A Python command-line tool that imports WAV stem files into a StageTraxx4 backup (`.st4b`) file. It parses structured stem filenames, auto-detects metadata from iTunes and lyrics from Genius, detects BPM from click tracks, skips silent stems, and optionally converts WAV to MP3 — producing a ready-to-restore `.st4b` backup.

## What It Does

1. **Reads an existing `.st4b` backup** (a ZIP archive containing `backup_data.json` and audio files).
2. **Scans a directory of WAV stems** named in the format `SongName_XX_StemName.wav` (e.g., `Dreams_01_Click.wav`, `Dreams_02_Drums.wav`).
3. **Groups stems by song name**, then for each song:
   - Looks up the **artist and canonical title** via the iTunes Search API.
   - Fetches **lyrics** from the Genius API.
   - **Detects BPM** by analyzing peak intervals in the click track (if present).
   - **Skips silent stems** automatically.
   - **Converts WAV to 192kbps MP3** using ffmpeg (unless `--no-convert` is passed).
   - **Assigns each stem to a bus** based on a built-in name-to-bus mapping (Click, Drums, Bass, Guitar, Keys, Pad, Vocals, Cues, etc.).
4. **Detects duplicate songs** already in the backup and prompts to skip or replace.
5. **Writes a new `.st4b` file** with the imported songs merged into the existing backup data.

## Assumptions

- **Stem filename format**: Files must follow the pattern `SongName_XX_StemName.wav` where `XX` is a two-digit track number and `StemName` matches one of the recognized bus names. Underscores in the song name separate the components.
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

### Python Dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `lyricsgenius` — Python client for the Genius API (used for lyrics lookup)

## Usage

```bash
python st4_import.py <input.st4b> [options]
```

### Options

| Flag | Description |
|---|---|
| `--stems <dir>` | Path to the stems directory. Defaults to `./Stems`. |
| `-o, --output <file>` | Output `.st4b` file path. Defaults to `<input>_imported.st4b`. |
| `--dry-run` | Preview what would be imported without writing any files. |
| `--no-convert` | Keep original WAV files instead of converting to MP3. |

### Examples

Import stems from the default `./Stems` directory:

```bash
python st4_import.py MyBackup.st4b
```

Dry run to preview without modifying anything:

```bash
python st4_import.py MyBackup.st4b --dry-run
```

Specify a custom stems directory and output file:

```bash
python st4_import.py MyBackup.st4b --stems /path/to/stems -o NewBackup.st4b
```

Keep WAV files without converting to MP3:

```bash
python st4_import.py MyBackup.st4b --no-convert
```
