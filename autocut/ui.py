import logging
import os

from .schema import (
    CutProject,
    load_project,
    project_to_segments,
    save_project,
    srt_to_project,
    md_to_project,
)


def create_ui():
    try:
        import gradio as gr
    except ImportError:
        raise ImportError(
            "Gradio is required for the web UI. Install it with: pip install autocut-sub[ui]"
        )

    from .ffmpeg_cut import cut_segments_stream_copy
    from . import utils

    def _check_media(media_path):
        if not media_path:
            return "Please provide a media file"
        path = media_path if isinstance(media_path, str) else media_path.name
        if not os.path.exists(path):
            return f"Media file not found: {path}"
        return None

    def load_file(media_path, srt_file, json_file, md_file):
        err = _check_media(media_path)
        if err:
            return None, err
        media_path_str = media_path if isinstance(media_path, str) else media_path.name
        if json_file is not None:
            project = load_project(json_file)
        elif srt_file is not None:
            if md_file is not None:
                project = md_to_project(md_file, srt_file, media_path_str)
            else:
                project = srt_to_project(srt_file, media_path_str)
        else:
            return None, "Please provide an SRT or JSON file"

        rows = []
        for seg in project["segments"]:
            rows.append([
                seg["index"],
                round(seg["start"], 3),
                round(seg["end"], 3),
                seg["text"],
                seg["keep"],
                seg["transition"],
                seg["transition_duration"],
            ])
        return rows, f"Loaded {len(project['segments'])} segments from {os.path.basename(media_path_str)}"

    def _parse_segments(segments_data):
        """Parse segments from Gradio Dataframe, handling both list and DataFrame inputs."""
        if segments_data is None:
            return None
        if hasattr(segments_data, 'empty') and segments_data.empty:
            return None
        if hasattr(segments_data, 'values'):
            rows = segments_data.values.tolist()
        elif isinstance(segments_data, list):
            rows = segments_data
        else:
            return None

        result = []
        for row in rows:
            try:
                result.append({
                    "index": int(float(row[0])),
                    "start": float(row[1]),
                    "end": float(row[2]),
                    "text": str(row[3]),
                    "keep": bool(row[4]),
                    "transition": str(row[5]),
                    "transition_duration": float(row[6]),
                })
            except (ValueError, TypeError, IndexError):
                continue
        return result

    def _resolve_media_path(media_path):
        if media_path is None:
            return None
        return media_path if isinstance(media_path, str) else media_path.name

    def save_project_json(segments_data, media_path):
        err = _check_media(media_path)
        if err:
            return err
        path = _resolve_media_path(media_path)
        segments = _parse_segments(segments_data)
        if not segments:
            return "No segments to save"
        project: CutProject = {
            "version": "1.0",
            "source": path,
            "segments": segments,
        }
        json_path = os.path.splitext(path)[0] + ".json"
        save_project(project, json_path)
        return f"Project saved to {json_path}"

    def run_cut(segments_data, media_path, precise):
        err = _check_media(media_path)
        if err:
            return err
        path = _resolve_media_path(media_path)
        segments = _parse_segments(segments_data)
        if not segments:
            return "No segments to cut"
        project: CutProject = {
            "version": "1.0",
            "source": path,
            "segments": segments,
        }

        segments = project_to_segments(project)
        if not segments:
            return "No segments marked as keep"

        is_video_file = utils.is_video(path.lower())
        outext = "mp4" if is_video_file else "mp3"
        output_fn = utils.change_ext(utils.add_cut(path), outext)

        cut_segments_stream_copy(path, output_fn, segments, precise=precise)
        return f"Cut saved to {output_fn} ({len(segments)} segments)"

    with gr.Blocks(title="AutoCut Editor") as app:
        gr.Markdown("# AutoCut - Segment Editor")

        with gr.Row():
            media_input = gr.File(label="Media file", file_types=[".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".mp3", ".wav", ".m4a", ".flac"])
            srt_input = gr.File(label="SRT file", file_types=[".srt"])
            md_input = gr.File(label="MD file", file_types=[".md"])
            json_input = gr.File(label="Project JSON", file_types=[".json"])
            load_btn = gr.Button("Load", variant="primary")

        segments_df = gr.Dataframe(
            headers=["Index", "Start", "End", "Text", "Keep", "Transition", "Trans. Duration"],
            datatype=["number", "number", "number", "str", "bool", "str", "number"],
            interactive=True,
            label="Segments (edit Keep/Transition columns, then cut)",
        )

        with gr.Row():
            save_btn = gr.Button("Save Project JSON")
            cut_btn = gr.Button("Run Cut (stream copy)", variant="primary")

        precise_chk = gr.Checkbox(label="Precise (frame-accurate, recommended)", value=True)

        status = gr.Textbox(label="Status")

        load_btn.click(
            fn=load_file,
            inputs=[media_input, srt_input, json_input, md_input],
            outputs=[segments_df, status],
        )
        save_btn.click(
            fn=save_project_json,
            inputs=[segments_df, media_input],
            outputs=[status],
        )
        cut_btn.click(
            fn=run_cut,
            inputs=[segments_df, media_input, precise_chk],
            outputs=[status],
        )

    return app
