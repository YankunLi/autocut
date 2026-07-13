import logging
import os
import time
import threading

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
    from .type import WhisperMode, WhisperModel

    # --- helpers ---

    def _resolve_media_path(media_path):
        if media_path is None:
            return None
        return media_path if isinstance(media_path, str) else media_path.name

    def _check_file(file_obj, label="File"):
        if not file_obj:
            return f"请提供{label}"
        path = file_obj if isinstance(file_obj, str) else file_obj.name
        if not os.path.exists(path):
            return f"{label}不存在: {path}"
        return None

    # --- Step 2: transcribe ---

    def transcribe_media(media_path, lang, whisper_mode, whisper_model_name, device):
        path = _resolve_media_path(media_path)
        if not path:
            yield None, "请先上传视频/音频文件"
            return

        from .transcribe import Transcribe

        class _Args:
            pass

        args = _Args()
        args.inputs = [path]
        args.lang = lang
        args.whisper_mode = whisper_mode
        args.whisper_model = whisper_model_name
        args.openai_rpm = 3
        args.prompt = ""
        args.encoding = "utf-8"
        args.force = True
        args.vad = "auto"
        args.device = device if device != "auto" else None

        # Transcription can take minutes. Run in a background thread and
        # poll a shared status dict so we can yield progress updates to
        # Gradio without hitting the timeout.
        result = {"status": "running", "srt_path": None, "error": None}

        def _worker():
            try:
                t = Transcribe(args)
                audio = utils.load_audio(path, sr=t.sampling_rate)
                speech_array_indices = t._detect_voice_activity(audio)
                transcribe_results = t._transcribe(path, audio, speech_array_indices)
                name, _ = os.path.splitext(path)
                srt_path = name + ".srt"
                t._save_srt(srt_path, transcribe_results)
                t._save_md(name + ".md", srt_path, path)
                result["status"] = "done"
                result["srt_path"] = srt_path
            except Exception as e:
                result["status"] = "error"
                result["error"] = str(e)

        _worker_start = time.time()
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        messages = [
            "正在加载 Whisper 模型（首次需下载，请耐心等待）...",
            "正在检测语音活动 (VAD)...",
            "正在转录语音（此步骤耗时较长，请勿关闭页面）...",
        ]
        msg_idx = 0
        while result["status"] == "running":
            if msg_idx < len(messages):
                yield None, messages[msg_idx]
                msg_idx += 1
            else:
                elapsed = int(time.time() - _worker_start)
                yield None, f"正在转录中...已用时 {elapsed} 秒"
            time.sleep(3)

        if result["status"] == "error":
            yield None, f"转录失败: {result['error']}"
        else:
            yield result["srt_path"], f"转录完成，已生成 {os.path.basename(result['srt_path'])}"

    # --- Step 3: load & display ---

    def load_segments(media_path, srt_file, json_file, md_file):
        err = _check_file(media_path, "视频/音频文件")
        if err:
            return None, err
        media_path_str = _resolve_media_path(media_path)

        if json_file is not None:
            project = load_project(json_file)
        elif srt_file is not None:
            if md_file is not None:
                project = md_to_project(md_file, srt_file, media_path_str)
            else:
                project = srt_to_project(srt_file, media_path_str)
        else:
            return None, "请提供 SRT 或 JSON 文件"

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
        return rows, f"已加载 {len(project['segments'])} 个片段"

    def _parse_segments(segments_data):
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

    # --- Step 4: cut ---

    def run_cut(segments_data, media_path, precise):
        err = _check_file(media_path, "视频/音频文件")
        if err:
            yield err
            return
        path = _resolve_media_path(media_path)
        segments = _parse_segments(segments_data)
        if not segments:
            yield "没有可剪辑的片段"
            return
        project: CutProject = {
            "version": "1.0",
            "source": path,
            "segments": segments,
        }

        cut_segs = project_to_segments(project)
        if not cut_segs:
            yield "没有勾选保留的片段"
            return

        is_video_file = utils.is_video(path.lower())
        outext = "mp4" if is_video_file else "mp3"
        output_fn = utils.change_ext(utils.add_cut(path), outext)

        total = len(cut_segs)
        try:
            for i, msg in _cut_with_progress(path, output_fn, cut_segs, precise, is_video_file):
                yield msg
            yield f"剪辑完成！已保存到 {output_fn}（共 {total} 个片段）"
        except Exception as e:
            yield f"剪辑失败: {e}"

    def _cut_with_progress(input_path, output_path, segments, precise, is_video_file):
        from .ffmpeg_cut import _run_ffmpeg
        import tempfile
        from .ffmpeg_cut import _concat_segments

        ext = os.path.splitext(output_path)[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            seg_files = []
            total = len(segments)
            for i, seg in enumerate(segments):
                duration = seg["end"] - seg["start"]
                if duration <= 0:
                    continue
                yield f"正在提取片段 {i + 1}/{total}（{duration:.1f}秒）..."
                seg_path = os.path.join(tmpdir, f"seg_{i:04d}{ext}")
                if is_video_file:
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", str(seg["start"]),
                        "-i", input_path,
                        "-t", str(duration),
                        "-c:v", "libx264", "-c:a", "aac",
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

            # Apply transitions
            has_transitions = any(
                s.get("transition", "cut") in ("fade", "crossfade") for s in segments
            )
            if has_transitions:
                yield "正在应用转场效果..."
                from .ffmpeg_cut import _apply_transitions
                seg_files = _apply_transitions(seg_files, segments, is_video_file, tmpdir)

            yield "正在合并片段..."
            _concat_segments(seg_files, output_path)

    def save_project_json(segments_data, media_path):
        err = _check_file(media_path, "视频/音频文件")
        if err:
            return err
        path = _resolve_media_path(media_path)
        segments = _parse_segments(segments_data)
        if not segments:
            return "没有片段可保存"
        project: CutProject = {
            "version": "1.0",
            "source": path,
            "segments": segments,
        }
        json_path = os.path.splitext(path)[0] + ".json"
        save_project(project, json_path)
        return f"项目已保存到 {json_path}"

    # --- Build UI ---

    with gr.Blocks(title="AutoCut", theme=gr.themes.Soft()) as app:
        gr.Markdown("# AutoCut - 视频智能剪辑")

        # --- Step 1: Import media ---
        with gr.Tab("1. 导入视频"):
            gr.Markdown("上传需要剪辑的视频或音频文件。")
            media_input = gr.File(
                label="视频/音频文件",
                file_types=[".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".mp3", ".wav", ".m4a", ".flac"],
            )
            media_info = gr.Textbox(label="文件信息", interactive=False)
            media_input.change(
                fn=lambda f: f"已选择: {os.path.basename(f.name)}" if f else "",
                inputs=[media_input],
                outputs=[media_info],
            )

        # --- Step 2: Transcribe ---
        with gr.Tab("2. 生成字幕"):
            with gr.Column():
                gr.Markdown("### 自动转录\n使用 Whisper 模型从视频中生成 SRT 字幕文件。")
                with gr.Row():
                    lang_input = gr.Dropdown(
                        choices=["zh", "en", "ja", "ko", "de", "fr", "es"],
                        value="zh",
                        label="语言",
                    )
                    whisper_mode_input = gr.Dropdown(
                        choices=WhisperMode.get_values(),
                        value=WhisperMode.WHISPER.value,
                        label="Whisper 模式",
                    )
                    whisper_model_input = gr.Dropdown(
                        choices=WhisperModel.get_values(),
                        value=WhisperModel.SMALL.value,
                        label="模型大小",
                    )
                    device_input = gr.Dropdown(
                        choices=["auto", "cpu", "cuda"],
                        value="auto",
                        label="设备",
                    )
                transcribe_btn = gr.Button("开始转录", variant="primary")
                transcribe_status = gr.Textbox(label="进度", interactive=False)
                transcribe_output = gr.File(label="生成的 SRT 文件", interactive=False)

            gr.Markdown("---")
            with gr.Column():
                gr.Markdown("### 导入已有字幕\n如果已有 SRT/MD/JSON 文件，可直接上传。")
                with gr.Row():
                    srt_input = gr.File(label="SRT 文件", file_types=[".srt"])
                    md_input = gr.File(label="MD 文件", file_types=[".md"])
                    json_input = gr.File(label="JSON 项目文件", file_types=[".json"])

        # --- Step 3: Edit segments ---
        with gr.Tab("3. 编辑片段"):
            gr.Markdown("加载字幕后，勾选要保留的片段，设置转场效果。")
            load_btn = gr.Button("加载片段", variant="primary")
            segments_df = gr.Dataframe(
                headers=["Index", "Start", "End", "Text", "Keep", "Transition", "Trans. Duration"],
                datatype=["number", "number", "number", "str", "bool", "str", "number"],
                interactive=True,
                label="片段列表",
            )

            with gr.Accordion("转场类型说明", open=False):
                gr.Markdown(
                    "| 类型 | 含义 |\n|---|---|\n"
                    "| cut | 硬切，直接跳到下一段 |\n"
                    "| fade | 淡入淡出，当前片段淡出至黑屏，下一段淡入 |\n"
                    "| crossfade | 交叉淡入淡出，当前片段淡出同时下一段淡入 |\n\n"
                    "转场作用在片段结尾处。"
                )

        # --- Step 4: Cut ---
        with gr.Tab("4. 剪辑视频"):
            gr.Markdown("编辑完成后，点击剪辑按钮生成结果。")
            with gr.Row():
                precise_chk = gr.Checkbox(label="帧精确剪切（推荐）", value=True)
            with gr.Row():
                cut_btn = gr.Button("开始剪辑", variant="primary")
                save_btn = gr.Button("保存项目 JSON")
            cut_status = gr.Textbox(label="进度", interactive=False)

        # --- Wire up events ---

        transcribe_btn.click(
            fn=transcribe_media,
            inputs=[media_input, lang_input, whisper_mode_input, whisper_model_input, device_input],
            outputs=[transcribe_output, transcribe_status],
        )

        load_btn.click(
            fn=load_segments,
            inputs=[media_input, srt_input, json_input, md_input],
            outputs=[segments_df, cut_status],
        )

        cut_btn.click(
            fn=run_cut,
            inputs=[segments_df, media_input, precise_chk],
            outputs=[cut_status],
        )

        save_btn.click(
            fn=save_project_json,
            inputs=[segments_df, media_input],
            outputs=[cut_status],
        )

    return app
