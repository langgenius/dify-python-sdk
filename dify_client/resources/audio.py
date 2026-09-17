"""Audio: speaking an answer, and hearing a question."""

from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO

from ..results import Message
from ._base import Resource


class Audio(Resource):
    """Text to speech, and speech to text, for this app."""

    def speak(
        self,
        text: str | None = None,
        *,
        message: Message | str | None = None,
        voice: str | None = None,
        user: str | None = None,
    ) -> bytes:
        """Turn text, or an existing message, into audio.

        Returns the audio itself. ``voice`` is often required: an app with no
        default voice configured answers "TTS is not enabled" without it. The
        app's own voice, when it has one, is in
        ``app.parameters().payload["text_to_speech"]["voice"]``.
        """
        body: dict[str, Any] = {"user": self._who(user)}
        if text is not None:
            body["text"] = text
        if message is not None:
            body["message_id"] = (
                message if isinstance(message, str) else message.message_id
            )
        if voice is not None:
            body["voice"] = voice
        return self._client._send_request("POST", "/text-to-audio", body).content

    def transcribe(
        self,
        file: str | Path | BinaryIO,
        *,
        user: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Turn recorded speech into text."""
        if isinstance(file, (str, Path)):
            path = Path(file)
            name, content = filename or path.name, path.read_bytes()
        else:
            name = filename or Path(str(getattr(file, "name", "audio.wav"))).name
            content = file.read()
        response = self._client._send_request_with_files(
            "POST",
            "/audio-to-text",
            data={"user": self._who(user)},
            files={"file": (name, content)},
        )
        return str(response.json().get("text") or "")


class AsyncAudio(Resource):
    """The async counterpart of :class:`Audio`."""

    async def speak(
        self,
        text: str | None = None,
        *,
        message: Message | str | None = None,
        voice: str | None = None,
        user: str | None = None,
    ) -> bytes:
        """Turn text, or an existing message, into audio.

        Returns the audio itself. ``voice`` is often required: an app with no
        default voice configured answers "TTS is not enabled" without it. The
        app's own voice, when it has one, is in
        ``app.parameters().payload["text_to_speech"]["voice"]``.
        """
        body: dict[str, Any] = {"user": self._who(user)}
        if text is not None:
            body["text"] = text
        if message is not None:
            body["message_id"] = (
                message if isinstance(message, str) else message.message_id
            )
        if voice is not None:
            body["voice"] = voice
        return (
            await self._client._send_request("POST", "/text-to-audio", body)
        ).content

    async def transcribe(
        self,
        file: str | Path | BinaryIO,
        *,
        user: str | None = None,
        filename: str | None = None,
    ) -> str:
        """Turn recorded speech into text."""
        if isinstance(file, (str, Path)):
            path = Path(file)
            name, content = filename or path.name, path.read_bytes()
        else:
            name = filename or Path(str(getattr(file, "name", "audio.wav"))).name
            content = file.read()
        response = await self._client._send_request_with_files(
            "POST",
            "/audio-to-text",
            data={"user": self._who(user)},
            files={"file": (name, content)},
        )
        return str(response.json().get("text") or "")
