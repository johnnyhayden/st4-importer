#!/usr/bin/env python3
"""StageTraxx4 Stem Importer - imports WAV stems into a .st4b backup file."""

import argparse
import csv
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import wave
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import lyricsgenius

# ── Bus mapping ──────────────────────────────────────────────────────────────

STEM_BUS_MAP = {
    "Click": 0,
    "Drums": 1,
    "Percussion": 2,
    "Bass": 3,
    "Guitar": 4,
    "Electric_Rhythm": 4,
    "Electric_Lead": 4,
    "Keys": 5,
    "Pad": 6,
    "Vocals_BG": 7,
    "Vocals_Lead": 8,
    "Cues": 9,
}
DEFAULT_BUS = 2

# ── Default EQ (flat 4-band) ────────────────────────────────────────────────

DEFAULT_EQ = {
    "bands": [
        {"bw": 1, "freq": 100, "gain": 0, "label": "LO", "type": 1},
        {"bw": 1, "freq": 500, "gain": 0, "label": "LM", "type": 0},
        {"bw": 2, "freq": 2000, "gain": 0, "label": "HM", "type": 0},
        {"bw": 1, "freq": 10000, "gain": 0, "label": "HI", "type": 2},
    ],
    "bypass": False,
}

# ── FFmpeg / conversion helpers ──────────────────────────────────────────────


def ffmpeg_available():
    """Return True if ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


def aeneas_available():
    """Return True if aeneas and espeak are available."""
    try:
        from aeneas.executetask import ExecuteTask
        from aeneas.task import Task
    except ImportError:
        return False
    return shutil.which("espeak") is not None or shutil.which("espeak-ng") is not None


def _format_lrc_timestamp(seconds):
    """Convert float seconds to [MM:SS.cc] LRC timestamp."""
    minutes = int(seconds) // 60
    secs = seconds - minutes * 60
    return f"[{minutes:02d}:{secs:05.2f}]"


def align_lyrics_to_audio(lyrics_text, audio_wav_path):
    """Align lyrics to audio using aeneas forced alignment. Returns LRC string or None."""
    try:
        from aeneas.executetask import ExecuteTask
        from aeneas.task import Task

        lines = lyrics_text.split("\n")
        # Track which lines are non-blank (for alignment) and which are blank (preserved as-is)
        non_blank_lines = []
        non_blank_indices = []
        for i, line in enumerate(lines):
            if line.strip():
                non_blank_lines.append(line)
                non_blank_indices.append(i)

        if not non_blank_lines:
            return None

        # Write non-blank lines to a temp file for aeneas
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tf:
            tf.write("\n".join(non_blank_lines))
            text_path = tf.name

        try:
            config = "task_language=eng|is_text_type=plain|os_task_file_format=json"
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as out_f:
                out_path = out_f.name

            task = Task(config_string=config)
            task.audio_file_path_absolute = audio_wav_path
            task.text_file_path_absolute = text_path
            task.sync_map_file_path_absolute = out_path
            ExecuteTask(task).execute()
            task.output_sync_map_file()

            with open(out_path, "r", encoding="utf-8") as f:
                sync_data = json.load(f)

            fragments = sync_data.get("fragments", [])
            if len(fragments) != len(non_blank_lines):
                return None

            # Build timestamp map: original line index -> timestamp
            timestamp_map = {}
            for idx, frag in zip(non_blank_indices, fragments):
                begin = float(frag.get("begin", 0))
                timestamp_map[idx] = begin

            # Reconstruct full lyrics with timestamps on text lines, blank lines preserved
            result_lines = []
            for i, line in enumerate(lines):
                if i in timestamp_map:
                    result_lines.append(f"{_format_lrc_timestamp(timestamp_map[i])}{line}")
                else:
                    result_lines.append(line)

            return "\n".join(result_lines)
        finally:
            try:
                os.unlink(text_path)
            except OSError:
                pass
            try:
                os.unlink(out_path)
            except OSError:
                pass
    except Exception as e:
        print(f"  [Aeneas alignment error: {e}]")
        return None


def convert_wav_to_mp3(wav_path, bitrate="192k"):
    """Convert a WAV file to MP3 using ffmpeg. Returns path to a temp MP3 file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-b:a", bitrate,
            tmp.name,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    return tmp.name


