import datetime
import logging
import os
from abc import ABC, abstractmethod
from typing import Literal, Union, List, Any, TypedDict

import numpy as np
import opencc
import srt
from pydub import AudioSegment
from tqdm import tqdm

from .type import SPEECH_ARRAY_INDEX, LANG

# whisper sometimes generate traditional chinese, explicitly convert
cc = opencc.OpenCC("t2s")


class AbstractWhisperModel(ABC):
    def __init__(self, mode, sample_rate=16000):
        self.mode = mode
        self.whisper_model = None
        self.sample_rate = sample_rate

    @abstractmethod
    def load(self, *args, **kwargs):
        pass

    @abstractmethod
    def transcribe(self, *args, **kwargs):
        pass

    @abstractmethod
    def _transcribe(self, *args, **kwargs):
        pass

    @abstractmethod
    def gen_srt(self, transcribe_results: List[Any]) -> List[srt.Subtitle]:
        pass


class WhisperModel(AbstractWhisperModel):
    def __init__(self, sample_rate=16000):
        super().__init__("whisper", sample_rate)
        self.device = None

    def load(
        self,
        model_name: str = "small",
        device: Union[Literal["cpu", "cuda"], None] = None,
    ):
        self.device = device

        import whisper

        self.whisper_model = whisper.load_model(model_name, device)

    def _transcribe(self, audio, seg, lang, prompt):
        r = self.whisper_model.transcribe(
            audio[int(seg["start"]) : int(seg["end"])],
            task="transcribe",
            language=lang,
            initial_prompt=prompt,
        )
        r["origin_timestamp"] = seg
        return r

    def transcribe(
        self,
        audio: np.ndarray,
        speech_array_indices: List[SPEECH_ARRAY_INDEX],
        lang: LANG,
        prompt: str,
        progress_callback=None,
    ):
        res = []
        total = len(speech_array_indices)
        if self.device == "cpu" and total > 1:
            from itertools import count
            from multiprocessing import Pool

            pbar = tqdm(total=total)
            completed = count(1)

            pool = Pool(processes=4)
            sub_res = []
            # TODO, a better way is merging these segments into a single one, so whisper can get more context
            for seg in speech_array_indices:
                sub_res.append(
                    pool.apply_async(
                        self._transcribe,
                        (
                            audio,
                            seg,
                            lang,
                            prompt,
                        ),
                        callback=lambda x: (
                            pbar.update(),
                            (
                                progress_callback(next(completed), total)
                                if progress_callback
                                else None
                            ),
                        ),
                    )
                )
            pool.close()
            pool.join()
            pbar.close()
            res = [i.get() for i in sub_res]
        else:
            for i, seg in enumerate(
                speech_array_indices if total == 1 else tqdm(speech_array_indices)
            ):
                r = self.whisper_model.transcribe(
                    audio[int(seg["start"]) : int(seg["end"])],
                    task="transcribe",
                    language=lang,
                    initial_prompt=prompt,
                    verbose=False if total == 1 else None,
                )
                r["origin_timestamp"] = seg
                res.append(r)
                if progress_callback:
                    progress_callback(i + 1, total)
        return res

    def gen_srt(self, transcribe_results):
        subs = []

        def _add_sub(start, end, text):
            subs.append(
                srt.Subtitle(
                    index=0,
                    start=datetime.timedelta(seconds=start),
                    end=datetime.timedelta(seconds=end),
                    content=cc.convert(text.strip()),
                )
            )

        prev_end = 0
        for r in transcribe_results:
            origin = r["origin_timestamp"]
            origin_start_sec = origin["start"] / self.sample_rate
            origin_end_sec = origin["end"] / self.sample_rate
            for s in r["segments"]:
                start = max(s["start"] + origin_start_sec, origin_start_sec)
                end = min(
                    s["end"] + origin_start_sec,
                    origin_end_sec,
                )
                if start > end:
                    continue
                # mark any empty segment that is not very short
                if start > prev_end + 1.0:
                    _add_sub(prev_end, start, "< No Speech >")
                _add_sub(start, end, s["text"])
                prev_end = end

        return subs


