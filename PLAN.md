# StageTraxx4 Stem Importer - Implementation Plan

## Context

The user has a collection of WAV stem files (backing tracks) in a `Stems/` directory and an existing StageTraxx4 backup file (`.st4b`, which is a ZIP). They need a Python CLI tool that reads the stems, groups them by song, looks up metadata (artist, lyrics, BPM), and produces a new `.st4b` backup with those songs added.

## Single file: `st4_import.py` (Python, no external dependencies)

### CLI Interface

```
python3 st4_import.py <input.st4b> [--stems <dir>] [-o <output.st4b>] [--dry-run]
```

- `input.st4b` - existing backup file (positional, required)
- `--stems` - stems directory (default: `./stems` or `./Stems`)
- `-o` / `--output` - output file (default: `<input>_imported.st4b`)
- `--dry-run` - preview without writing

### Processing Flow

1. **Parse stem filenames** - Pattern: `SongName_XX_StemName.wav` → group by song name, sorted by track number
2. **Detect duplicates** - Normalize titles (strip apostrophes) and compare against existing backup songs. **Prompt user interactively**: skip or replace each duplicate
3. **For each new song:**
   - Generate UUID (uppercase with hyphens)
   - Look up **artist** via MusicBrainz API (free, no key, 1 req/sec rate limit)
   - Look up **lyrics** via LRCLIB API (`lrclib.net` - free, returns synced LRC lyrics when available)
   - Get **release year** from MusicBrainz response
   - Detect **BPM** from click track WAV (peak interval analysis)
   - Set **duration** from longest track
4. **For each stem WAV in the song** (sorted alphabetically):
   - Check if silent (all samples below threshold) → skip if so
   - Generate track UUID
   - Set `filePath`: `SongTitle__<first6 of songID>/SongName_XX_StemName.wav`
   - Assign **bus** using per-stem 10-bus mapping (matching existing backup pattern)
   - Set `name` from stem name portion of filename
   - Set `number` as sequential (1, 2, 3...)
   - Read WAV duration
5. **Build output .st4b** - Copy existing ZIP entries, append new song directories with WAVs, write updated `backup_data.json`

### Bus Mapping (per-stem, matching existing backup)

| Stem Name | Bus | Existing Bus Name |
|-----------|-----|-------------------|
| Click | 0 | Click |
| Drums | 1 | Drums |
| Percussion | 2 | Perc. |
| Bass | 3 | Bass |
| Guitar / Electric_Rhythm / Electric_Lead | 4 | Guitar |
| Keys | 5 | Keys |
| Pad | 6 | Pad |
| Vocals_BG | 7 | BG Vocal |
| Vocals_Lead | 8 | Lead Voc |
| Cues | 9 | Cues |

Unrecognized stem names default to bus 2.

### Key Technical Details

- **BPM detection**: Read click track WAV, find peaks above 50% max amplitude, compute median inter-peak interval, convert to BPM
- **Silence detection**: Read WAV in chunks, return silent if all samples < 0.1% of max value
- **WAV duration**: `frames / sample_rate` via Python `wave` module
- **24-bit WAV support**: Pad 3-byte samples to 4-byte int32 for unpacking
- **JSON formatting**: `json.dumps(sort_keys=True, indent=2)` - StageTraxx4 uses sorted keys
- **ZIP compression**: `ZIP_STORED` (WAVs don't compress, matches existing backup)
- **UUIDs**: `str(uuid.uuid4()).upper()` to match existing format
- **Timestamps**: ISO 8601 with `Z` suffix
- **Title correction**: Use MusicBrainz canonical title when available (restores apostrophes like "I Don't Want To Know")

### Song/Track JSON Templates

Song fields (from existing backup): `id, title, artist, added, lastModified, duration, startTime, endTime, lyrics, fontSize(21), bpm, pitch(0), tune(0), speed(1), volume(0), fadeIn(0), fadeOut(0), chordTranspose(0), pitchToChords(false), scrollSpeed(1.2), metronomeMode(0), metronomeType(2), color(0), playCount(0), timecodeOffset(0)`

Track fields: `id, songID, number, filePath, bus, duration, volume(0), pan(0), mute(false), transpose(true), muteGroupMask(0), color(12), name, hasMarkers(false), lastModified, equalizer(default 4-band flat EQ)`

### Critical Files

- `/Users/johnhayden/Music/st4-importer/StageTraxx4_Full_2026-03-01_20-00/backup_data.json` - Reference JSON schema (4545 lines)
- `/Users/johnhayden/Music/st4-importer/Stems/` - 80 WAV files across 9 songs (8 existing + "You Wreck Me" is new)
- `/Users/johnhayden/Music/st4-importer/StageTraxx4_Full_2026-03-01_20-00.st4b` - Reference .st4b backup (2.3GB ZIP)

### Verification

1. Run `python3 st4_import.py StageTraxx4_Full_2026-03-01_20-00.st4b --stems Stems --dry-run` - should list 9 songs found, prompt about 8 duplicates, show "You Wreck Me" as new
2. Run without `--dry-run` to produce output .st4b
3. Unzip output and diff `backup_data.json` against original - verify new song + tracks added with correct fields
4. Verify "You Wreck Me" directory exists in ZIP with non-silent WAV files
5. Verify BPM was detected from click track
6. Check that artist/lyrics were fetched (may need internet)
7. Load the output .st4b in StageTraxx4 app to confirm it works