# ── WAV helpers ──────────────────────────────────────────────────────────────


def wav_duration(path):
    """Return duration in seconds of a WAV file."""
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _unpack_samples(raw, sampwidth):
    """Unpack raw WAV bytes into integer samples, supporting 24-bit."""
    if sampwidth == 3:
        # 24-bit: pad each 3-byte sample to 4 bytes and unpack as int32
        import array

        n_samples = len(raw) // 3
        padded = bytearray(n_samples * 4)
        for i in range(n_samples):
            src = i * 3
            dst = i * 4
            padded[dst] = 0
            padded[dst + 1] = raw[src]
            padded[dst + 2] = raw[src + 1]
            padded[dst + 3] = raw[src + 2]
        # Unpack as signed 32-bit (values are shifted left by 8 bits)
        samples = struct.unpack(f"<{n_samples}i", padded)
        return list(samples)
    fmt = {1: "b", 2: "<h", 4: "<i"}.get(sampwidth)
    if fmt is None:
        return []
    return list(struct.unpack(f"{len(raw) // sampwidth}{fmt[-1]}", raw))


def wav_is_silent(path, threshold_ratio=0.001):
    """Return True if the WAV file is effectively silent."""
    with wave.open(str(path), "rb") as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        n_frames = w.getnframes()
        if n_frames == 0:
            return True

        max_possible = (1 << (sampwidth * 8 - 1)) - 1
        threshold = max_possible * threshold_ratio
        chunk_size = 44100 * n_channels  # ~1 second at a time
        while True:
            raw = w.readframes(chunk_size)
            if not raw:
                break
            samples = _unpack_samples(raw, sampwidth)
            if not samples:
                return False
            if any(abs(s) > threshold for s in samples):
                return False
    return True


