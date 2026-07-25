import logging
import os
import re

import ffmpeg
import numpy as np
import opencc
import srt


def load_silero_vad():
    """Load the Silero VAD model and return (model, detect_speech_fn).

    Prefers the local torch.hub cache so we don't hit GitHub on every
    call once the model has been downloaded. torch.hub.load with the
    default github source re-checks the network even when the repo is
    cached, which causes intermittent RemoteDisconnected failures when
    GitHub rate-limits.
    """
    import torch

    # Bypass the forked-repo prompt that would otherwise block non-interactive
    # runs. Older torch versions call this during load; setting it is harmless
    # on newer versions.
    torch.hub._validate_not_a_forked_repo = lambda a, b, c: True

    cache_dir = torch.hub.get_dir()
    local_repo = os.path.join(cache_dir, "snakers4_silero-vad_master")
    if os.path.isdir(local_repo):
        vad_model, funcs = torch.hub.load(
            source="local",
            repo_or_dir=local_repo,
            model="silero_vad",
            trust_repo=True,
        )
    else:
        vad_model, funcs = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            trust_repo=True,
        )
    return vad_model, funcs[0]


def load_audio(file: str, sr: int = 16000) -> np.ndarray:
    try:
        out, _ = (
            ffmpeg.input(file, threads=0)
            .output("-", format="s16le", acodec="pcm_s16le", ac=1, ar=sr)
            .run(cmd=["ffmpeg", "-nostdin"], capture_stdout=True, capture_stderr=True)
        )
    except ffmpeg.Error as e:
        stderr = (e.stderr or b"").decode(errors="replace")
        raise RuntimeError(f"Failed to load audio: {stderr}") from e
    except FileNotFoundError as e:
        raise RuntimeError(
            "ffmpeg not found. Please install ffmpeg and add it to PATH."
        ) from e

    return np.frombuffer(out, np.int16).flatten().astype(np.float32) / 32768.0


def is_video(filename):
    _, ext = os.path.splitext(filename)
    return ext.lower() in [".mp4", ".mov", ".mkv", ".avi", ".flv", ".f4v", ".webm"]


def is_audio(filename):
    _, ext = os.path.splitext(filename)
    return ext.lower() in [".ogg", ".wav", ".mp3", ".flac", ".m4a"]


def change_ext(filename, new_ext):
    # Change the extension of filename to new_ext
    base, _ = os.path.splitext(filename)
    if not new_ext.startswith("."):
        new_ext = "." + new_ext
    return base + new_ext


def add_cut(filename):
    # Add cut mark to the filename
    base, ext = os.path.splitext(filename)
    base += "_cut"
    return base + ext


# a very simple markdown parser
class MD:
    def __init__(self, filename, encoding):
        self.lines = []
        self.EDIT_DONE_MAKR = "<-- Mark if you are done editing."
        self.encoding = encoding
        self.filename = filename
        if filename:
            self.load_file()

    def load_file(self):
        if os.path.exists(self.filename):
            with open(self.filename, encoding=self.encoding) as f:
                # Strip trailing newlines so write() can join with "\n"
                # without producing doubled blank lines on roundtrip.
                self.lines = [line.rstrip("\r\n") for line in f]

    def clear(self):
        self.lines = []

    def write(self):
        with open(self.filename, "wb") as f:
            f.write("\n".join(self.lines).encode(self.encoding, "replace"))

    def tasks(self):
        # get all tasks with their status
        ret = []
        for l in self.lines:
            mark, task = self._parse_task_status(l)
            if mark is not None:
                ret.append((mark, task))
        return ret

    def done_editing(self):
        for m, t in self.tasks():
            if m and self.EDIT_DONE_MAKR in t:
                return True
        return False

    def add(self, line):
        self.lines.append(line)

    def add_task(self, mark, contents):
        self.add(f'- [{"x" if mark else " "}] {contents.strip()}')

    def add_done_editing(self, mark):
        self.add_task(mark, self.EDIT_DONE_MAKR)

    def add_video(self, video_fn):
        ext = os.path.splitext(video_fn)[1][1:]
        self.add(
            f'\n<video controls="true" allowfullscreen="true"> <source src="{video_fn}" type="video/{ext}"> </video>\n'
        )

    def _parse_task_status(self, line):
        # return (is_marked, rest) or (None, line) if not a task
        m = re.match(r"- +\[([ xX])\] +(.*)", line)
        if not m:
            return None, line
        return m.groups()[0].lower() == "x", m.groups()[1]


