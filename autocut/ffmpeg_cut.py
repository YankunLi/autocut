import logging
import os
import subprocess
import tempfile


def _run_ffmpeg(cmd: list[str]) -> str:
    logging.info(f"ffmpeg cmd: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (code {result.returncode}): {result.stderr}")
    return result.stdout


def _to_concat_path(path: str) -> str:
    return path.replace("\\", "/")


def cut_segments_stream_copy(
    input_path: str,
    output_path: str,
    segments: list[dict[str, float]],
) -> str:
    if not segments:
        raise ValueError("No segments to cut")

    ext = os.path.splitext(output_path)[1]
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
                "-copyts",
                "-avoid_negative_ts", "make_zero",
                seg_path,
            ]
            _run_ffmpeg(cmd)
            seg_files.append(seg_path)

        if not seg_files:
            raise ValueError("All segments had non-positive duration")

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