class OpenAIModel(AbstractWhisperModel):
    max_single_audio_bytes = 25 * 2**20  # 25MB
    split_audio_bytes = 23 * 2**20  # 23MB, 2MB for safety(header, etc.)
    rpm = 3

    def __init__(self, rpm: int, sample_rate=16000):
        super().__init__("openai_whisper-1", sample_rate)
        self.rpm = rpm
        if (
            os.environ.get("OPENAI_API_KEY") is None
            and os.environ.get("OPENAI_API_KEY_PATH") is None
        ):
            raise Exception("OPENAI_API_KEY is not set")

    def load(self, model_name: Literal["whisper-1"] = "whisper-1"):
        try:
            import openai
        except ImportError:
            raise Exception(
                "Please use openai mode(pip install '.[openai]') or all mode(pip install '.[all]')"
            )

        # openai>=1.0 removed the module-level openai.Audio.transcribe helper.
        # Build a client and wrap the new client.audio.transcriptions.create
        # so _transcribe can keep calling self.whisper_model(file=..., ...).
        api_key = os.environ.get("OPENAI_API_KEY")
        key_path = os.environ.get("OPENAI_API_KEY_PATH")
        if not api_key and key_path:
            with open(key_path) as f:
                api_key = f.read().strip()

        client = openai.OpenAI(api_key=api_key)
        self.whisper_model = lambda file, prompt, language, response_format: client.audio.transcriptions.create(
            model=model_name,
            file=file,
            prompt=prompt,
            language=language,
            response_format=response_format,
        )

    def transcribe(
        self,
        input: str,
        audio: np.ndarray,
        speech_array_indices: List[SPEECH_ARRAY_INDEX],
        lang: LANG,
        prompt: str,
        progress_callback=None,
    ) -> List[srt.Subtitle]:
        res = []
        name, _ = os.path.splitext(input)
        raw_audio = AudioSegment.from_file(input)
        ms_bytes = len(raw_audio[:1].raw_data)
        audios: List[
            TypedDict(
                "AudioInfo", {"input": str, "audio": AudioSegment, "start_ms": float}
            )
        ] = []

        i = 0
        for index in speech_array_indices:
            start = int(index["start"]) / self.sample_rate * 1000
            end = int(index["end"]) / self.sample_rate * 1000
            audio_seg = raw_audio[start:end]
            if len(audio_seg.raw_data) < self.split_audio_bytes:
                temp_file = f"{name}_temp_{i}.wav"
                audios.append(
                    {"input": temp_file, "audio": audio_seg, "start_ms": start}
                )
            else:
                logging.info(
                    f"Long audio segment ({len(audio_seg.raw_data)} bytes) exceeds the "
                    f"{self.split_audio_bytes}-byte split threshold (set 2MB below the "
                    f"{self.max_single_audio_bytes}-byte OpenAI API limit); segmenting it."
                )
                split_num = (
                    len(audio_seg.raw_data) + self.split_audio_bytes - 1
                ) // self.split_audio_bytes
                for j in range(split_num):
                    temp_file = f"{name}_{i}_temp_{j}.wav"
                    split_audio = audio_seg[
                        j
                        * self.split_audio_bytes
                        // ms_bytes : (j + 1)
                        * self.split_audio_bytes
                        // ms_bytes
                    ]
                    audios.append(
                        {
                            "input": temp_file,
                            "audio": split_audio,
                            "start_ms": start + j * self.split_audio_bytes // ms_bytes,
                        }
                    )
            i += 1

        if len(audios) > 1:
            from itertools import count
            from multiprocessing import Pool

            pbar = tqdm(total=len(audios))
            completed = count(1)
            total = len(audios)

            pool = Pool(processes=min(8, self.rpm))
            sub_res = []
            for audio in audios:
                sub_res.append(
                    pool.apply_async(
                        self._transcribe,
                        (
                            audio["input"],
                            audio["audio"],
                            prompt,
                            lang,
                            audio["start_ms"],
                        ),
                        callback=lambda x: (
                            pbar.update(),
                            (
                                progress_callback(next(completed), total)
                                if progress_callback
                                else None
                            ),
                        ),
                    )
                )
            pool.close()
            pool.join()
            pbar.close()
            for subs in sub_res:
                subtitles = subs.get()
                res.extend(subtitles)
        else:
            res = self._transcribe(
                audios[0]["input"],
                audios[0]["audio"],
                prompt,
                lang,
                audios[0]["start_ms"],
            )
            if progress_callback:
                progress_callback(1, 1)

        return res

    def _transcribe(
        self, input: str, audio: AudioSegment, prompt: str, lang: LANG, start_ms: float
    ):
        try:
            audio.export(input, "wav")
            with open(input, "rb") as f:
                subtitles = self.whisper_model(
                    file=f, prompt=prompt, language=lang, response_format="srt"
                )
            return list(
                map(
                    lambda x: (
                        setattr(
                            x,
                            "start",
                            x.start + datetime.timedelta(milliseconds=start_ms),
                        ),
                        setattr(
                            x, "end", x.end + datetime.timedelta(milliseconds=start_ms)
                        ),
                        x,
                    )[-1],
                    list(srt.parse(subtitles)),
                )
            )
        finally:
            try:
                os.remove(input)
            except OSError:
                pass

    def gen_srt(self, transcribe_results: List[srt.Subtitle]):
        if len(transcribe_results) == 0:
            return []
        subs = []
        prev_end = datetime.timedelta(0)
        for subtitle in transcribe_results:
            # mark any empty segment that is not very short
            if subtitle.start - prev_end > datetime.timedelta(seconds=1):
                subs.append(
                    srt.Subtitle(
                        index=0,
                        start=prev_end,
                        end=subtitle.start,
                        content="< No Speech >",
                    )
                )
            subs.append(subtitle)
            prev_end = subtitle.end
        return subs


