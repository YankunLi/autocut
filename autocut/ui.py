import logging
import os
import platform
import subprocess
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


class _CancelFlag:
    """Thread-safe cancel flag shared between UI and worker threads."""

    def __init__(self):
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            self._cancelled = True

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    def reset(self):
        with self._lock:
            self._cancelled = False


# Global cancel flags — one per operation type
_transcribe_cancel = _CancelFlag()
_cut_cancel = _CancelFlag()


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
            yield None, "请先上传视频/音频文件", gr.update(interactive=True), gr.update(interactive=False)
            return

        _transcribe_cancel.reset()

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

        result = {"status": "running", "srt_path": None, "error": None, "step": "初始化..."}

        def _worker():
            try:
                result["step"] = "正在加载 Whisper 模型（首次需下载，请耐心等待）..."
                t = Transcribe(args)

                if _transcribe_cancel.cancelled:
                    result["status"] = "cancelled"
                    return

                result["step"] = "正在加载音频文件..."
                audio = utils.load_audio(path, sr=t.sampling_rate)

                if _transcribe_cancel.cancelled:
                    result["status"] = "cancelled"
                    return

                result["step"] = "正在检测语音活动 (VAD)..."
                speech_array_indices = t._detect_voice_activity(audio)

                if _transcribe_cancel.cancelled:
                    result["status"] = "cancelled"
                    return

                result["step"] = f"正在转录语音，共 {len(speech_array_indices)} 个片段（此步骤耗时较长）..."
                transcribe_results = t._transcribe(path, audio, speech_array_indices)

                if _transcribe_cancel.cancelled:
                    result["status"] = "cancelled"
                    return

                result["step"] = "正在保存字幕文件..."
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

        last_step = None
        while result["status"] == "running":
            if _transcribe_cancel.cancelled:
                result["status"] = "cancelled"
                break
            step = result["step"]
            if step != last_step:
                yield None, step, gr.update(interactive=False), gr.update(interactive=True)
                last_step = step
            else:
                elapsed = int(time.time() - _worker_start)
                yield None, f"{step}  已用时 {elapsed} 秒", gr.update(interactive=False), gr.update(interactive=True)
            time.sleep(2)

        if result["status"] == "cancelled":
            yield None, "转录已取消", gr.update(interactive=True), gr.update(interactive=False)
        elif result["status"] == "error":
            yield None, f"转录失败: {result['error']}", gr.update(interactive=True), gr.update(interactive=False)
        else:
            yield result["srt_path"], f"转录完成！已生成 {os.path.basename(result['srt_path'])}", gr.update(interactive=True), gr.update(interactive=False)

    # --- Step 3: load & display ---

    def load_segments(media_path, srt_file, json_file, md_file):
        err = _check_file(media_path, "视频/音频文件")
        if err:
            return None, err
        media_path_str = _resolve_media_path(media_path)

        # Auto-detect SRT/JSON next to the media file if not explicitly provided
        if json_file is None and srt_file is None:
            base, _ = os.path.splitext(media_path_str)
            auto_json = base + ".json"
            auto_srt = base + ".srt"
            if os.path.exists(auto_json):
                json_file = auto_json
            elif os.path.exists(auto_srt):
                srt_file = auto_srt

        if json_file is not None:
            project = load_project(json_file)
        elif srt_file is not None:
            if md_file is not None:
                project = md_to_project(md_file, srt_file, media_path_str)
            else:
                project = srt_to_project(srt_file, media_path_str)
        else:
            return None, "请提供 SRT 或 JSON 文件，或先在第二步生成字幕"

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

    def _open_directory(path):
        dirname = os.path.dirname(path)
        try:
            if platform.system() == "Windows":
                os.startfile(dirname)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", dirname])
            else:
                subprocess.Popen(["xdg-open", dirname])
            return f"已打开文件夹: {dirname}"
        except Exception as e:
            return f"打开文件夹失败: {e}"

    def _find_existing_cut(project, media_path_str):
        """Check if a cut video already exists for this exact project content.

        Uses a content hash of the project segments to identify duplicate cuts.
        Returns (video_path, json_path) if found, otherwise (None, None).
        """
        import hashlib
        import json as json_mod

        content = json_mod.dumps(project["segments"], sort_keys=True, ensure_ascii=False)
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        is_video_file = utils.is_video(media_path_str.lower())
        outext = "mp4" if is_video_file else "mp3"
        base, _ = os.path.splitext(media_path_str)
        dirname = os.path.dirname(media_path_str)

        # Scan for existing _cut_*.json files matching this media
        for fname in os.listdir(dirname):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(dirname, fname)
            try:
                existing = load_project(fpath)
            except Exception:
                continue
            if existing.get("source") != media_path_str:
                continue
            existing_content = json_mod.dumps(existing["segments"], sort_keys=True, ensure_ascii=False)
            existing_hash = hashlib.md5(existing_content.encode()).hexdigest()[:8]
            if existing_hash != content_hash:
                continue
            # Hash matches — check if the corresponding video exists
            json_base = os.path.splitext(fpath)[0]
            video_path = json_base + "." + outext
            if os.path.exists(video_path):
                return video_path, fpath
        return None, None

    def run_cut(segments_data, media_path, precise):
        err = _check_file(media_path, "视频/音频文件")
        if err:
            yield err, gr.update(interactive=True), gr.update(interactive=False), gr.update(visible=False)
            return
        path = _resolve_media_path(media_path)
        segments = _parse_segments(segments_data)
        if not segments:
            yield "没有可剪辑的片段", gr.update(interactive=True), gr.update(interactive=False), gr.update(visible=False)
            return
        project: CutProject = {
            "version": "1.0",
            "source": path,
            "segments": segments,
        }

        cut_segs = project_to_segments(project)
        if not cut_segs:
            yield "没有勾选保留的片段", gr.update(interactive=True), gr.update(interactive=False), gr.update(visible=False)
            return

        # Skip cutting if output already exists for this exact project
        existing_video, existing_json = _find_existing_cut(project, path)
        if existing_video:
            yield (f"剪辑视频已存在，跳过重复剪辑：\n视频: {existing_video}\n项目: {existing_json}",
                   gr.update(interactive=True), gr.update(interactive=False), gr.update(visible=True))
            _last_output_path["value"] = existing_video
            return

        _cut_cancel.reset()

        is_video_file = utils.is_video(path.lower())
        outext = "mp4" if is_video_file else "mp3"
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        base, _ = os.path.splitext(path)
        output_fn = f"{base}_cut_{timestamp}.{outext}"
        json_fn = f"{base}_cut_{timestamp}.json"

        total = len(cut_segs)
        try:
            for msg in _cut_with_progress(path, output_fn, cut_segs, precise, is_video_file):
                if _cut_cancel.cancelled:
                    yield "剪辑已取消", gr.update(interactive=True), gr.update(interactive=False), gr.update(visible=False)
                    return
                yield msg, gr.update(interactive=False), gr.update(interactive=True), gr.update(visible=False)
            save_project(project, json_fn)
            yield f"剪辑完成！\n视频: {output_fn}\n项目: {json_fn}（共 {total} 个片段）", gr.update(interactive=True), gr.update(interactive=False), gr.update(visible=True)
            _last_output_path["value"] = output_fn
        except Exception as e:
            yield f"剪辑失败: {e}", gr.update(interactive=True), gr.update(interactive=False), gr.update(visible=False)

    def _cut_with_progress(input_path, output_path, segments, precise, is_video_file):
        from .ffmpeg_cut import _run_ffmpeg
        import tempfile
        from .ffmpeg_cut import _concat_segments

        ext = os.path.splitext(output_path)[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            seg_files = []
            total = len(segments)
            for i, seg in enumerate(segments):
                if _cut_cancel.cancelled:
                    return
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

            if _cut_cancel.cancelled:
                return

            # Apply transitions
            has_transitions = any(
                s.get("transition", "cut") in ("fade", "crossfade") for s in segments
            )
            if has_transitions:
                yield "正在应用转场效果..."
                from .ffmpeg_cut import _apply_transitions
                seg_files = _apply_transitions(seg_files, segments, is_video_file, tmpdir)

            if _cut_cancel.cancelled:
                return

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

    # --- History ---

    def _scan_history():
        """Scan for cut projects grouped by source media file.

        Returns a list of dicts: [{source, source_name, cuts: [{video, json, time_str}]}]
        """
        import json as json_mod

        history = {}
        # Look through common media directories — use _last_output_path and
        # any previously seen media paths. For simplicity, scan the directory
        # of the currently loaded media if available.
        search_dirs = set()
        media_path = _last_output_path.get("value")
        if media_path and os.path.exists(media_path):
            search_dirs.add(os.path.dirname(media_path))

        # Also scan directories of all _cut_*.json files we can find
        # by checking the current working directory
        search_dirs.add(os.getcwd())

        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for fname in os.listdir(search_dir):
                if not fname.endswith(".json") or "_cut_" not in fname:
                    continue
                fpath = os.path.join(search_dir, fname)
                try:
                    proj = load_project(fpath)
                except Exception:
                    continue
                source = proj.get("source", "")
                if not source:
                    continue
                if source not in history:
                    history[source] = {
                        "source": source,
                        "source_name": os.path.basename(source),
                        "cuts": [],
                    }
                json_base = os.path.splitext(fpath)[0]
                # Find corresponding video file
                video_path = None
                for ext in (".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".m4a"):
                    candidate = json_base + ext
                    if os.path.exists(candidate):
                        video_path = candidate
                        break
                # Extract timestamp from filename like demo_cut_20260713_153045
                ts_part = fname.replace("_cut_", "|").split("|")[-1].replace(".json", "")
                time_str = ts_part if ts_part else "?"
                history[source]["cuts"].append({
                    "video": video_path or "",
                    "json": fpath,
                    "time_str": time_str,
                })

        # Sort cuts by time within each source
        for entry in history.values():
            entry["cuts"].sort(key=lambda c: c["time_str"])
        return list(history.values())

    def _refresh_history():
        """Build HTML for the history panel."""
        records = _scan_history()
        if not records:
            return "<p style='color:#888'>暂无剪辑记录</p>"

        html_parts = []
        for rec in records:
            source_name = rec["source_name"]
            source = rec["source"]
            source_id = str(hash(source))[-8:]
            html_parts.append(
                "<div style='border:1px solid #ddd; border-radius:8px; padding:12px; margin-bottom:12px;'>"
                "<div style='display:flex; justify-content:space-between; align-items:center;'>"
                f"<b>{source_name}</b>"
                f"<button onclick=\"document.querySelector('#del-src-{source_id}').click()\" "
                "style='color:#e74c3c; background:none; border:none; cursor:pointer; font-size:14px;'>"
                "删除全部</button>"
                "</div>"
            )
            for cut in rec["cuts"]:
                video_name = os.path.basename(cut["video"]) if cut["video"] else "(视频已删除)"
                time_str = cut["time_str"]
                cut_id = str(hash(cut["json"]))[-8:]
                html_parts.append(
                    "<div style='margin:6px 0 6px 12px; display:flex; justify-content:space-between; align-items:center;'>"
                    f"<span>{time_str} - {video_name}</span>"
                    f"<button onclick=\"document.querySelector('#del-cut-{cut_id}').click()\" "
                    "style='color:#e67e22; background:none; border:none; cursor:pointer; font-size:13px;'>"
                    "删除</button>"
                    "</div>"
                )
            html_parts.append("</div>")
        return "".join(html_parts)

    def _delete_source(source_path):
        """Delete a source record: remove all associated cut videos and json files."""
        records = _scan_history()
        for rec in records:
            if rec["source"] == source_path:
                for cut in rec["cuts"]:
                    if cut["video"] and os.path.exists(cut["video"]):
                        os.remove(cut["video"])
                    if os.path.exists(cut["json"]):
                        os.remove(cut["json"])
                break
        return _refresh_history()

    def _delete_cut(json_path):
        """Delete a single cut result (video + json)."""
        try:
            proj = load_project(json_path)
        except Exception:
            pass
        json_base = os.path.splitext(json_path)[0]
        for ext in (".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".m4a"):
            candidate = json_base + ext
            if os.path.exists(candidate):
                os.remove(candidate)
        if os.path.exists(json_path):
            os.remove(json_path)
        return _refresh_history()

    # --- Build UI ---

    _last_output_path = {"value": None}

    with gr.Blocks(title="AutoCut", theme=gr.themes.Soft()) as app:
        with gr.Row():
            # --- Left sidebar navigation ---
            with gr.Column(scale=1, min_width=180):
                gr.Markdown("# AutoCut")
                nav_cut_btn = gr.Button("视频剪辑", variant="secondary", size="sm")
                nav_history_btn = gr.Button("历史记录", variant="secondary", size="sm")

            # --- Right content area ---
            with gr.Column(scale=5):
                # === Video Cut Panel ===
                with gr.Column(visible=True) as cut_panel:
                    gr.Markdown("## 视频剪辑")

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
                            with gr.Row():
                                transcribe_btn = gr.Button("开始转录", variant="primary")
                                cancel_transcribe_btn = gr.Button("取消转录", variant="stop", visible=False)
                            transcribe_status = gr.Textbox(label="进度", interactive=False)
                            transcribe_output = gr.File(label="生成的 SRT 文件", interactive=False)

                        gr.Markdown("---")
                        with gr.Column():
                            gr.Markdown("### 导入已有字幕\n如果已有 SRT/MD/JSON 文件，可直接上传。")
                            with gr.Row():
                                srt_input = gr.File(label="SRT 文件", file_types=[".srt"])
                                md_input = gr.File(label="MD 文件", file_types=[".md"])
                                json_input = gr.File(label="JSON 项目文件", file_types=[".json"])

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

                    with gr.Tab("4. 剪辑视频"):
                        gr.Markdown("编辑完成后，点击剪辑按钮生成结果。")
                        with gr.Row():
                            precise_chk = gr.Checkbox(label="帧精确剪切（推荐）", value=True)
                        with gr.Row():
                            cut_btn = gr.Button("开始剪辑", variant="primary")
                            cancel_cut_btn = gr.Button("取消剪辑", variant="stop", visible=False)
                            save_btn = gr.Button("保存项目 JSON")
                            open_dir_btn = gr.Button("打开输出目录", visible=False)
                        cut_status = gr.Textbox(label="进度", interactive=False)

                # === History Panel ===
                with gr.Column(visible=False) as history_panel:
                    gr.Markdown("## 历史记录")
                    with gr.Row():
                        refresh_history_btn = gr.Button("刷新", variant="secondary", size="sm")
                    history_html = gr.HTML(value=_refresh_history())

                    # Hidden buttons for delete actions triggered from HTML
                    _del_source_path = gr.Textbox(visible=False)
                    _del_cut_path = gr.Textbox(visible=False)
                    _del_source_btn = gr.Button("del-source", visible=False, elem_id="del-source-action")
                    _del_cut_btn = gr.Button("del-cut", visible=False, elem_id="del-cut-action")

        # --- Navigation ---
        def show_cut():
            return gr.update(visible=True), gr.update(visible=False)

        def show_history():
            return gr.update(visible=False), gr.update(visible=True)

        nav_cut_btn.click(fn=show_cut, inputs=[], outputs=[cut_panel, history_panel])
        nav_history_btn.click(fn=show_history, inputs=[], outputs=[cut_panel, history_panel])

        # --- Wire up events ---

        def cancel_transcribe():
            _transcribe_cancel.cancel()
            return gr.update(interactive=True), gr.update(interactive=False)

        def cancel_cut():
            _cut_cancel.cancel()
            return gr.update(interactive=True), gr.update(interactive=False)

        def open_output_dir():
            path = _last_output_path.get("value")
            if not path or not os.path.exists(path):
                return "输出文件不存在"
            return _open_directory(path)

        # History events
        refresh_history_btn.click(fn=lambda: _refresh_history(), inputs=[], outputs=[history_html])

        _del_source_btn.click(fn=_delete_source, inputs=[_del_source_path], outputs=[history_html])
        _del_cut_btn.click(fn=_delete_cut, inputs=[_del_cut_path], outputs=[history_html])

        # Also refresh history when switching to history panel
        nav_history_btn.click(
            fn=lambda: (gr.update(visible=False), gr.update(visible=True), _refresh_history()),
            inputs=[],
            outputs=[cut_panel, history_panel, history_html],
        )

        transcribe_btn.click(
            fn=transcribe_media,
            inputs=[media_input, lang_input, whisper_mode_input, whisper_model_input, device_input],
            outputs=[transcribe_output, transcribe_status, transcribe_btn, cancel_transcribe_btn],
        )

        cancel_transcribe_btn.click(
            fn=cancel_transcribe,
            inputs=[],
            outputs=[transcribe_btn, cancel_transcribe_btn],
        )

        load_btn.click(
            fn=load_segments,
            inputs=[media_input, srt_input, json_input, md_input],
            outputs=[segments_df, cut_status],
        )

        cut_btn.click(
            fn=run_cut,
            inputs=[segments_df, media_input, precise_chk],
            outputs=[cut_status, cut_btn, cancel_cut_btn, open_dir_btn],
        )

        cancel_cut_btn.click(
            fn=cancel_cut,
            inputs=[],
            outputs=[cut_btn, cancel_cut_btn],
        )

        open_dir_btn.click(
            fn=open_output_dir,
            inputs=[],
            outputs=[cut_status],
        )

        save_btn.click(
            fn=save_project_json,
            inputs=[segments_df, media_input],
            outputs=[cut_status],
        )

    return app
