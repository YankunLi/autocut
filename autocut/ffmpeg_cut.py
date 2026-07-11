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


def cut_segments_stream_copy(
    input_path: str,
    output_path: str,
    segments: list[dict[str, float]],
    precise: bool = False,
) -> str:
    if not segments:
        raise ValueError("No segments to cut")

    ext = os.path.splitext(output_path)[1]

    if precise:
        return _cut_segments_precise(input_path, output_path, segments, ext)

    with tempfile.TemporaryDirectory() as tmpdir:
        seg_files = []
        for i, seg in enumerate(segments):
            duration = seg["end"] - seg["start"]
            if duration <= 0:
                logging.warning(f"Skipping segment {i}: non-positive duration {duration}")
                continue
            seg_path = os.path.join(tmpdir, f"seg_{i:04d}{ext}")
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(seg["start"]),
                "-i", input_path,
                "-t", str(duration),
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
    keyframes = _get_keyframes(input_path)
    if not keyframes:
        logging.warning("Could not detect keyframes, falling back to stream copy")
        return cut_segments_stream_copy(input_path, output_path, segments, precise=False)

    with tempfile.TemporaryDirectory() as tmpdir:
        seg_files = []
        has_reencoded = False
        for i, seg in enumerate(segments):
            duration = seg["end"] - seg["start"]
            if duration <= 0:
                logging.warning(f"Skipping segment {i}: non-positive duration {duration}")
                continue

            kf_before_start = _find_nearest_keyframe_before(keyframes, seg["start"])
            kf_after_end = _find_nearest_keyframe_after(keyframes, seg["end"])

            # If cut points already align with keyframes, use stream copy
            if abs(seg["start"] - kf_before_start) < 0.01 and abs(seg["end"] - kf_after_end) < 0.01:
                seg_path = os.path.join(tmpdir, f"seg_{i:04d}{ext}")
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(seg["start"]),
                    "-i", input_path,
                    "-t", str(duration),
                    "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    seg_path,
                ]
                _run_ffmpeg(cmd)
                seg_files.append(seg_path)
                continue

            # Need re-encode for frame-accurate boundaries
            has_reencoded = True
            seg_path = os.path.join(tmpdir, f"seg_{i:04d}{ext}")
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(kf_before_start),
                "-i", input_path,
                "-t", str(seg["end"] - kf_before_start),
                "-c:v", "libx264",
                "-c:a", "aac",
                "-ss", str(seg["start"] - kf_before_start),
                "-avoid_negative_ts", "make_zero",
                seg_path,
            ]
            _run_ffmpeg(cmd)
            seg_files.append(seg_path)

        if not seg_files:
            raise ValueError("All segments had non-positive duration")

        if has_reencoded:
            return _concat_segments_reencode(seg_files, output_path)
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