def detect_bpm(path):
    """Detect BPM from a click track by analyzing peak intervals."""
    with wave.open(str(path), "rb") as w:
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        n_frames = w.getnframes()

        raw = w.readframes(n_frames)
        samples = _unpack_samples(raw, sampwidth)
        if not samples:
            return 0

        # Mix to mono by taking every n_channels-th sample (first channel)
        if n_channels > 1:
            samples = samples[::n_channels]

        max_val = max(abs(s) for s in samples) if samples else 0
        if max_val == 0:
            return 0

        threshold = max_val * 0.5

        # Find peaks: samples above threshold with at least min_gap between them
        min_gap = int(framerate * 0.15)  # 150ms minimum gap between beats
        peak_positions = []
        i = 0
        while i < len(samples):
            if abs(samples[i]) > threshold:
                peak_positions.append(i)
                i += min_gap  # skip ahead
            else:
                i += 1

        if len(peak_positions) < 2:
            return 0

        # Compute intervals between consecutive peaks
        intervals = [
            peak_positions[j + 1] - peak_positions[j]
            for j in range(len(peak_positions) - 1)
        ]

        # Use median interval for robustness
        intervals.sort()
        median_interval = intervals[len(intervals) // 2]

        if median_interval == 0:
            return 0

        bpm = 60.0 * framerate / median_interval
        return round(bpm)


# ── Filename parsing ─────────────────────────────────────────────────────────


def parse_stem_filename(filename):
    """Parse 'SongName_XX_StemName.wav' → (song_name, track_num, stem_name)."""
    base = filename.rsplit(".", 1)[0]  # strip .wav
    # Match: everything up to _XX_ where XX is digits, then stem name
    m = re.match(r"^(.+?)_(\d+)_(.+)$", base)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def normalize_title(title):
    """Normalize for comparison: lowercase, strip apostrophes and special chars."""
    t = title.lower()
    t = t.replace("\u2019", "").replace("'", "").replace("\u2018", "")
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ── API lookups ──────────────────────────────────────────────────────────────

# Preferred artist search order for metadata lookups.
METADATA_ARTIST_ORDER = [
    "Fleetwood Mac",
    "Stevie Nicks",
    "Tom Petty",
    "Tom Petty and the Heartbreakers",
]

# ── Genius lyrics config ─────────────────────────────────────────────────────

GENIUS_ACCESS_TOKEN = "NfWHIqz-VIwU2H9PwMpusGa8yuCL9-Q3ra4nCtqu3MlJvAt8R6c2GOZSu05OxgTB"

LYRICS_ARTIST_ORDER = [
    "Fleetwood Mac",
    "Stevie Nicks",
    "Tom Petty",
    "Tom Petty and the Heartbreakers",
]

_genius_client = None


def _get_genius():
    global _genius_client
    if _genius_client is None:
        _genius_client = lyricsgenius.Genius(
            GENIUS_ACCESS_TOKEN, verbose=False,
            remove_section_headers=True, skip_non_songs=True, retries=2,
        )
    return _genius_client


def lookup_itunes(title, artist=None):
    """Look up artist and canonical title via iTunes Search API.

    If artist is provided, tries that artist first (strict match), then falls
    back to METADATA_ARTIST_ORDER and finally an unqualified search.
    Otherwise tries each artist in METADATA_ARTIST_ORDER, returning the first match.
    Falls back to an unqualified search if no artist-specific match is found.
    """
    norm_query = normalize_title(title)

    def _search(term, search_artist=None):
        params = quote(f"{search_artist} {term}" if search_artist else term)
        url = f"https://itunes.apple.com/search?term={params}&media=music&entity=song&limit=10"
        req = Request(url)
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("results", [])

    def _best_match(results, strict=False):
        """Return the result whose trackName best matches the query title.

        If strict, only return an exact normalized match (used for per-artist
        searches so we don't grab an unrelated song by the same artist).
        """
        for r in results:
            if normalize_title(r.get("trackName", "")) == norm_query:
                return r
        if not strict:
            return results[0] if results else None
        return None

    def _extract(result):
        found_artist = result.get("artistName", "")
        canonical = result.get("trackName", title)
        year = None
        release_date = result.get("releaseDate", "")
        if release_date:
            year = release_date[:4]
        return found_artist, canonical, year

    # If a specific artist was provided, try that first (strict match)
    if artist:
        try:
            results = _search(title, artist)
            match = _best_match(results, strict=True)
            if match:
                return _extract(match)
        except (URLError, OSError, json.JSONDecodeError, KeyError) as e:
            print(f"  [iTunes lookup failed for '{artist}': {e}]")

    # Try each preferred artist in order (strict: title must match)
    for preferred_artist in METADATA_ARTIST_ORDER:
        if preferred_artist == artist:
            continue  # already tried above
        try:
            results = _search(title, preferred_artist)
            match = _best_match(results, strict=True)
            if match:
                return _extract(match)
        except (URLError, OSError, json.JSONDecodeError, KeyError) as e:
            print(f"  [iTunes lookup failed for '{preferred_artist}': {e}]")

    # Fallback: search with just the title
    try:
        results = _search(title)
        match = _best_match(results)
        if match:
            return _extract(match)
    except (URLError, OSError, json.JSONDecodeError, KeyError) as e:
        print(f"  [iTunes lookup failed: {e}]")

    return None, None, None


def _clean_genius_lyrics(lyrics):
    """Clean Genius formatting artifacts from lyrics text."""
    if not lyrics:
        return ""
    # Remove the title/artist header line Genius prepends (e.g. "Song Title Lyrics")
    lines = lyrics.split("\n")
    if lines and lines[0].endswith("Lyrics"):
        lines = lines[1:]
    text = "\n".join(lines)
    # Remove preamble up to and including "Read More" (contributor counts, descriptions)
    text = re.sub(r"(?si)^.*?Read More\s*", "", text)
    # Remove contributor count lines (e.g. "7 Contributors") that may appear without "Read More"
    text = re.sub(r"(?m)^\d+\s+Contributors?\s*\n?", "", text)
    # Remove trailing "Embed" or "...Embed" text Genius appends
    text = re.sub(r"\d*Embed$", "", text).rstrip()
    return text


def lookup_lyrics(title, artist=""):
    """Look up lyrics via Genius API, trying the given artist first, then preferred artists."""
    genius = _get_genius()
    # Build list of artists to try: given artist first, then preferred order
    artists_to_try = []
    if artist:
        artists_to_try.append(artist)
    for a in LYRICS_ARTIST_ORDER:
        if a not in artists_to_try:
            artists_to_try.append(a)

    for try_artist in artists_to_try:
        try:
            song = genius.search_song(title, try_artist)
            if song:
                cleaned = _clean_genius_lyrics(song.lyrics)
                if cleaned:
                    return cleaned
        except Exception as e:
            print(f"  [Genius lookup failed for '{try_artist}': {e}]")

    # Last resort: search with no artist
    try:
        song = genius.search_song(title)
        if song:
            cleaned = _clean_genius_lyrics(song.lyrics)
            if cleaned:
                return cleaned
    except Exception as e:
        print(f"  [Genius lookup failed: {e}]")

    return ""


# ── Song/track builders ─────────────────────────────────────────────────────


def make_song(song_id, title, artist, duration, bpm, lyrics):
    """Build a song dict matching StageTraxx4 schema."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "added": now,
        "artist": artist,
        "bpm": bpm,
        "chordTranspose": 0,
        "color": 0,
        "duration": duration,
        "endTime": duration,
        "fadeIn": 0,
        "fadeOut": 0,
        "fontSize": 21,
        "id": song_id,
        "lastModified": now,
        "lyrics": lyrics,
        "metronomeMode": 0,
        "metronomeType": 2,
        "pitch": 0,
        "pitchToChords": False,
        "playCount": 0,
        "scrollSpeed": 1.2,
        "speed": 1,
        "startTime": 0,
        "timecodeOffset": 0,
        "title": title,
        "tune": 0,
        "volume": 0,
    }


def make_track(track_id, song_id, number, file_path, bus, duration, name):
    """Build a track dict matching StageTraxx4 schema."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "bus": bus,
        "color": 12,
        "duration": duration,
        "equalizer": DEFAULT_EQ,
        "filePath": file_path,
        "hasMarkers": False,
        "id": track_id,
        "lastModified": now,
        "mute": False,
        "muteGroupMask": 0,
        "name": name,
        "number": number,
        "pan": 0,
        "songID": song_id,
        "transpose": True,
        "volume": 0,
    }


