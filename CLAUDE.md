# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AutoCut cuts video based on transcribed subtitles. The workflow: transcribe video → edit markdown to select segments → cut video. Supports local Whisper, faster-whisper, and OpenAI API transcription modes. Published on PyPI as `autocut-sub`.

## Build & Run Commands

```shell
pip install .              # base (local whisper only)
pip install '.[faster]'    # + faster-whisper
pip install '.[openai]'    # + OpenAI API
pip install '.[all]'       # all extras

# CLI usage
autocut -t video.mp4                                          # transcribe
autocut -c video.mp4 video.srt video.md                       # cut
autocut -d /path/to/folder                                    # daemon mode

# Run as module
python -m autocut
```

## Testing & Linting

```shell
pytest test/                      # run all tests
pytest test/test_transcribe.py    # single test file
WHISPER_MODE=faster pytest test/  # test with faster-whisper
black . --check                   # lint check
black .                           # auto-format
```

CI runs pytest on Python 3.9/3.10 across Ubuntu, Windows, macOS, plus a black lint check.

## Architecture

Two entry points into the package:

1. **CLI** (`autocut/main.py:main()`) — argparse dispatches to transcribe/cut/daemon based on flags (`-t`, `-c`, `-d`)
2. **Library API** (`autocut/__init__.py`) — exports `Transcribe` from `package_transcribe.py` (not `transcribe.py`), plus `load_audio`, `WhisperMode`, `WhisperModel`, `LANG`

Key distinction: `transcribe.py::Transcribe` wraps CLI args; `package_transcribe.py::Transcribe` takes explicit parameters for programmatic use.

### Module responsibilities

- **`whisper_model.py`** — Abstract base + 3 implementations: `WhisperModel` (local openai-whisper), `FasterWhisperModel`, `OpenAIModel`. All convert Traditional→Simplified Chinese via opencc. `OpenAIModel` handles audio splitting for the 25MB API limit and rate limiting.
- **`cut.py`** — `Cutter` reads .srt/.md to cut video via moviepy; `Merger` concatenates multiple cut videos.
- **`daemon.py`** — Folder monitor with adaptive sleep (1–60s). Auto-transcribes when .srt/.md missing, auto-cuts when editing done.
- **`utils.py`** — `MD` class parses the checkbox markdown format (`- [x]` keep / `- [ ]` skip). VAD segment post-processing (expand, remove_short, merge_adjacent). Audio loading via ffmpeg-python. SRT↔compact format conversion.
- **`type.py`** — `SPEECH_ARRAY_INDEX` TypedDict, `LANG` Literal type, `WhisperModel` enum (tiny through large-v3-turbo), `WhisperMode` enum (whisper/openai/faster).

## Key Conventions

- SRT subtitle files (`.srt`) and Markdown edit files (`.md`) are the interchange format between transcription and cutting.
- The markdown editing convention: `- [x]` = keep segment, `- [ ]` = skip segment, "done editing" marker signals completion.
- ffmpeg must be installed on the system (used via CLI, not just the Python binding).
- Silero VAD is loaded at runtime via `torch.hub.load("snakers4/silero-vad")`.
- Requires Python >= 3.9.
