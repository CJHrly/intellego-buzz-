import logging
import math
import os
import subprocess
import tempfile
from typing import Optional, List

from PyQt6.QtCore import QObject
from openai import OpenAI

from buzz.transcriber.file_transcriber import FileTranscriber
from buzz.transcriber.transcriber import FileTranscriptionTask, Segment, Task


class OpenAIWhisperAPIFileTranscriber(FileTranscriber):
    def __init__(self, task: FileTranscriptionTask, parent: Optional["QObject"] = None):
        super().__init__(task=task, parent=parent)
        self.task = task.transcription_options.task
        self.openai_client = OpenAI(
            api_key=self.transcription_task.transcription_options.openai_access_token
        )

    def transcribe(self) -> List[Segment]:
        logging.debug(
            "Starting OpenAI Whisper API file transcription, file path = %s, task = %s",
            self.transcription_task.file_path,
            self.task,
        )

        mp3_file = tempfile.mktemp() + ".mp3"

        cmd = ["ffmpeg", "-i", self.transcription_task.file_path, mp3_file]

        try:
            subprocess.run(cmd, capture_output=True, check=True)
        except subprocess.CalledProcessError as exc:
            logging.exception("")
            raise Exception(exc.stderr.decode("utf-8"))

        # fmt: off
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            mp3_file,
        ]
        # fmt: on
        duration_secs = float(
            subprocess.run(cmd, capture_output=True, check=True).stdout.decode("utf-8")
        )

        total_size = os.path.getsize(mp3_file)
        max_chunk_size = 25 * 1024 * 1024

        self.progress.emit((0, 100))

        if total_size < max_chunk_size:
            return self.get_segments_for_file(mp3_file)

        # If the file is larger than 25MB, split into chunks
        # and transcribe each chunk separately
        num_chunks = math.ceil(total_size / max_chunk_size)
        chunk_duration = duration_secs / num_chunks

        segments = []

        for i in range(num_chunks):
            chunk_start = i * chunk_duration
            chunk_end = min((i + 1) * chunk_duration, duration_secs)

            chunk_file = tempfile.mktemp() + ".mp3"

            # fmt: off
            cmd = [
                "ffmpeg",
                "-i", mp3_file,
                "-ss", str(chunk_start),
                "-to", str(chunk_end),
                "-c", "copy",
                chunk_file,
            ]
            # fmt: on
            subprocess.run(cmd, capture_output=True, check=True)
            logging.debug('Created chunk file "%s"', chunk_file)

            segments.extend(
                self.get_segments_for_file(
                    chunk_file, offset_ms=int(chunk_start * 1000)
                )
            )
            os.remove(chunk_file)
            self.progress.emit((i + 1, num_chunks))

        return segments

    def get_segments_for_file(self, file: str, offset_ms: int = 0):
        kwargs = {
            "model": "whisper-1",
            "file": file,
            "response_format": "verbose_json",
            "language": self.transcription_task.transcription_options.language,
        }
        transcript = (
            self.openai_client.audio.transcriptions.create(**kwargs)
            if self.transcription_task.transcription_options.task == Task.TRANSLATE
            else self.openai_client.audio.translations.create(**kwargs)
        )

        return [
            Segment(
                int(segment["start"] * 1000 + offset_ms),
                int(segment["end"] * 1000 + offset_ms),
                segment["text"],
            )
            for segment in transcript["segments"]
        ]

    def stop(self):
        pass