# ── CSV bulk import ──────────────────────────────────────────────────────────


def calculate_scroll_speed(lyrics, duration_seconds):
    """Calculate scroll speed based on lyrics density (chars per second).

    Returns a value between 0.5 and 2.0, defaulting to 1.2 if no lyrics.
    """
    if not lyrics or duration_seconds <= 0:
        return 1.2
    density = len(lyrics) / duration_seconds
    # Linear map: density 2 -> 0.8, density 12 -> 1.5
    speed = 0.8 + (density - 2) * (1.5 - 0.8) / (12 - 2)
    return round(max(0.5, min(2.0, speed)), 2)


def bulk_import_csv(csv_path, backup_json):
    """Import songs from a Spotify-export CSV into the backup JSON.

    Creates song entries only (no audio tracks), enriched with lyrics and BPM.
    """
    existing_songs = backup_json.get("songs", [])
    existing_norm_titles = {normalize_title(s["title"]) for s in existing_songs}

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"CSV contains {len(rows)} row(s)\n")
    added = 0
    skipped = 0

    for i, row in enumerate(rows, 1):
        title = row.get("Track Name", "").strip()
        csv_artist = row.get("Artist Name(s)", "").strip()
        duration_ms_str = row.get("Duration (ms)", "0")
        tempo_str = row.get("Tempo", "0")

        if not title:
            print(f"  [{i}/{len(rows)}] Skipping row with empty title")
            skipped += 1
            continue

        # Duplicate detection
        norm = normalize_title(title)
        if norm in existing_norm_titles:
            print(f"  [{i}/{len(rows)}] SKIP (duplicate): \"{title}\"")
            skipped += 1
            continue

        print(f"  [{i}/{len(rows)}] \"{title}\" by {csv_artist}")

        # Duration
        try:
            duration_ms = int(float(duration_ms_str))
        except (ValueError, TypeError):
            duration_ms = 0
        duration_seconds = duration_ms / 1000.0

        # BPM from CSV Tempo column
        try:
            bpm = round(float(tempo_str))
        except (ValueError, TypeError):
            bpm = 0

        # iTunes lookup with CSV artist hint
        print("    Looking up on iTunes...")
        itunes_artist, canonical_title, year = lookup_itunes(title, artist=csv_artist)
        if itunes_artist:
            print(f"    Artist: {itunes_artist}")
        else:
            itunes_artist = csv_artist
            print(f"    Artist: {csv_artist} (from CSV)")

        if canonical_title and canonical_title != title:
            print(f"    Canonical title: {canonical_title}")
        else:
            canonical_title = title

        # Lyrics lookup using CSV artist directly
        print("    Looking up lyrics on Genius...")
        lyrics = lookup_lyrics(canonical_title, csv_artist)
        if lyrics:
            print(f"    Lyrics: found ({len(lyrics)} chars)")
        else:
            print("    Lyrics: (not found)")

        # Scroll speed
        scroll_speed = calculate_scroll_speed(lyrics, duration_seconds)

        # Build song entry
        song_id = str(uuid.uuid4()).upper()
        song_entry = make_song(
            song_id=song_id,
            title=canonical_title,
            artist=itunes_artist,
            duration=duration_seconds,
            bpm=bpm,
            lyrics=lyrics,
        )
        song_entry["scrollSpeed"] = scroll_speed
        print(f"    BPM: {bpm}, Duration: {duration_seconds:.1f}s, Scroll: {scroll_speed}")

        backup_json.setdefault("songs", []).append(song_entry)
        existing_norm_titles.add(normalize_title(canonical_title))
        added += 1

    print(f"\nCSV import complete: {added} added, {skipped} skipped")
    return added


