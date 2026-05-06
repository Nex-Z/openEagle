from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from app.attachments import AttachmentError, AttachmentStore
from app.confirmations import ToolConfirmationStore
from app.default_tools import build_default_tools, execute_confirmed_tool
from app.models import AttachmentRef


class AttachmentStoreTest(unittest.TestCase):
    def test_base64_attachment_is_stored_without_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AttachmentStore(Path(tmp))
            payload = base64.b64encode(b"hello").decode("ascii")

            attachments = store.prepare_user_attachments(
                "conversation/one",
                [
                    AttachmentRef(
                        id="att-local",
                        name="note.txt",
                        mimeType="text/plain",
                        size=5,
                        kind="file",
                        source="local",
                        contentBase64=payload,
                    )
                ],
            )

            self.assertEqual(len(attachments), 1)
            self.assertTrue(Path(attachments[0].local_path or "").is_file())
            public = store.public_dicts(attachments)[0]
            self.assertNotIn("contentBase64", public)
            self.assertEqual(public["status"], "ready")

    def test_attachment_count_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AttachmentStore(Path(tmp))
            attachments = [
                AttachmentRef(name=f"{index}.txt", contentBase64=base64.b64encode(b"x").decode("ascii"))
                for index in range(6)
            ]

            with self.assertRaises(AttachmentError):
                store.prepare_user_attachments("conversation", attachments)

    def test_remote_reply_attachment_requires_and_honors_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "report.txt").write_text("hello", encoding="utf-8")
            attachments = AttachmentStore(workspace)
            confirmations = ToolConfirmationStore()
            tools = build_default_tools(
                workspace_root=workspace,
                confirmation_store=confirmations,
                request_id="req-original",
                conversation_id="im_telegram_test",
                attachment_store=attachments,
            )

            result = tools.attach_file_to_reply("report.txt")

            self.assertTrue(result.startswith("CONFIRMATION_REQUIRED "))
            pending = confirmations.latest_for_conversation("im_telegram_test")
            self.assertIsNotNone(pending)
            confirmed = execute_confirmed_tool(
                workspace,
                pending,
                attachment_store=attachments,
                attachment_request_id="req-allow",
            )

            self.assertIn("已登记回复附件", confirmed)
            self.assertEqual(
                attachments.peek_reply_attachments("im_telegram_test", "req-allow")[0].name,
                "report.txt",
            )


if __name__ == "__main__":
    unittest.main()
