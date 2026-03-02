#!/usr/bin/env python3
"""StageTraxx4 Stem Importer - imports WAV stems into a .st4b backup file."""

import argparse
import json
import os
import re
import struct
import sys
import time
import uuid
import wave
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

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

MUSICBRAINZ_UA = "ST4Importer/1.0 (st4-importer)"
_last_mb_request = 0.0


def _mb_rate_limit():
    """Enforce 1 request/sec for MusicBrainz."""
    global _last_mb_request
    elapsed = time.time() - _last_mb_request
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_mb_request = time.time()


def lookup_musicbrainz(title):
    """Look up artist and canonical title via MusicBrainz recording search."""
    _mb_rate_limit()
    query = title.replace(" ", "+")
    url = f"https://musicbrainz.org/ws/2/recording/?query={query}&fmt=json&limit=5"
    req = Request(url, headers={"User-Agent": MUSICBRAINZ_UA})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        recordings = data.get("recordings", [])
        if not recordings:
            return None, None, None

        # Find best match by normalized title
        norm_query = normalize_title(title)
        best = recordings[0]
        for rec in recordings:
            if normalize_title(rec.get("title", "")) == norm_query:
                best = rec
                break

        artist = ""
        credits = best.get("artist-credit", [])
        if credits:
            parts = []
            for c in credits:
                parts.append(c.get("name", ""))
                joinphrase = c.get("joinphrase", "")
                if joinphrase:
                    parts.append(joinphrase)
            artist = "".join(parts)

        canonical_title = best.get("title", title)

        # Try to get release year
        year = None
        releases = best.get("releases", [])
        if releases:
            dates = [r.get("date", "") for r in releases if r.get("date")]
            if dates:
                dates.sort()
                year = dates[0][:4] if dates[0] else None

        return artist, canonical_title, year
    except (URLError, OSError, json.JSONDecodeError, KeyError) as e:
        print(f"  [MusicBrainz lookup failed: {e}]")
        return None, None, None


def lookup_lyrics(title, artist=""):
    """Look up lyrics via LRCLIB API."""
    params = f"track_name={title.replace(' ', '+')}"
    if artist:
        params += f"&artist_name={artist.replace(' ', '+')}"
    url = f"https://lrclib.net/api/search?{params}"
    req = Request(url, headers={"User-Agent": MUSICBRAINZ_UA})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if not data:
            return ""

        # Prefer synced lyrics, fall back to plain
        best = data[0]
        norm_query = normalize_title(title)
        for entry in data:
            if normalize_title(entry.get("trackName", "")) == norm_query:
                best = entry
                break

        synced = best.get("syncedLyrics", "")
        if synced:
            return synced
        return best.get("plainLyrics", "")
    except (URLError, OSError, json.JSONDecodeError) as e:
        print(f"  [Lyrics lookup failed: {e}]")
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
        description="Import WAV stems into a StageTraxx4 backup (.st4b)"
    )
    parser.add_argument("input", help="Existing .st4b backup file")
    parser.add_argument(
        "--stems", type=Path, default=None, help="Stems directory (default: ./Stems)"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="Output .st4b file (default: <input>_imported.st4b)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without writing"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    stems_dir = args.stems or find_stems_dir()
    if stems_dir is None or not stems_dir.is_dir():
        print(f"Error: Stems directory not found. Use --stems to specify.")
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(
            input_path.stem + "_imported" + input_path.suffix
        )

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
                    print(f"    {num:02d} {stem_name} → bus {bus}")
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

    # ── Process each new song ────────────────────────────────────────────
    new_song_entries = []
    new_track_entries = []
    new_wav_files = []  # (zip_path, local_path)

    for song_name in sorted(new_songs_to_add.keys()):
        stems = new_songs_to_add[song_name]
        song_id = str(uuid.uuid4()).upper()
        dir_prefix_id = song_id[:6].lower()

        print(f"\nProcessing: \"{song_name}\"")

        # Look up metadata
        print("  Looking up artist/title on MusicBrainz...")
        artist, canonical_title, year = lookup_musicbrainz(song_name)
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

        print("  Looking up lyrics on LRCLIB...")
        lyrics = lookup_lyrics(canonical_title, artist)
        if lyrics:
            print(f"  Lyrics: found ({len(lyrics)} chars)")
        else:
            print("  Lyrics: (not found)")

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
            # filePath uses original filename (without apostrophes) inside canonical dir
            zip_file_path = f"{dir_name}/{path.name}"

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
            song_wavs.append((zip_file_path, path))
            print(f"  Track {track_number}: {stem_name} → bus {bus} ({dur:.1f}s)")

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

    with zipfile.ZipFile(str(output_path), "w", zipfile.ZIP_STORED) as zout:
        # Copy existing entries (except backup_data.json and replaced dirs)
        with zipfile.ZipFile(str(input_path), "r") as zin:
            for item in zin.namelist():
                if item == "backup_data.json":
                    continue
                # Skip entries from replaced songs
                skip = False
                for prefix in replaced_dir_prefixes:
                    if item.startswith(prefix + "/") or item == prefix:
                        skip = True
                        break
                if skip:
                    continue
                data = zin.read(item)
                zout.writestr(item, data)

        # Write new WAV files
        for zip_path, local_path in new_wav_files:
            print(f"  Adding: {zip_path}")
            zout.write(str(local_path), zip_path)

        # Write updated backup_data.json
        zout.writestr("backup_data.json", updated_json)

    print(f"\nDone! Added {len(new_song_entries)} song(s) with {len(new_track_entries)} track(s).")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