def check_exists(output, force):
    if os.path.exists(output):
        if force:
            logging.info(f"{output} exists. Will overwrite it")
        else:
            logging.info(f"{output} exists. Use --force to overwrite")
            return True
    return False


def expand_segments(segments, expand_head, expand_tail, total_length):
    # Pad head and tail for each time segment
    results = []
    for i in range(len(segments)):
        t = segments[i]
        # Clamp start against the EXPANDED end of the previous segment so a
        # non-zero expand_tail can't make consecutive segments overlap.
        start = max(t["start"] - expand_head, results[i - 1]["end"] if i > 0 else 0)
        end = min(
            t["end"] + expand_tail,
            segments[i + 1]["start"] if i < len(segments) - 1 else total_length,
        )
        results.append({"start": start, "end": end})
    return results


def remove_short_segments(segments, threshold):
    # Remove segments whose length < threshold, but always keep segments at the
    # very start of audio to avoid losing opening words.
    if not segments:
        return segments
    results = []
    for i, s in enumerate(segments):
        if s["end"] - s["start"] > threshold:
            results.append(s)
        elif i == 0 and s["start"] < threshold:
            # First segment that starts near the beginning — keep it even if
            # short, because dropping it means losing opening words entirely.
            results.append({"start": 0, "end": s["end"]})
    return results


def merge_adjacent_segments(segments, threshold):
    # Merge two adjacent segments if their distance < threshold.
    # When merging, keep the transition from the LAST segment in the group,
    # since transition applies at the boundary between this group and the next.
    results = []
    i = 0
    while i < len(segments):
        s = dict(segments[i])
        for j in range(i + 1, len(segments)):
            if segments[j]["start"] < s["end"] + threshold:
                s["end"] = segments[j]["end"]
                s["transition"] = segments[j].get("transition", "cut")
                s["transition_duration"] = segments[j].get("transition_duration", 0.0)
                i = j
            else:
                break
        i += 1
        results.append(s)
    return results


def compact_rst(sub_fn, encoding):
    base, ext = os.path.splitext(sub_fn)
    COMPACT = "_compact"
    if ext != ".srt":
        logging.error("only .srt file is supported")
        return

    if base.endswith(COMPACT):
        # to original rst
        with open(sub_fn, encoding=encoding) as f:
            lines = f.readlines()
        subs = []
        for l in lines:
            items = l.split(" ")
            if len(items) < 4:
                continue
            subs.append(
                srt.Subtitle(
                    index=0,
                    start=srt.srt_timestamp_to_timedelta(items[0]),
                    end=srt.srt_timestamp_to_timedelta(items[2]),
                    content=" ".join(items[3:]).strip(),
                )
            )
        with open(base[: -len(COMPACT)] + ext, "wb") as f:
            f.write(srt.compose(subs).encode(encoding, "replace"))
    else:
        # to a compact version
        cc = opencc.OpenCC("t2s")
        with open(sub_fn, encoding=encoding) as f:
            subs = srt.parse(f.read())
        with open(base + COMPACT + ext, "wb") as f:
            for s in subs:
                f.write(
                    f"{srt.timedelta_to_srt_timestamp(s.start)} --> {srt.timedelta_to_srt_timestamp(s.end)} "
                    f"{cc.convert(s.content.strip())}\n".encode(encoding, "replace")
                )


def trans_srt_to_md(encoding, force, srt_fn, video_fn=None):
    base, ext = os.path.splitext(srt_fn)
    if ext != ".srt":
        logging.error("only .srt file is supported")
        return
    md_fn = base + ".md"

    if check_exists(md_fn, force):
        return

    with open(srt_fn, encoding=encoding) as f:
        subs = srt.parse(f.read())

    md = MD(md_fn, encoding)
    md.clear()
    md.add_done_editing(False)
    if video_fn:
        if not is_video(video_fn):
            logging.warning(f"{video_fn} may not be a video, skipping video tag")
        else:
            md.add_video(os.path.basename(video_fn))
    md.add(
        f"\nTexts generated from [{os.path.basename(srt_fn)}]({os.path.basename(srt_fn)}). "
        "Mark the sentences to keep for autocut.\n"
        "The format is [subtitle_index,duration_in_second] subtitle context.\n\n"
    )

    for s in subs:
        sec = s.start.seconds
        pre = f"[{s.index},{sec // 60:02d}:{sec % 60:02d}]"
        md.add_task(False, f"{pre:11} {s.content.strip()}")
    md.write()
