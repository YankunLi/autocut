import json
import logging
import os
import platform
import random
import shutil
import string
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


# --- Workspace config ---

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".autocut")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")


def _load_config():
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_config(cfg):
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _get_workspace():
    cfg = _load_config()
    ws = cfg.get("workspace", "")
    if ws and os.path.isdir(ws):
        return ws
    return os.path.join(os.path.expanduser("~"), "autocut_workspace")


def _set_workspace(path):
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    cfg = _load_config()
    cfg["workspace"] = path
    _save_config(cfg)


def _gen_dirname(source_name):
    """Generate a directory name: YYYYMMDD_HHMMSS_<random4>_<source_name>"""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    rand4 = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{timestamp}_{rand4}_{source_name}"


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

        result = {"status": "running", "srt_path": None, "error": None, "step": "初始化...", "segment_done": 0, "segment_total": 0}

        def _on_transcribe_progress(done, total):
            result["segment_done"] = done
            result["segment_total"] = total
            result["step"] = f"正在转录语音 {done}/{total} 个片段..."

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
                result["segment_total"] = len(speech_array_indices)
                transcribe_results = t._transcribe(path, audio, speech_array_indices, progress_callback=_on_transcribe_progress)

                if _transcribe_cancel.cancelled:
                    result["status"] = "cancelled"
                    return

                result["step"] = "正在保存字幕文件..."
                # Save SRT/MD to workspace project dir
                source_dir = _get_source_dir(path)
                if not source_dir:
                    source_dir = _create_source_dir(path)
                source_name = os.path.splitext(os.path.basename(path))[0]
                srt_path = os.path.join(source_dir, source_name + ".srt")
                md_path = os.path.join(source_dir, source_name + ".md")
                t._save_srt(srt_path, transcribe_results)
                t._save_md(md_path, srt_path, path)
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
            elapsed = int(time.time() - _worker_start)
            if step != last_step:
                if result["segment_total"] > 0:
                    yield None, f"{step}  已用时 {elapsed} 秒", gr.update(interactive=False), gr.update(interactive=True)
                else:
                    yield None, step, gr.update(interactive=False), gr.update(interactive=True)
                last_step = step
            else:
                yield None, f"{step}  已用时 {elapsed} 秒", gr.update(interactive=False), gr.update(interactive=True)
            time.sleep(2)

        if result["status"] == "cancelled":
            yield None, "转录已取消", gr.update(interactive=True), gr.update(interactive=False)
        elif result["status"] == "error":
            yield None, f"转录失败: {result['error']}", gr.update(interactive=True), gr.update(interactive=False)
        else:
            yield result["srt_path"], f"转录完成！已生成 {result['srt_path']}", gr.update(interactive=True), gr.update(interactive=False)

    # --- Step 3: load & display ---

    def load_segments(media_path, srt_file, json_file, md_file):
        err = _check_file(media_path, "视频/音频文件")
        if err:
            return None, err, -1
        media_path_str = _resolve_media_path(media_path)

        # If user explicitly provided files, use them directly
        user_provided = json_file is not None or srt_file is not None

        if not user_provided:
            # Auto-detect: first check workspace project dir, then next to media
            source_dir = _get_source_dir(media_path_str)
            if source_dir:
                for fname in os.listdir(source_dir):
                    if fname.endswith(".srt"):
                        srt_file = os.path.join(source_dir, fname)
                        break
                if srt_file is None:
                    for fname in os.listdir(source_dir):
                        if fname.endswith(".json") and "_cut_" not in fname:
                            json_file = os.path.join(source_dir, fname)
                            break
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
            return None, "请提供 SRT 或 JSON 文件，或先在第二步生成字幕", -1

        # Build source info string
        def _display_path(p):
            if p is None:
                return None
            return p if isinstance(p, str) else p.name

        source_info = ""
        if json_file is not None:
            source_info = f"JSON: {_display_path(json_file)}"
            if md_file is not None:
                source_info += f"\nMD: {_display_path(md_file)}"
        elif srt_file is not None:
            source_info = f"SRT: {_display_path(srt_file)}"
            if md_file is not None:
                source_info += f"\nMD: {_display_path(md_file)}"

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
        return rows, f"已加载 {len(project['segments'])} 个片段\n{source_info}", -1

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
        """Check if a cut video already exists for this exact project content."""
        import hashlib
        import json as json_mod

        content = json_mod.dumps(project["segments"], sort_keys=True, ensure_ascii=False)
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        is_video_file = utils.is_video(media_path_str.lower())
        outext = "mp4" if is_video_file else "mp3"

        source_dir = _get_source_dir(media_path_str)
        if not source_dir or not os.path.isdir(source_dir):
            return None, None

        # Scan cut subdirectories
        for name in os.listdir(source_dir):
            cut_dir = os.path.join(source_dir, name)
            if not os.path.isdir(cut_dir):
                continue
            for fname in os.listdir(cut_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(cut_dir, fname)
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
                json_base = os.path.splitext(fpath)[0]
                video_path = json_base + "." + outext
                if os.path.exists(video_path):
                    return video_path, fpath
        return None, None

    def _get_source_dir(media_path_str):
        """Find or create the project directory for a source media file.

        Layout: workspace/YYYYMMDD_HHMMSS_<rand4>_<source_name>/
        If a project dir already exists for this source (contains a .meta marker),
        return it. Otherwise caller should create a new one.
        """
        workspace = _get_workspace()
        source_name = os.path.splitext(os.path.basename(media_path_str))[0]
        if os.path.isdir(workspace):
            for name in os.listdir(workspace):
                candidate = os.path.join(workspace, name)
                if not os.path.isdir(candidate):
                    continue
                meta_path = os.path.join(candidate, ".autocut_meta.json")
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, encoding="utf-8") as f:
                            meta = json.load(f)
                        if meta.get("source") == media_path_str:
                            return candidate
                    except Exception:
                        pass
        return None

    def _create_source_dir(media_path_str):
        """Create a new project directory for a source media file."""
        workspace = _get_workspace()
        source_name = os.path.splitext(os.path.basename(media_path_str))[0]
        dirname = _gen_dirname(source_name)
        source_dir = os.path.join(workspace, dirname)
        os.makedirs(source_dir, exist_ok=True)
        # Write marker file
        meta = {"source": media_path_str, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(os.path.join(source_dir, ".autocut_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        # Copy SRT if it exists next to the source
        base, _ = os.path.splitext(media_path_str)
        for ext in (".srt", ".md"):
            src = base + ext
            if os.path.exists(src):
                dest = os.path.join(source_dir, os.path.basename(src))
                if not os.path.exists(dest):
                    shutil.copy2(src, dest)
        return source_dir

    def _ensure_source_copied(source_dir, media_path_str):
        """Copy source media into project dir if not already there.
        Called lazily only when needed (e.g. during cut).
        """
        dest_media = os.path.join(source_dir, os.path.basename(media_path_str))
        if not os.path.exists(dest_media):
            shutil.copy2(media_path_str, dest_media)

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

        # Ensure source project dir exists and source media is copied
        source_dir = _get_source_dir(path)
        if not source_dir:
            source_dir = _create_source_dir(path)
        _ensure_source_copied(source_dir, path)

        is_video_file = utils.is_video(path.lower())
        outext = "mp4" if is_video_file else "mp3"
        source_name = os.path.splitext(os.path.basename(path))[0]
        # Each cut gets its own subdirectory
        cut_dirname = _gen_dirname(source_name + "_cut")
        cut_dir = os.path.join(source_dir, cut_dirname)
        os.makedirs(cut_dir, exist_ok=True)
        output_fn = os.path.join(cut_dir, f"{source_name}_cut.{outext}")
        json_fn = os.path.join(cut_dir, f"{source_name}_cut.json")

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
        """Scan workspace for project directories grouped by source media file.

        Layout:
          workspace/20260713_153045_a1b2_demo/   <- source project dir
            .autocut_meta.json
            demo.mp4
            demo.srt
            20260713_160000_c3d4_demo_cut/       <- cut result dir
              demo_cut.mp4
              demo_cut.json
        """
        history = []
        workspace = _get_workspace()
        if not os.path.isdir(workspace):
            return history

        for dirname in sorted(os.listdir(workspace), reverse=True):
            source_dir = os.path.join(workspace, dirname)
            if not os.path.isdir(source_dir):
                continue
            meta_path = os.path.join(source_dir, ".autocut_meta.json")
            if not os.path.exists(meta_path):
                continue
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue
            source = meta.get("source", "")
            source_name = os.path.basename(source) if source else dirname
            created = meta.get("created", "")

            cuts = []
            for sub_name in sorted(os.listdir(source_dir), reverse=True):
                cut_dir = os.path.join(source_dir, sub_name)
                if not os.path.isdir(cut_dir):
                    continue
                # Look for .json project file inside cut dir
                for fname in os.listdir(cut_dir):
                    if not fname.endswith(".json"):
                        continue
                    json_path = os.path.join(cut_dir, fname)
                    video_path = None
                    json_base = os.path.splitext(json_path)[0]
                    for ext in (".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".m4a"):
                        candidate = json_base + ext
                        if os.path.exists(candidate):
                            video_path = candidate
                            break
                    cuts.append({
                        "video": video_path or "",
                        "json": json_path,
                        "cut_dir": cut_dir,
                        "cut_dirname": sub_name,
                    })
                    break  # one json per cut dir

            history.append({
                "source": source,
                "source_name": source_name,
                "source_dir": source_dir,
                "source_dirname": dirname,
                "created": created,
                "cuts": cuts,
            })

        return history

    def _refresh_history():
        """Build HTML for the history panel with inline delete/view buttons."""
        records = _scan_history()
        if not records:
            return "<p style='color:#888'>暂无剪辑记录</p>"

        html_parts = []
        idx = 0
        for rec in records:
            source_dirname = rec["source_dirname"]
            source_name = rec["source_name"]
            created = rec["created"]
            html_parts.append(
                "<div style='border:1px solid #ddd; border-radius:8px; padding:12px; margin-bottom:12px;'>"
                "<div style='display:flex; justify-content:space-between; align-items:center;'>"
                f"<div><b>{source_dirname}</b><br><span style='color:#666; font-size:12px;'>{source_name} &nbsp; {created}</span></div>"
                f"<button data-action='del' data-idx='{idx}' style='color:#e74c3c; background:none; border:1px solid #e74c3c; border-radius:4px; cursor:pointer; font-size:12px; padding:2px 8px;'>删除全部</button>"
                "</div>"
            )
            idx += 1
            for cut in rec["cuts"]:
                video_name = os.path.basename(cut["video"]) if cut["video"] else "(视频已删除)"
                cut_dirname = cut["cut_dirname"]
                html_parts.append(
                    "<div style='margin:6px 0 6px 12px; display:flex; justify-content:space-between; align-items:center;'>"
                    f"<div><b>{cut_dirname}</b><br><span style='color:#666; font-size:12px;'>{video_name}</span></div>"
                    f"<div style='display:flex; gap:4px;'>"
                    f"<button data-action='view' data-idx='{idx}' style='color:#3498db; background:none; border:1px solid #3498db; border-radius:4px; cursor:pointer; font-size:12px; padding:2px 8px;'>查看</button>"
                    f"<button data-action='del' data-idx='{idx}' style='color:#e67e22; background:none; border:1px solid #e67e22; border-radius:4px; cursor:pointer; font-size:12px; padding:2px 8px;'>删除</button>"
                    "</div>"
                    "</div>"
                )
                idx += 1
            html_parts.append("</div>")
        return "".join(html_parts)

    def _get_history_items():
        """Return ordered list of (label, dir_path, video_path) for all history items."""
        records = _scan_history()
        items = []
        for rec in records:
            source_dir = rec["source_dir"]
            source_dirname = rec["source_dirname"]
            items.append((f"[源] {source_dirname} (删除全部)", source_dir, ""))
            for cut in rec["cuts"]:
                cut_dir = cut["cut_dir"]
                cut_dirname = cut["cut_dirname"]
                video = cut["video"] or ""
                items.append((f"[剪辑] {cut_dirname}", cut_dir, video))
        return items

    def _delete_item(idx_val):
        """Delete a history item by index."""
        items = _get_history_items()
        try:
            idx = int(idx_val)
        except (ValueError, TypeError):
            return _refresh_history(), "无效的索引"
        if idx < 0 or idx >= len(items):
            return _refresh_history(), "索引超出范围"
        target_path = items[idx][1]
        if not os.path.isdir(target_path):
            return _refresh_history(), "路径已不存在"
        shutil.rmtree(target_path)
        return _refresh_history(), "已删除"

    def _view_item(idx_val):
        """Open the video file for a history item by index."""
        items = _get_history_items()
        try:
            idx = int(idx_val)
        except (ValueError, TypeError):
            return _refresh_history(), "无效的索引"
        if idx < 0 or idx >= len(items):
            return _refresh_history(), "索引超出范围"
        video_path = items[idx][2]
        if not video_path or not os.path.exists(video_path):
            return _refresh_history(), "视频文件不存在"
        _open_directory(video_path)
        return _refresh_history(), "已打开文件夹"

    # --- Build UI ---

    _last_output_path = {"value": None}

    with gr.Blocks(title="AutoCut") as app:
        with gr.Row():
            # --- Left sidebar navigation ---
            with gr.Column(scale=1, min_width=180):
                gr.Markdown("# AutoCut")
                nav_cut_btn = gr.Button("视频剪辑", variant="primary", size="sm")
                nav_history_btn = gr.Button("历史记录", variant="secondary", size="sm")
                nav_config_btn = gr.Button("配置", variant="secondary", size="sm")

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
                        _selected_row = gr.State(-1)
                        with gr.Row():
                            transition_dd = gr.Dropdown(
                                choices=["cut", "fade", "crossfade"],
                                value="cut",
                                label="转场类型",
                                scale=4,
                                info="在表格中点选一行，再点应用",
                            )
                            apply_transition_btn = gr.Button("应用到选中行", scale=1)
                            apply_all_transition_btn = gr.Button("应用到所有", scale=1)
                        transition_status = gr.Textbox(label="转场操作结果", interactive=False)

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
                            save_btn = gr.Button("保存项目 JSON", visible=False)
                            open_dir_btn = gr.Button("打开输出目录", visible=False)
                        cut_status = gr.Textbox(label="进度", interactive=False)

                # === History Panel ===
                with gr.Column(visible=False) as history_panel:
                    gr.Markdown("## 历史记录")
                    with gr.Row():
                        refresh_history_btn = gr.Button("刷新", variant="secondary", size="sm")
                    history_html = gr.HTML(
                        value=_refresh_history(),
                        elem_classes="history-html",
                        js_on_load="""function attachListeners(){
                            element.querySelectorAll('[data-action]').forEach(function(btn){
                                btn.addEventListener('click', function(){
                                    var action=this.getAttribute('data-action');
                                    var idx=parseInt(this.getAttribute('data-idx'));
                                    if(isNaN(idx)) return;
                                    if(action==='del'){
                                        server._delete_item(idx).then(function(r){ props.value=r[0]; });
                                    } else if(action==='view'){
                                        server._view_item(idx).then(function(r){ props.value=r[0]; });
                                    }
                                });
                            });
                        }
                        attachListeners();
                        watch('value', attachListeners);""",
                        server_functions=[_delete_item, _view_item],
                    )

                # === Config Panel ===
                with gr.Column(visible=False) as config_panel:
                    gr.Markdown("## 配置")
                    workspace_tb = gr.Textbox(
                        label="工作目录",
                        value=_get_workspace(),
                        info="剪辑结果将保存在此目录下，按原始视频名称建立子目录",
                    )
                    with gr.Row():
                        save_workspace_btn = gr.Button("保存", variant="primary")
                        open_workspace_btn = gr.Button("打开目录")
                    workspace_status = gr.Textbox(label="", interactive=False)

        # --- Navigation ---
        def show_cut():
            return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False),
                    gr.update(variant="primary"), gr.update(variant="secondary"), gr.update(variant="secondary"))

        def show_history():
            return (gr.update(visible=False), gr.update(visible=True), gr.update(visible=False),
                    gr.update(variant="secondary"), gr.update(variant="primary"), gr.update(variant="secondary"))

        def show_config():
            return (gr.update(visible=False), gr.update(visible=False), gr.update(visible=True),
                    gr.update(variant="secondary"), gr.update(variant="secondary"), gr.update(variant="primary"))

        nav_cut_btn.click(fn=show_cut, inputs=[], outputs=[cut_panel, history_panel, config_panel, nav_cut_btn, nav_history_btn, nav_config_btn])
        nav_config_btn.click(fn=show_config, inputs=[], outputs=[cut_panel, history_panel, config_panel, nav_cut_btn, nav_history_btn, nav_config_btn])

        # History panel: also refresh when switching to it
        nav_history_btn.click(
            fn=lambda: (gr.update(visible=False), gr.update(visible=True), gr.update(visible=False),
                        gr.update(variant="secondary"), gr.update(variant="primary"), gr.update(variant="secondary"),
                        _refresh_history()),
            inputs=[],
            outputs=[cut_panel, history_panel, config_panel, nav_cut_btn, nav_history_btn, nav_config_btn, history_html],
        )

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

        # Remove unused hidden endpoints - the HTML component's server_functions handle it now

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
            outputs=[segments_df, cut_status, _selected_row],
        )

        def _on_segment_select(evt: gr.SelectData):
            """Store selected row index and populate dropdown with that row's transition."""
            row_idx = evt.index[0]
            row_val = evt.row_value
            transition_val = row_val[5] if row_val and len(row_val) > 5 else "cut"
            return row_idx, gr.update(value=transition_val, visible=True)

        def _apply_transition_to_row(df_data, row_idx, transition_val):
            if df_data is None or row_idx is None or row_idx < 0:
                return df_data, "请先在表格中点选一行"
            rows = _df_to_rows(df_data)
            if not rows or row_idx >= len(rows):
                return df_data, "行索引超出范围"
            rows[row_idx][5] = transition_val
            return rows, f"已将第 {row_idx + 1} 行的转场设为 '{transition_val}'"

        def _apply_transition_to_all(df_data, transition_val):
            if df_data is None:
                return df_data, "没有片段"
            rows = _df_to_rows(df_data)
            if not rows:
                return df_data, "没有片段"
            for row in rows:
                row[5] = transition_val
            return rows, f"已将全部 {len(rows)} 个片段的转场设为 '{transition_val}'"

        def _df_to_rows(df_data):
            if df_data is None:
                return None
            if hasattr(df_data, "empty") and df_data.empty:
                return None
            if hasattr(df_data, "values"):
                return df_data.values.tolist()
            if isinstance(df_data, list):
                return df_data
            return None

        segments_df.select(
            fn=_on_segment_select,
            inputs=[],
            outputs=[_selected_row, transition_dd],
        )

        apply_transition_btn.click(
            fn=_apply_transition_to_row,
            inputs=[segments_df, _selected_row, transition_dd],
            outputs=[segments_df, transition_status],
        )

        apply_all_transition_btn.click(
            fn=_apply_transition_to_all,
            inputs=[segments_df, transition_dd],
            outputs=[segments_df, transition_status],
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

        # Config events
        def save_workspace(path):
            path = path.strip()
            if not path:
                return "请输入工作目录路径"
            try:
                _set_workspace(path)
                return f"工作目录已保存: {path}"
            except Exception as e:
                return f"保存失败: {e}"

        def open_workspace():
            ws = _get_workspace()
            os.makedirs(ws, exist_ok=True)
            return _open_directory(ws + os.sep)

        save_workspace_btn.click(fn=save_workspace, inputs=[workspace_tb], outputs=[workspace_status])
        open_workspace_btn.click(fn=open_workspace, inputs=[], outputs=[workspace_status])

    return app


_BRIDGE_JS = ""
