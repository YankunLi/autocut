import json
import os
import re
from typing import TypedDict

import srt

from . import utils


class Segment(TypedDict):
    index: int
    start: float
    end: float
    text: str
    keep: bool
    transition: str
    transition_duration: float


class CutProject(TypedDict):
    version: str
    source: str
    segments: list[Segment]


def srt_to_project(
    srt_path: str, media_path: str, encoding: str = "utf-8"
) -> CutProject:
    with open(srt_path, encoding=encoding) as f:
        subs = list(srt.parse(f.read()))

    segments: list[Segment] = []
    for s in subs:
        segments.append(
            {
                "index": s.index,
                "start": s.start.total_seconds(),
                "end": s.end.total_seconds(),
                "text": s.content.strip(),
                "keep": True,
                "transition": "cut",
                "transition_duration": 0.0,
            }
        )

    return {"version": "1.0", "source": media_path, "segments": segments}


def md_to_project(
    md_path: str, srt_path: str, media_path: str, encoding: str = "utf-8"
) -> CutProject:
    project = srt_to_project(srt_path, media_path, encoding)
    md = utils.MD(md_path, encoding)

    kept_indices = set()
    for mark, sent in md.tasks():
        if not mark:
            continue
        m = re.match(r"\[(\d+)", sent.strip())
        if m:
            kept_indices.add(int(m.group(1)))

    for seg in project["segments"]:
        seg["keep"] = seg["index"] in kept_indices

    return project


def project_to_segments(
    project: CutProject, merge_gap: float = 0.5
) -> list[dict[str, float]]:
    kept = [s for s in project["segments"] if s["keep"]]
    if not kept:
        return []

    kept.sort(key=lambda s: s["start"])

    raw = [{"start": s["start"], "end": s["end"]} for s in kept]
    merged = utils.merge_adjacent_segments(raw, merge_gap)
    return merged


def save_project(project: CutProject, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(project, f, ensure_ascii=False, indent=2)


def load_project(path: str) -> CutProject:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
