from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from .models import AttachmentRef
from .safety import resolve_workspace_path

MAX_ATTACHMENTS_PER_MESSAGE = 5
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
ATTACHMENT_DIR = ".open-eagle/attachments"


class AttachmentError(ValueError):
    pass


def infer_attachment_kind(name: str, mime_type: str | None) -> str:
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime:
        return "file"
    ext_mime, _ = mimetypes.guess_type(name)
    return infer_attachment_kind(name, ext_mime)


def safe_filename(name: str, fallback: str = "attachment") -> str:
    raw = Path(name or fallback).name.strip() or fallback
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", raw).strip(" .")
    return cleaned[:180] or fallback


def safe_path_segment(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._")
    return cleaned[:120] or "conversation"


def attachment_summary(attachments: Iterable[AttachmentRef]) -> str:
    rows = []
    for index, attachment in enumerate(attachments, start=1):
        size = f"{attachment.size} bytes" if attachment.size else "unknown size"
        rows.append(
            f"{index}. {attachment.name or attachment.id} "
            f"({attachment.kind}, {attachment.mime_type}, {size})"
        )
    return "\n".join(rows)


def append_attachment_context(content: str, attachments: list[AttachmentRef]) -> str:
    if not attachments:
        return content
    prefix = content.strip() or "请处理这些附件。"
    return f"{prefix}\n\n附件列表:\n{attachment_summary(attachments)}"


class AttachmentStore:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.root = (self.workspace_root / ATTACHMENT_DIR).resolve()
        self._reply_attachments: dict[tuple[str, str], list[AttachmentRef]] = {}

    def prepare_user_attachments(
        self,
        conversation_id: str,
        attachments: list[AttachmentRef],
    ) -> list[AttachmentRef]:
        self._validate_count(attachments)
        prepared: list[AttachmentRef] = []
        for attachment in attachments:
            if attachment.content_base64:
                prepared.append(self._store_base64(conversation_id, attachment))
                continue
            if attachment.local_path:
                prepared.append(self._import_existing_local_path(conversation_id, attachment))
                continue
            if attachment.status == "error":
                prepared.append(attachment)
                continue
            raise AttachmentError(f"附件 {attachment.name or attachment.id} 缺少内容。")
        return prepared

    def store_bytes(
        self,
        conversation_id: str,
        *,
        data: bytes,
        name: str,
        mime_type: str | None = None,
        kind: str | None = None,
        source: str = "remote",
        remote_meta: dict[str, object] | None = None,
        attachment_id: str | None = None,
    ) -> AttachmentRef:
        self._validate_size(len(data), name)
        safe_name = safe_filename(name)
        guessed_mime = mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        actual_kind = kind or infer_attachment_kind(safe_name, guessed_mime)
        target_dir = self._attachment_dir(conversation_id, attachment_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = self._safe_child(target_dir, safe_name)
        target.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        attachment = AttachmentRef(
            id=target_dir.name,
            name=safe_name,
            mimeType=guessed_mime,
            size=len(data),
            kind=actual_kind,
            source=source,
            localPath=str(target),
            remoteMeta={**(remote_meta or {}), "sha256": digest},
            status="ready",
        )
        self._write_metadata(target_dir, attachment)
        return attachment

    def register_generated_file(
        self,
        conversation_id: str,
        request_id: str,
        path: str,
        display_name: str | None = None,
    ) -> AttachmentRef:
        target = resolve_workspace_path(self.workspace_root, path)
        if not target.exists() or not target.is_file():
            raise AttachmentError(f"文件不存在或不是文件: {path}")
        existing = self._reply_attachments.setdefault((conversation_id, request_id), [])
        if len(existing) >= MAX_ATTACHMENTS_PER_MESSAGE:
            raise AttachmentError(
                f"单条回复最多支持 {MAX_ATTACHMENTS_PER_MESSAGE} 个附件。"
            )
        attachment = self._copy_from_path(
            conversation_id,
            target,
            source="generated",
            display_name=display_name,
            remote_meta={"attachedBy": "attach_file_to_reply"},
        )
        existing.append(attachment)
        return attachment

    def peek_reply_attachments(self, conversation_id: str, request_id: str) -> list[AttachmentRef]:
        return list(self._reply_attachments.get((conversation_id, request_id), []))

    def pop_reply_attachments(self, conversation_id: str, request_id: str) -> list[AttachmentRef]:
        return self._reply_attachments.pop((conversation_id, request_id), [])

    def public_dicts(self, attachments: list[AttachmentRef]) -> list[dict[str, object]]:
        return [
            attachment.model_copy(update={"content_base64": None}).model_dump(
                by_alias=True,
                exclude_none=True,
            )
            for attachment in attachments
        ]

    def _store_base64(self, conversation_id: str, attachment: AttachmentRef) -> AttachmentRef:
        raw = attachment.content_base64 or ""
        if "," in raw and raw.strip().lower().startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=True)
        except ValueError as exc:
            raise AttachmentError(f"附件 {attachment.name or attachment.id} base64 无效。") from exc
        return self.store_bytes(
            conversation_id,
            data=data,
            name=attachment.name or attachment.id,
            mime_type=attachment.mime_type,
            kind=attachment.kind,
            source=attachment.source,
            remote_meta=attachment.remote_meta,
            attachment_id=attachment.id,
        )

    def _import_existing_local_path(
        self,
        conversation_id: str,
        attachment: AttachmentRef,
    ) -> AttachmentRef:
        path = Path(str(attachment.local_path)).resolve()
        if self._is_managed_path(path):
            if not path.exists() or not path.is_file():
                raise AttachmentError(f"附件文件不存在: {attachment.name or path.name}")
            return attachment.model_copy(update={"status": "ready", "content_base64": None})
        try:
            path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise AttachmentError("出于安全原因，localPath 附件必须位于工作区或附件仓库内。") from exc
        return self._copy_from_path(
            conversation_id,
            path,
            source=attachment.source,
            display_name=attachment.name or None,
            remote_meta=attachment.remote_meta,
            attachment_id=attachment.id,
        )

    def _copy_from_path(
        self,
        conversation_id: str,
        source_path: Path,
        *,
        source: str,
        display_name: str | None = None,
        remote_meta: dict[str, object] | None = None,
        attachment_id: str | None = None,
    ) -> AttachmentRef:
        if not source_path.exists() or not source_path.is_file():
            raise AttachmentError(f"文件不存在或不是文件: {source_path}")
        size = source_path.stat().st_size
        self._validate_size(size, source_path.name)
        safe_name = safe_filename(display_name or source_path.name)
        mime_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        target_dir = self._attachment_dir(conversation_id, attachment_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = self._safe_child(target_dir, safe_name)
        shutil.copy2(source_path, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        attachment = AttachmentRef(
            id=target_dir.name,
            name=safe_name,
            mimeType=mime_type,
            size=size,
            kind=infer_attachment_kind(safe_name, mime_type),
            source=source,
            localPath=str(target),
            remoteMeta={**(remote_meta or {}), "sha256": digest},
            status="ready",
        )
        self._write_metadata(target_dir, attachment)
        return attachment

    def _attachment_dir(self, conversation_id: str, attachment_id: str | None = None) -> Path:
        safe_conversation = safe_path_segment(conversation_id)
        raw_id = attachment_id or f"att-{uuid4().hex}"
        safe_id = safe_path_segment(raw_id)
        return (self.root / safe_conversation / safe_id).resolve()

    def _safe_child(self, parent: Path, name: str) -> Path:
        target = (parent / name).resolve()
        try:
            target.relative_to(parent.resolve())
        except ValueError as exc:
            raise AttachmentError("附件目标路径越界。") from exc
        return target

    def _is_managed_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            return False
        return True

    def _write_metadata(self, directory: Path, attachment: AttachmentRef) -> None:
        data = attachment.model_dump(by_alias=True, exclude_none=True)
        (directory / "metadata.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _validate_count(attachments: list[AttachmentRef]) -> None:
        if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise AttachmentError(
                f"单条消息最多支持 {MAX_ATTACHMENTS_PER_MESSAGE} 个附件。"
            )

    @staticmethod
    def _validate_size(size: int, name: str) -> None:
        if size > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(
                f"附件 {name} 超过 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB 限制。"
            )