# ── Main ─────────────────────────────────────────────────────────────────────


def find_stems_dir():
    """Find stems directory (case-insensitive)."""
    for name in ["Stems", "stems", "STEMS"]:
        p = Path(name)
        if p.is_dir():
            return p
    return None


def group_stems(stems_dir):
    """Group stem files by song name. Returns {song_name: [(num, stem_name, path), ...]}."""
    songs = defaultdict(list)
    for f in sorted(stems_dir.iterdir()):
        if not f.name.lower().endswith(".wav"):
            continue
        parsed = parse_stem_filename(f.name)
        if parsed is None:
            print(f"  Warning: Could not parse filename: {f.name}")
            continue
        song_name, track_num, stem_name = parsed
        songs[song_name].append((track_num, stem_name, f))
    # Sort each song's stems by track number
    for name in songs:
        songs[name].sort(key=lambda x: x[0])
    return dict(songs)


def main():
    parser = argparse.ArgumentParser(
        description="Import WAV stems or CSV song list into a StageTraxx4 backup (.st4b)"
    )
    parser.add_argument("input", help="Existing .st4b backup file")
    parser.add_argument(
        "--csv", type=Path, default=None,
        help="CSV file for bulk song import (mutually exclusive with --stems)",
    )
    parser.add_argument(
        "--stems", type=Path, default=None, help="Stems directory (default: ./Stems)"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="Output .st4b file (default: <input>_imported.st4b)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing"
    )
    parser.add_argument(
        "--no-convert", action="store_true",
        help="Keep original WAV files instead of converting to 192kbps MP3",
    )
    parser.add_argument(
        "--no-align", action="store_true",
        help="Skip forced alignment of lyrics to lead vocal",
    )
    args = parser.parse_args()

    if args.csv and args.stems:
        print("Error: --csv and --stems are mutually exclusive.")
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(
            input_path.stem + "_imported" + input_path.suffix
        )

    # ── CSV bulk import mode ────────────────────────────────────────────
    if args.csv:
        if not args.csv.exists():
            print(f"Error: CSV file not found: {args.csv}")
            sys.exit(1)

        print(f"Reading backup: {input_path}")
        with zipfile.ZipFile(str(input_path), "r") as zin:
            backup_json = json.loads(zin.read("backup_data.json"))

        added = bulk_import_csv(str(args.csv), backup_json)

        if added == 0:
            print("No new songs to add.")
            sys.exit(0)

        # Update metadata timestamp
        backup_json["metadata"]["created"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        updated_json = json.dumps(backup_json, sort_keys=True, indent=2, ensure_ascii=False)

        print(f"\nWriting output: {output_path}")
        with zipfile.ZipFile(str(output_path), "w", zipfile.ZIP_DEFLATED) as zout:
            # Copy existing entries except backup_data.json
            with zipfile.ZipFile(str(input_path), "r") as zin:
                for item in zin.infolist():
                    if item.filename == "backup_data.json":
                        continue
                    data = zin.read(item.filename)
                    zout.writestr(item, data)
            # Write updated backup_data.json
            zout.writestr("backup_data.json", updated_json)

        print(f"\nDone! Added {added} song(s).")
        print(f"Output: {output_path}")
        return

    # ── Stems import mode ───────────────────────────────────────────────
    # Determine whether to convert WAV→MP3
    do_convert = not args.no_convert
    if do_convert and not ffmpeg_available():
        print("Warning: ffmpeg not found on PATH — skipping MP3 conversion (keeping WAV)")
        do_convert = False

    do_align = not args.no_align
    if do_align and not aeneas_available():
        print("Warning: aeneas/espeak not found — skipping lyric alignment")
        do_align = False

    stems_dir = args.stems or find_stems_dir()
    if stems_dir is None or not stems_dir.is_dir():
        print(f"Error: Stems directory not found. Use --stems to specify.")
        sys.exit(1)

    # ── Read existing backup ─────────────────────────────────────────────
    print(f"Reading backup: {input_path}")
    with zipfile.ZipFile(str(input_path), "r") as zin:
        backup_json = json.loads(zin.read("backup_data.json"))

    existing_songs = backup_json.get("songs", [])
    existing_tracks = backup_json.get("tracks", [])
    existing_norm_titles = {normalize_title(s["title"]): s for s in existing_songs}

    # ── Scan stems ───────────────────────────────────────────────────────
    print(f"Scanning stems: {stems_dir}")
    stem_groups = group_stems(stems_dir)
    print(f"Found {len(stem_groups)} song(s): {', '.join(sorted(stem_groups.keys()))}\n")

    # ── Detect duplicates ────────────────────────────────────────────────
    new_songs_to_add = {}  # song_name -> stems list
    songs_to_replace = {}  # song_name -> existing song dict

    for song_name in sorted(stem_groups.keys()):
        norm = normalize_title(song_name)
        if norm in existing_norm_titles:
            existing = existing_norm_titles[norm]
            if args.dry_run:
                print(f"  DUPLICATE: \"{song_name}\" matches existing \"{existing['title']}\" — would prompt")
                continue

            print(f"  DUPLICATE: \"{song_name}\" matches existing \"{existing['title']}\"")
            while True:
                choice = input(f"    [s]kip or [r]eplace? ").strip().lower()
                if choice in ("s", "skip"):
                    print(f"    Skipping \"{song_name}\"")
                    break
                elif choice in ("r", "replace"):
                    print(f"    Will replace \"{existing['title']}\"")
                    songs_to_replace[song_name] = existing
                    new_songs_to_add[song_name] = stem_groups[song_name]
                    break
                else:
                    print("    Please enter 's' or 'r'")
        else:
            new_songs_to_add[song_name] = stem_groups[song_name]

    if not new_songs_to_add:
        print("\nNo new songs to import.")
        sys.exit(0)

    if args.dry_run:
        # Show what would be new
        for song_name in sorted(stem_groups.keys()):
            norm = normalize_title(song_name)
            if norm not in existing_norm_titles:
                stems = stem_groups[song_name]
                print(f"\n  NEW: \"{song_name}\" ({len(stems)} stems)")
                for num, stem_name, path in stems:
                    bus = STEM_BUS_MAP.get(stem_name, DEFAULT_BUS)
                    print(f"    {num:02d} {stem_name} → bus {bus + 1}")
        print(f"\nDry run complete. Would write to: {output_path}")
        sys.exit(0)

    # ── Remove replaced songs/tracks ─────────────────────────────────────
    replaced_song_ids = set()
    replaced_dir_prefixes = set()
    for song_name, existing_song in songs_to_replace.items():
        sid = existing_song["id"]
        replaced_song_ids.add(sid)
        # Find the directory prefix used in existing tracks
        for t in existing_tracks:
            if t["songID"] == sid:
                dir_prefix = t["filePath"].split("/")[0]
                replaced_dir_prefixes.add(dir_prefix)
                break

    filtered_songs = [s for s in existing_songs if s["id"] not in replaced_song_ids]
    filtered_tracks = [t for t in existing_tracks if t["songID"] not in replaced_song_ids]

    # Also filter junction/child tables that reference replaced songs
    if replaced_song_ids:
        for key in ("playlistSongs", "songKeywords", "regions"):
            if key in backup_json:
                backup_json[key] = [
                    r for r in backup_json[key] if r.get("songID") not in replaced_song_ids
                ]

    # ── Process each new song ────────────────────────────────────────────
    new_song_entries = []
    new_track_entries = []
    new_wav_files = []  # (zip_path, local_path)
    temp_mp3_files = []  # temp files to clean up after zip is written

    for song_name in sorted(new_songs_to_add.keys()):
        stems = new_songs_to_add[song_name]
        song_id = str(uuid.uuid4()).upper()
        dir_prefix_id = song_id[:6].lower()

        print(f"\nProcessing: \"{song_name}\"")

        # Look up metadata
        print("  Looking up artist/title on iTunes...")
        artist, canonical_title, year = lookup_itunes(song_name)
        if artist:
            print(f"  Artist: {artist}")
        else:
            artist = ""
            print("  Artist: (not found)")

        if canonical_title and canonical_title != song_name:
            print(f"  Canonical title: {canonical_title}")
        else:
            canonical_title = song_name

        # Use canonical title for the directory name
        dir_name = f"{canonical_title}__{dir_prefix_id}"

        print("  Looking up lyrics on Genius...")
        lyrics = lookup_lyrics(canonical_title, artist)
        if lyrics:
            print(f"  Lyrics: found ({len(lyrics)} chars)")
        else:
            print("  Lyrics: (not found)")

        # Align lyrics to lead vocal
        if lyrics and do_align:
            lead_vocal_stems = [s for s in stems if s[1] == "Vocals_Lead"]
            if lead_vocal_stems:
                vocal_wav_path = lead_vocal_stems[0][2]
                print("  Aligning lyrics to lead vocal...")
                lrc_lyrics = align_lyrics_to_audio(lyrics, str(vocal_wav_path))
                if lrc_lyrics:
                    lyrics = lrc_lyrics
                    print(f"  Lyrics aligned ({lyrics.count(chr(10)) + 1} lines)")
                else:
                    print("  Alignment failed, using plain text")

        # Detect BPM from click track
        bpm = 0
        click_stems = [s for s in stems if s[1] == "Click"]
        if click_stems:
            click_path = click_stems[0][2]
            print(f"  Detecting BPM from click track...")
            bpm = detect_bpm(str(click_path))
            if bpm:
                print(f"  BPM: {bpm}")
            else:
                print("  BPM: (detection failed)")

        # Process individual stems
        max_duration = 0.0
        track_number = 0
        song_tracks = []
        song_wavs = []

        for num, stem_name, path in stems:
            # Check if silent
            if wav_is_silent(str(path)):
                print(f"  Skipping silent track: {path.name}")
                continue

            track_number += 1
            track_id = str(uuid.uuid4()).upper()
            dur = wav_duration(str(path))
            max_duration = max(max_duration, dur)

            bus = STEM_BUS_MAP.get(stem_name, DEFAULT_BUS)

            # Convert WAV→MP3 if enabled
            if do_convert:
                mp3_name = path.stem + ".mp3"
                zip_file_path = f"{dir_name}/{mp3_name}"
                mp3_path = convert_wav_to_mp3(str(path))
                temp_mp3_files.append(mp3_path)
                local_file = Path(mp3_path)
            else:
                zip_file_path = f"{dir_name}/{path.name}"
                local_file = path

            track = make_track(
                track_id=track_id,
                song_id=song_id,
                number=track_number,
                file_path=zip_file_path,
                bus=bus,
                duration=dur,
                name="",
            )
            song_tracks.append(track)
            song_wavs.append((zip_file_path, local_file))
            fmt = "mp3" if do_convert else "wav"
            print(f"  Track {track_number}: {stem_name} → bus {bus + 1} ({dur:.1f}s) [{fmt}]")

        if not song_tracks:
            print(f"  No non-silent tracks — skipping song")
            continue

        # Build song entry
        song_entry = make_song(
            song_id=song_id,
            title=canonical_title,
            artist=artist,
            duration=max_duration,
            bpm=bpm,
            lyrics=lyrics,
        )
        new_song_entries.append(song_entry)
        new_track_entries.extend(song_tracks)
        new_wav_files.extend(song_wavs)

    if not new_song_entries:
        print("\nNo songs to add after processing.")
        sys.exit(0)

    # ── Build output .st4b ───────────────────────────────────────────────
    print(f"\nWriting output: {output_path}")

    # Merge into backup data
    all_songs = filtered_songs + new_song_entries
    all_tracks = filtered_tracks + new_track_entries
    backup_json["songs"] = all_songs
    backup_json["tracks"] = all_tracks

    # Update metadata timestamp
    backup_json["metadata"]["created"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    updated_json = json.dumps(backup_json, sort_keys=True, indent=2, ensure_ascii=False)

    with zipfile.ZipFile(str(output_path), "w", zipfile.ZIP_DEFLATED) as zout:
        # Copy existing entries (except backup_data.json and replaced dirs)
        with zipfile.ZipFile(str(input_path), "r") as zin:
            for item in zin.infolist():
                if item.filename == "backup_data.json":
                    continue
                # Skip entries from replaced songs
                skip = False
                for prefix in replaced_dir_prefixes:
                    if item.filename.startswith(prefix + "/") or item.filename == prefix:
                        skip = True
                        break
                if skip:
                    continue
                # Preserve original entry metadata by copying ZipInfo
                data = zin.read(item.filename)
                zout.writestr(item, data)

        # Write new audio files
        for zip_path, local_path in new_wav_files:
            print(f"  Adding: {zip_path}")
            zout.write(str(local_path), zip_path)

        # Write updated backup_data.json
        zout.writestr("backup_data.json", updated_json)

    # Clean up temp MP3 files
    for tmp in temp_mp3_files:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    print(f"\nDone! Added {len(new_song_entries)} song(s) with {len(new_track_entries)} track(s).")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
