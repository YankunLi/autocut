import logging
import os
import re

from . import utils
from .schema import CutProject, load_project, md_to_project, project_to_segments, srt_to_project, save_project


# Merge videos
class Merger:
    def __init__(self, args):
        self.args = args

    def write_md(self, videos):
        md = utils.MD(self.args.inputs[0], self.args.encoding)
        num_tasks = len(md.tasks())
        # Not overwrite if already marked as down or no new videos
        if md.done_editing() or num_tasks == len(videos) + 1:
            return

        md.clear()
        md.add_done_editing(False)
        md.add("\nSelect the files that will be used to generate `autocut_final.mp4`\n")
        base = lambda fn: os.path.basename(fn)
        for f in videos:
            md_fn = utils.change_ext(f, "md")
            video_md = utils.MD(md_fn, self.args.encoding)
            # select a few words to scribe the video
            desc = ""
            if len(video_md.tasks()) > 1:
                for _, t in video_md.tasks()[1:]:
                    m = re.findall(r"\] (.*)", t)
                    if m and "no speech" not in m[0].lower():
                        desc += m[0] + " "
                    if len(desc) > 50:
                        break
            md.add_task(
                False,
                f'[{base(f)}]({base(md_fn)}) {"[Edited]" if video_md.done_editing() else ""} {desc}',
            )
        md.write()

    def run(self):
        md_fn = self.args.inputs[0]
        md = utils.MD(md_fn, self.args.encoding)
        if not md.done_editing():
            return

        videos = []
        for m, t in md.tasks():
            if not m:
                continue
            m = re.findall(r"\[(.*)\]", t)
            if not m:
                continue
            fn = os.path.join(os.path.dirname(md_fn), m[0])
            logging.info(f"Loading {fn}")
            videos.append(fn)

        logging.info(f"Merging {len(videos)} videos")

        fn = os.path.splitext(md_fn)[0] + "_merged.mp4"
        encoding_method = getattr(self.args, "encoding_method", "stream_copy")

        if encoding_method == "stream_copy":
            from .ffmpeg_cut import merge_videos_stream_copy
            merge_videos_stream_copy(videos, fn)
        else:
            from moviepy import editor
            clips = [editor.VideoFileClip(v) for v in videos]
            merged = editor.concatenate_videoclips(clips)
            merged.write_videofile(
                fn, audio_codec="aac", bitrate=self.args.bitrate
            )
            for c in clips:
                c.close()

        logging.info(f"Saved merged video to {fn}")


# Cut media
class Cutter:
    def __init__(self, args):
        self.args = args

    def run(self):
        fns = {"srt": None, "media": None, "md": None, "json": None}
        for fn in self.args.inputs:
            ext = os.path.splitext(fn)[1][1:]
            fns[ext if ext in fns else "media"] = fn

        assert fns["media"], "must provide a media filename"
        assert fns["srt"] or fns["json"], "must provide a srt or json filename"

        is_video_file = utils.is_video(fns["media"].lower())
        outext = "mp4" if is_video_file else "mp3"
        output_fn = utils.change_ext(utils.add_cut(fns["media"]), outext)
        if utils.check_exists(output_fn, self.args.force):
            return

        merge_gap = getattr(self.args, "merge_gap", 0.5)
        encoding_method = getattr(self.args, "encoding_method", "stream_copy")

        if fns["json"]:
            project = load_project(fns["json"])
            logging.info(f'Cut {fns["media"]} based on {fns["json"]}')
        elif fns["md"]:
            project = md_to_project(
                fns["md"], fns["srt"], fns["media"], self.args.encoding
            )
            md = utils.MD(fns["md"], self.args.encoding)
            if not md.done_editing():
                logging.warning("Editing not marked as done, skipping")
                return
            logging.info(f'Cut {fns["media"]} based on {fns["srt"]} and {fns["md"]}')
        else:
            project = srt_to_project(fns["srt"], fns["media"], self.args.encoding)
            logging.info(f'Cut {fns["media"]} based on {fns["srt"]}')

        segments = project_to_segments(project, merge_gap)
        if not segments:
            logging.warning("No segments to cut")
            return

        if encoding_method == "stream_copy":
            from .ffmpeg_cut import cut_segments_stream_copy

            precise = getattr(self.args, "precise", False)
            cut_segments_stream_copy(fns["media"], output_fn, segments, precise=precise)
        else:
            from moviepy import editor

            if is_video_file:
                media = editor.VideoFileClip(fns["media"])
            else:
                media = editor.AudioFileClip(fns["media"])

            clips = [media.subclip(s["start"], s["end"]) for s in segments]
            if is_video_file:
                final_clip = editor.concatenate_videoclips(clips)
                logging.info(
                    f"Reduced duration from {media.duration:.1f} to {final_clip.duration:.1f}"
                )

                aud = final_clip.audio.set_fps(44100)
                final_clip = final_clip.without_audio().set_audio(aud)
                final_clip = final_clip.fx(editor.afx.audio_normalize)

                final_clip.write_videofile(
                    output_fn, audio_codec="aac", bitrate=self.args.bitrate
                )
            else:
                final_clip = editor.concatenate_audioclips(clips)
                logging.info(
                    f"Reduced duration from {media.duration:.1f} to {final_clip.duration:.1f}"
                )

                final_clip = final_clip.fx(editor.afx.audio_normalize)
                final_clip.write_audiofile(
                    output_fn, codec="libmp3lame", fps=44100, bitrate=self.args.bitrate
                )

            media.close()

        project_json_path = utils.change_ext(output_fn, "json")
        save_project(project, project_json_path)

        logging.info(f"Saved media to {output_fn}")