class FasterWhisperModel(AbstractWhisperModel):
    def __init__(self, sample_rate=16000):
        super().__init__("faster-whisper", sample_rate)
        self.device = None

    def load(
        self,
        model_name: str = "small",
        device: Union[Literal["cpu", "cuda"], None] = None,
    ):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise Exception(
                "Please use faster mode(pip install '.[faster]') or all mode(pip install '.[all]')"
            )

        self.device = device if device else "cpu"
        self.whisper_model = WhisperModel(model_name, self.device)

    def _transcribe(self):
        raise Exception("Not implemented")

    def transcribe(
        self,
        audio: np.ndarray,
        speech_array_indices: List[SPEECH_ARRAY_INDEX],
        lang: LANG,
        prompt: str,
        progress_callback=None,
    ):
        res = []
        total = len(speech_array_indices)
        for i, seg in enumerate(speech_array_indices):
            segments, info = self.whisper_model.transcribe(
                audio[int(seg["start"]) : int(seg["end"])],
                task="transcribe",
                language=lang,
                initial_prompt=prompt,
                vad_filter=False,
            )
            segments = list(segments)  # The transcription will actually run here.
            r = {"origin_timestamp": seg, "segments": segments, "info": info}
            res.append(r)
            if progress_callback:
                progress_callback(i + 1, total)
        return res

    def gen_srt(self, transcribe_results):
        subs = []

        def _add_sub(start, end, text):
            subs.append(
                srt.Subtitle(
                    index=0,
                    start=datetime.timedelta(seconds=start),
                    end=datetime.timedelta(seconds=end),
                    content=cc.convert(text.strip()),
                )
            )

        prev_end = 0
        for r in transcribe_results:
            origin = r["origin_timestamp"]
            origin_start_sec = origin["start"] / self.sample_rate
            origin_end_sec = origin["end"] / self.sample_rate
            for seg in r["segments"]:
                s = dict(start=seg.start, end=seg.end, text=seg.text)
                start = max(s["start"] + origin_start_sec, origin_start_sec)
                end = min(
                    s["end"] + origin_start_sec,
                    origin_end_sec,
                )
                if start > end:
                    continue
                # mark any empty segment that is not very short
                if start > prev_end + 1.0:
                    _add_sub(prev_end, start, "< No Speech >")
                _add_sub(start, end, s["text"])
                prev_end = end

        return subs
