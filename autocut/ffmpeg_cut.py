import logging
import os
import subprocess
import tempfile


def _run_ffmpeg(cmd: list[str]) -> str:
    logging.info(f"ffmpeg cmd: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"ffmpeg not found. Please install ffmpeg and add it to PATH.")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (code {result.returncode}): {result.stderr}")
    return result.stdout


def _to_concat_path(path: str) -> str:
    return path.replace("\\", "/")


def _get_keyframes(input_path: str) -> list[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "frame=pts_time",
                "-of", "csv=p=0",
                "-skip_frame", "nokey",
                input_path,
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logging.warning("ffprobe not found, cannot detect keyframes")
        return []
    if result.returncode != 0:
        return []
    keyframes = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                keyframes.append(float(line))
            except ValueError:
                pass
    return keyframes


def _find_nearest_keyframe_before(keyframes: list[float], time: float) -> float:
    best = 0.0
    for kf in keyframes:
        if kf <= time:
            best = kf
        else:
            break
    return best


def _find_nearest_keyframe_after(keyframes: list[float], time: float) -> float:
    for kf in keyframes:
        if kf >= time:
            return kf
    return keyframes[-1] if keyframes else time


def _apply_transitions(seg_files, segments, is_video, tmpdir):
    """Apply fade/crossfade filters to segment files based on transition settings."""
    result_files = []
    for i, (seg_path, seg) in enumerate(zip(seg_files, segments)):
        transition = seg.get("transition", "cut")
        trans_dur = seg.get("transition_duration", 0.5) or 0.5
        prev_transition = segments[i - 1].get("transition", "cut") if i > 0 else "cut"

        needs_fade_in = i > 0 and prev_transition in ("fade", "crossfade")
        needs_fade_out = transition in ("fade", "crossfade")

        if not needs_fade_in and not needs_fade_out:
            result_files.append(seg_path)
            continue

        dur = seg["end"] - seg["start"]
        vfilters = []
        afilters = []

        if needs_fade_in:
            if is_video:
                vfilters.append(f"fade=t=in:st=0:d={trans_dur}")
            afilters.append(f"afade=t=in:st=0:d={trans_dur}")

        if needs_fade_out:
            fade_out_st = max(0, dur - trans_dur)
            if is_video:
                vfilters.append(f"fade=t=out:st={fade_out_st}:d={trans_dur}")
            afilters.append(f"afade=t=out:st={fade_out_st}:d={trans_dur}")

        ext = os.path.splitext(seg_path)[1]
        out_path = os.path.join(tmpdir, f"trans_{i:04d}{ext}")
        cmd = ["ffmpeg", "-y", "-i", seg_path]
        if is_video and vfilters:
            cmd += ["-vf", ",".join(vfilters)]
        if afilters:
            cmd += ["-af", ",".join(afilters)]
        if is_video:
            cmd += ["-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-c:a", "libmp3lame" if ext == ".mp3" else "aac"]
        cmd.append(out_path)
        _run_ffmpeg(cmd)
        result_files.append(out_path)

    return result_files


def cut_segments_stream_copy(
    input_path: str,
    output_path: str,
    segments: list[dict[str, float]],
    precise: bool = True,
) -> str:
    if not segments:
        raise ValueError("No segments to cut")

    ext = os.path.splitext(output_path)[1]

    if precise:
        return _cut_segments_precise(input_path, output_path, segments, ext)

    keyframes = _get_keyframes(input_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        seg_files = []
        for i, seg in enumerate(segments):
            duration = seg["end"] - seg["start"]
            if duration <= 0:
                logging.warning(f"Skipping segment {i}: non-positive duration {duration}")
                continue

            if keyframes:
                kf_before = _find_nearest_keyframe_before(keyframes, seg["start"])
                seek_start = kf_before
                seek_duration = seg["end"] - kf_before
            else:
                seek_start = seg["start"]
                seek_duration = duration

            seg_path = os.path.join(tmpdir, f"seg_{i:04d}{ext}")
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(seek_start),
                "-i", input_path,
                "-t", str(seek_duration),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                seg_path,
            ]
            _run_ffmpeg(cmd)
            seg_files.append(seg_path)

        if not seg_files:
            raise ValueError("All segments had non-positive duration")

        return _concat_segments(seg_files, output_path)


def _cut_segments_precise(
    input_path: str,
    output_path: str,
    segments: list[dict[str, float]],
    ext: str,
) -> str:
    is_video = ext in (".mp4", ".mov", ".mkv", ".avi", ".flv", ".f4v", ".webm")

    with tempfile.TemporaryDirectory() as tmpdir:
        seg_files = []
        for i, seg in enumerate(segments):
            duration = seg["end"] - seg["start"]
            if duration <= 0:
                logging.warning(f"Skipping segment {i}: non-positive duration {duration}")
                continue

            seg_path = os.path.join(tmpdir, f"seg_{i:04d}{ext}")
            if is_video:
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(seg["start"]),
                    "-i", input_path,
                    "-t", str(duration),
                    "-c:v", "libx264",
                    "-c:a", "aac",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    seg_path,
                ]
            else:
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(seg["start"]),
                    "-i", input_path,
                    "-t", str(duration),
                    "-c:a", "libmp3lame" if ext == ".mp3" else "aac",
                    seg_path,
                ]
            _run_ffmpeg(cmd)
            seg_files.append(seg_path)

        if not seg_files:
            raise ValueError("All segments had non-positive duration")

        # Apply fade/crossfade transitions if any segment has a non-cut transition
        has_transitions = any(
            s.get("transition", "cut") in ("fade", "crossfade") for s in segments
        )
        if has_transitions:
            seg_files = _apply_transitions(seg_files, segments, is_video, tmpdir)

        # Segments are already re-encoded with consistent parameters,
        # so concat with stream copy (no double re-encode).
        return _concat_segments(seg_files, output_path)


def _concat_segments(seg_files: list[str], output_path: str) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        concat_list = os.path.join(tmpdir, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for sf in seg_files:
                f.write(f"file '{_to_concat_path(sf)}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list,
            "-c", "copy",
            output_path,
        ]
        _run_ffmpeg(cmd)

    logging.info(f"Stream copy cut saved to {output_path}")
    return output_path


def _concat_segments_reencode(seg_files: list[str], output_path: str) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        concat_list = os.path.join(tmpdir, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for sf in seg_files:
                f.write(f"file '{_to_concat_path(sf)}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list,
            "-c:v", "libx264",
            "-c:a", "aac",
            output_path,
        ]
        _run_ffmpeg(cmd)

    logging.info(f"Precise cut saved to {output_path}")
    return output_path


def merge_videos_stream_copy(
    video_paths: list[str],
    output_path: str,
) -> str:
    if not video_paths:
        raise ValueError("No videos to merge")

    with tempfile.TemporaryDirectory() as tmpdir:
        concat_list = os.path.join(tmpdir, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for vp in video_paths:
                f.write(f"file '{_to_concat_path(vp)}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list,
            "-c", "copy",
            output_path,
        ]
        _run_ffmpeg(cmd)

    logging.info(f"Stream copy merge saved to {output_path}")
    return output_path
