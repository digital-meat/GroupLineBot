"""
Band practice WAV splitter + normalizer.

Detects song boundaries by finding sustained low-volume regions
(not silence — just significantly quieter than the overall mean),
splits the recording at those points, and normalizes each segment.

Usage:
  - Colab: see practice_review.ipynb
  - CLI:   python split_and_normalize.py <input_dir> [output_dir]

Dependencies: numpy, ffmpeg (CLI)
"""

import subprocess
import re
import sys
import numpy as np
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────
MIN_LOW_SEC    = 4.0       # 低音量がこの秒数以上続いたら境界とみなす
MIN_TRACK_SEC  = 60.0      # これより短いセグメントは隣にマージ
PAD_SEC        = 0.5       # 切り出しの前後に残すパディング
MEAN_DROP_DB   = 25.0      # 全体平均からこのdB以上下がったら「低い」
TARGET_PEAK_DB = -1.0      # ノーマライズ目標ピーク
GAIN_CAP_DB    = 30.0      # ゲイン上限
WINDOW_SEC     = 0.5       # RMS計算の窓サイズ(秒)
ANALYSIS_SR    = 8000      # 解析用ダウンサンプルレート(メモリ節約)


# ── ffmpeg helpers ────────────────────────────────────────────────

def run_cmd(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr


def get_duration(path: Path) -> float:
    """Get audio duration in seconds via ffprobe."""
    rc, out, err = run_cmd([
        "ffprobe", "-hide_banner", "-v", "error",
        "-show_entries", "format=duration", "-of", "csv=p=0",
        str(path)
    ])
    return float(out.strip())


def volumedetect(path: Path):
    """Get max and mean volume in dB."""
    rc, out, err = run_cmd([
        "ffmpeg", "-hide_banner", "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-"
    ])
    m_max  = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", err)
    m_mean = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", err)
    max_db  = float(m_max.group(1)) if m_max else None
    mean_db = float(m_mean.group(1)) if m_mean else None
    return max_db, mean_db


def read_audio_for_analysis(path: Path) -> tuple[np.ndarray, int]:
    """Decode audio to mono float32 at low sample rate for envelope analysis.

    Uses ANALYSIS_SR (8kHz) to keep memory small even for long recordings.
    A 3-hour session at 8kHz = ~86M samples = ~345MB.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-v", "error",
        "-i", str(path),
        "-ac", "1",
        "-ar", str(ANALYSIS_SR),
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "pipe:1"
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {p.stderr.decode()}")
    samples = np.frombuffer(p.stdout, dtype=np.float32)
    return samples, ANALYSIS_SR


def wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: str = "192k"):
    """Convert WAV to MP3 for web streaming."""
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(wav_path),
        "-codec:a", "libmp3lame", "-b:a", bitrate,
        str(mp3_path)
    ]
    rc, _, err = run_cmd(cmd)
    if rc != 0:
        raise RuntimeError(f"MP3 conversion failed: {err}")
    return mp3_path


# ── Envelope & boundary detection ─────────────────────────────────

def compute_rms_envelope_db(samples: np.ndarray, sr: int, window_sec: float) -> np.ndarray:
    """Compute RMS envelope in dB."""
    win = int(sr * window_sec)
    n_windows = len(samples) // win
    if n_windows == 0:
        return np.array([])
    trimmed = samples[:n_windows * win].reshape(n_windows, win)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1))
    rms_db = 20.0 * np.log10(rms + 1e-10)
    return rms_db


def find_low_regions(rms_db: np.ndarray, window_sec: float,
                     mean_db: float, drop_db: float,
                     min_low_sec: float) -> list[tuple[float, float]]:
    """Find sustained low-volume regions.

    Returns list of (start_sec, end_sec) for regions where
    volume stays below (mean_db - drop_db) for at least min_low_sec.
    """
    threshold = mean_db - drop_db
    is_low = rms_db < threshold

    regions = []
    in_region = False
    start = 0

    for i in range(len(is_low)):
        if is_low[i] and not in_region:
            start = i
            in_region = True
        elif not is_low[i] and in_region:
            duration = (i - start) * window_sec
            if duration >= min_low_sec:
                regions.append((start * window_sec, i * window_sec))
            in_region = False

    if in_region:
        duration = (len(is_low) - start) * window_sec
        if duration >= min_low_sec:
            regions.append((start * window_sec, len(is_low) * window_sec))

    return regions


def regions_to_segments(low_regions: list[tuple[float, float]],
                        total_duration: float, pad_sec: float,
                        min_track_sec: float) -> list[tuple[float, float]]:
    """Convert low regions into segment boundaries (start, end) for each track.

    Split points are at the center of each low region.
    Short segments are merged with their neighbor.
    """
    if not low_regions:
        return [(0.0, total_duration)]

    split_points = [(r[0] + r[1]) / 2.0 for r in low_regions]

    boundaries = [0.0] + split_points + [total_duration]
    raw_segments = []
    for i in range(len(boundaries) - 1):
        s = boundaries[i]
        e = boundaries[i + 1]
        raw_segments.append((s, e))

    # Merge short segments with the next one
    merged = []
    for s, e in raw_segments:
        if merged and (merged[-1][1] - merged[-1][0]) < min_track_sec:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))

    # Check if the last segment is too short, merge with previous
    if len(merged) > 1 and (merged[-1][1] - merged[-1][0]) < min_track_sec:
        prev = merged[-2]
        last = merged[-1]
        merged[-2] = (prev[0], last[1])
        merged.pop()

    # Apply padding (shrink each segment slightly to avoid overlap noise)
    padded = []
    for i, (s, e) in enumerate(merged):
        ps = s + pad_sec if i > 0 else s
        pe = e - pad_sec if i < len(merged) - 1 else e
        if pe > ps:
            padded.append((ps, pe))

    return padded


# ── Splitting & normalization ─────────────────────────────────────

def compute_gain_db(max_db: float | None) -> float:
    if max_db is None:
        return 0.0
    g = TARGET_PEAK_DB - max_db
    return max(0.0, min(GAIN_CAP_DB, g))


def extract_and_normalize(src: Path, out: Path, start: float, end: float):
    """Extract a segment from src, normalize volume, write to out."""
    duration = end - start

    # First pass: detect volume of the segment
    rc, _, err = run_cmd([
        "ffmpeg", "-hide_banner",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(src),
        "-af", "volumedetect",
        "-f", "null", "-"
    ])
    m_max = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", err)
    max_db = float(m_max.group(1)) if m_max else None
    gain_db = compute_gain_db(max_db)

    # Second pass: extract + normalize
    af = f"volume={gain_db}dB,alimiter=limit=0.98"
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(src),
        "-af", af,
        "-acodec", "pcm_s16le",
        str(out)
    ]
    rc, _, err = run_cmd(cmd)
    if rc != 0:
        print(f"  ERROR extracting segment: {err}")
        return False
    return True


def normalize_file(src: Path, out: Path):
    """Normalize a single file (no splitting)."""
    max_db, mean_db = volumedetect(src)
    gain_db = compute_gain_db(max_db)
    print(f"  Max: {max_db} dB, Mean: {mean_db} dB, Gain: +{gain_db:.1f} dB")

    af = f"volume={gain_db}dB,alimiter=limit=0.98"
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(src),
        "-af", af,
        "-acodec", "pcm_s16le",
        str(out)
    ]
    rc, _, err = run_cmd(cmd)
    if rc != 0:
        print(f"  ERROR: {err}")
        return False
    return True


# ── Main processing ──────────────────────────────────────────────

def process_file(wav_path: Path, out_dir: Path, make_mp3: bool = True) -> dict:
    """Detect boundaries, split, normalize, and optionally create MP3s.

    Returns dict with:
      - duration_sec: total duration
      - segments: list of {track_number, start_sec, end_sec, wav_path, mp3_path}
      - mp3_path: full session MP3 (if make_mp3)
    """
    stem = wav_path.stem
    print(f"\n{'='*60}")
    print(f"Processing: {wav_path.name}")

    # 1. Get duration
    total_dur = get_duration(wav_path)
    print(f"  Duration: {total_dur/60:.1f} min")

    # 2. Normalize the whole file first
    normalized_path = out_dir / f"{stem}_normalized.wav"
    print("  Normalizing...")
    normalize_file(wav_path, normalized_path)

    # 3. Create MP3 of the full normalized session (for web streaming)
    session_mp3 = None
    if make_mp3:
        session_mp3 = out_dir / f"{stem}.mp3"
        print("  Creating MP3 for streaming...")
        wav_to_mp3(normalized_path, session_mp3)

    # 4. Read audio for analysis (low-res mono)
    print("  Analyzing volume envelope...")
    samples, sr = read_audio_for_analysis(normalized_path)
    rms_db = compute_rms_envelope_db(samples, sr, WINDOW_SEC)
    del samples

    if len(rms_db) == 0:
        print("  SKIP: file too short for analysis")
        return {"duration_sec": total_dur, "segments": [], "mp3_path": session_mp3}

    overall_mean_db = float(np.mean(rms_db))
    print(f"  Envelope mean: {overall_mean_db:.1f} dB")
    print(f"  Boundary threshold: < {overall_mean_db - MEAN_DROP_DB:.1f} dB for {MIN_LOW_SEC}s+")

    # 5. Find low-volume regions
    low_regions = find_low_regions(rms_db, WINDOW_SEC, overall_mean_db, MEAN_DROP_DB, MIN_LOW_SEC)
    del rms_db

    if not low_regions:
        print("  No song boundaries detected — single track")
    else:
        print(f"  Found {len(low_regions)} boundary region(s):")
        for i, (s, e) in enumerate(low_regions):
            print(f"    [{i+1}] {_fmt_time(s)} ~ {_fmt_time(e)}  ({e-s:.1f}s)")

    # 6. Build segments
    segments = regions_to_segments(low_regions, total_dur, PAD_SEC, MIN_TRACK_SEC)
    print(f"  → {len(segments)} segment(s)")

    # 7. Extract each segment
    result_segments = []
    for i, (s, e) in enumerate(segments):
        num = i + 1
        seg_wav = out_dir / f"{stem}_track{num:02d}.wav"
        dur = e - s

        print(f"  [Track {num:02d}] {_fmt_time(s)} ~ {_fmt_time(e)}  ({dur/60:.1f} min)")

        if extract_and_normalize(normalized_path, seg_wav, s, e):
            seg_info = {
                "track_number": num,
                "start_sec": s,
                "end_sec": e,
                "wav_path": seg_wav,
            }

            if make_mp3:
                seg_mp3 = out_dir / f"{stem}_track{num:02d}.mp3"
                wav_to_mp3(seg_wav, seg_mp3)
                seg_info["mp3_path"] = seg_mp3

            result_segments.append(seg_info)

    # Clean up intermediate normalized file
    try:
        normalized_path.unlink()
    except OSError:
        pass

    return {
        "duration_sec": total_dur,
        "segments": result_segments,
        "mp3_path": session_mp3,
    }


def _fmt_time(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def main():
    if len(sys.argv) < 2:
        print("Usage: python split_and_normalize.py <input_dir> [output_dir]")
        print("  input_dir:  WAV files to process")
        print("  output_dir: where to write splits (default: <input_dir>/split)")
        sys.exit(1)

    in_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) >= 3 else in_dir / "split"

    if not in_dir.exists():
        print(f"Error: input directory not found: {in_dir}")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    wavs = sorted(in_dir.glob("*.wav")) + sorted(in_dir.glob("*.WAV"))
    wavs = list({p.resolve(): p for p in wavs}.values())

    if not wavs:
        print(f"No WAV files found in {in_dir}")
        sys.exit(1)

    print(f"Found {len(wavs)} WAV file(s) in {in_dir}")
    print(f"Output: {out_dir}")
    print(f"Config: drop={MEAN_DROP_DB}dB, min_low={MIN_LOW_SEC}s, "
          f"min_track={MIN_TRACK_SEC}s, pad={PAD_SEC}s")

    for wav in wavs:
        process_file(wav, out_dir)

    print(f"\n{'='*60}")
    print("Done!")


if __name__ == "__main__":
    main()
