"""Files: uploading one so a run or a message can reference it."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from ._base import Resource


@dataclass(frozen=True)
class UploadedFile:
    """A file Dify has taken, and the reference that points at it."""

    id: str
    name: str = ""
    size: int = 0
    mime_type: str = ""
    extension: str = ""
    created_by: str = ""
    created_at: int | None = None

    def reference(self, *, type: str = "document") -> dict[str, Any]:
        """The mapping a run's or a message's inputs use to name this file.

        Dify takes a reference, not the bytes, so an input carries this rather
        than the file itself::

            uploaded = app.files.upload("report.pdf")
            app.workflows.runs.create({"doc": uploaded.reference()})
        """
        return {
            "transfer_method": "local_file",
            "upload_file_id": self.id,
            "type": type,
        }


def _uploaded(payload: dict[str, Any]) -> UploadedFile:
    return UploadedFile(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        size=int(payload.get("size") or 0),
        mime_type=str(payload.get("mime_type") or ""),
        extension=str(payload.get("extension") or ""),
        created_by=str(payload.get("created_by") or ""),
        created_at=payload.get("created_at"),
    )


def _part(
    file: str | Path | BinaryIO,
    filename: str | None,
    content_type: str | None,
) -> tuple[str, Any, str]:
    """Work out the (name, bytes, type) triple Dify's upload wants."""
    from ..exceptions import ValidationError

    if isinstance(file, (str, Path)):
        path = Path(file)
        name = filename or path.name
        content: Any = path.read_bytes()
    else:
        name = filename or getattr(file, "name", None)
        if not name:
            msg = (
                "This file has no name to record. Pass filename=… — Dify types "
                "the upload by its extension and rejects one it cannot type."
            )
            raise ValidationError(msg)
        name = Path(str(name)).name
        content = file.read()

    guessed = content_type or mimetypes.guess_type(name)[0]
    if not guessed:
        msg = (
            f"Cannot tell what kind of file {name!r} is. Pass content_type=… — "
            "Dify answers an untyped upload with 415."
        )
        raise ValidationError(msg)
    return name, content, guessed


class Files(Resource):
    """Files this app's runs and messages can reference."""

    def upload(
        self,
        file: str | Path | BinaryIO,
        *,
        user: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> UploadedFile:
        """Upload a file and get back the reference to it.

        Args:
            file: A path, or an already-open binary file.
            user: End-user identifier.
            filename: Overrides the recorded name. Required for an object with
                no ``name``, such as a ``BytesIO``.
            content_type: Overrides the type guessed from the name.
        """
        name, content, guessed = _part(file, filename, content_type)
        response = self._client._send_request_with_files(
            "POST",
            "/files/upload",
            data={"user": self._who(user)},
            files={"file": (name, content, guessed)},
        )
        return _uploaded(response.json())

    def preview_url(self, file: UploadedFile | str) -> str:
        """Where Dify serves this file. Reaching it needs the app's key, so
        the URL alone is not enough for a browser — use :meth:`download`."""
        file_id = file if isinstance(file, str) else file.id
        return f"{self._client.base_url}/files/{file_id}/preview"

    def download(
        self, file: UploadedFile | str, *, as_attachment: bool = False
    ) -> bytes:
        """The bytes of a file **that a message carries**.

        Not the counterpart to :meth:`upload`, despite appearances: Dify serves
        this only for files attached to a message in this app. A file that was
        uploaded but not yet used in a conversation answers "The requested file
        was not found" — which reads like the id is wrong when it is not.

        Args:
            file: The file, or its id — from a message's ``files``.
            as_attachment: Ask Dify for a download rather than an inline
                preview. Only changes the headers.
        """
        file_id = file if isinstance(file, str) else file.id
        return self._client._send_request(
            "GET",
            f"/files/{file_id}/preview",
            params={"as_attachment": as_attachment},
        ).content


class AsyncFiles(Resource):
    """The async counterpart of :class:`Files`."""

    def preview_url(self, file: UploadedFile | str) -> str:
        """Where Dify serves this file. Not awaited: it sends nothing."""
        file_id = file if isinstance(file, str) else file.id
        return f"{self._client.base_url}/files/{file_id}/preview"

    async def download(
        self, file: UploadedFile | str, *, as_attachment: bool = False
    ) -> bytes:
        file_id = file if isinstance(file, str) else file.id
        return (
            await self._client._send_request(
                "GET",
                f"/files/{file_id}/preview",
                params={"as_attachment": as_attachment},
            )
        ).content

    async def upload(
        self,
        file: str | Path | BinaryIO,
        *,
        user: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> UploadedFile:
        name, content, guessed = _part(file, filename, content_type)
        response = await self._client._send_request_with_files(
            "POST",
            "/files/upload",
            data={"user": self._who(user)},
            files={"file": (name, content, guessed)},
        )
        return _uploaded(response.json())
