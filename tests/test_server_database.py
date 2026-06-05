import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gemini_webapi.server.database import AccountStore


class TestAccountStore(unittest.TestCase):
    def test_imports_account_array_and_rotates_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "app.db"
            accounts_path = Path(tmp) / "accounts.json"
            accounts_path.write_text(
                """
                {
                  "accounts": [
                    {
                      "name": "one",
                      "__Secure-1PSID": "psid-one",
                      "__Secure-1PSIDTS": "ts-one"
                    },
                    {
                      "name": "two",
                      "cookies": [
                        {"name": "__Secure-1PSID", "value": "psid-two"},
                        {"name": "__Secure-1PSIDTS", "value": "ts-two"}
                      ]
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )

            store = AccountStore(db_path)
            try:
                self.assertEqual(store.import_accounts_file(accounts_path), 2)
                accounts = store.get_active_accounts()
                self.assertEqual([account.name for account in accounts], ["one", "two"])
                self.assertEqual(accounts[1].secure_1psid, "psid-two")

                store.set_state("current_account_id", str(accounts[0].id))
                self.assertEqual(store.get_state("current_account_id"), str(accounts[0].id))

                store.mark_success(accounts[0].id)
                self.assertEqual(store.get_account(accounts[0].id).usage_count, 1)

                failure_count = store.mark_failure(accounts[0].id)
                self.assertEqual(failure_count, 1)
            finally:
                store.close()

    def test_deletes_uploaded_file_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "app.db")
            try:
                store.add_file(
                    file_id="file-test",
                    filename="demo.txt",
                    content_type="text/plain",
                    path=str(Path(tmp) / "demo.txt"),
                    size=4,
                )
                self.assertIsNotNone(store.get_file("file-test"))
                self.assertTrue(store.delete_file("file-test"))
                self.assertIsNone(store.get_file("file-test"))
                self.assertFalse(store.delete_file("file-test"))
            finally:
                store.close()

    def test_persists_native_runtime_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "app.db")
            try:
                store.add_request_log(
                    time="2026-01-01T00:00:00Z",
                    duration_ms=123,
                    account_id=1,
                    account_name="one",
                    endpoint="/v1/gemini/generate",
                    model="gemini",
                    ok=True,
                    output_type="gemini_native",
                    job_id="req-1",
                    media_count=2,
                )
                logs = store.list_request_logs()
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0].endpoint, "/v1/gemini/generate")
                self.assertEqual(logs[0].media_count, 2)

                updated = store.update_request_log_media_count("req-1", 3)
                self.assertEqual(updated, 1)
                logs = store.list_request_logs()
                self.assertEqual(logs[0].media_count, 3)

                store.add_request_log(
                    time="2026-01-01T00:00:01Z",
                    duration_ms=99,
                    account_id=1,
                    account_name="one",
                    endpoint="/v1/generate",
                    model="gemini",
                    ok=True,
                    output_type="gemini_video",
                    job_id="req-legacy-generate",
                )
                # 旧 /v1/generate 也会产出媒体，媒体数量需要能回填到请求日志。
                updated = store.update_request_log_media_count("req-legacy-generate", 1)
                self.assertEqual(updated, 1)
                legacy_log = {
                    log.job_id: log
                    for log in store.list_request_logs()
                }["req-legacy-generate"]
                self.assertEqual(legacy_log.media_count, 1)

                store.add_media_output(
                    request_id="req-1",
                    account_id=1,
                    kind="image",
                    title="Image",
                    url="https://example.com/image.png",
                    local_path=str(Path(tmp) / "image.png"),
                    local_content_type="image/png",
                    local_size=12,
                    metadata={"alt": "demo"},
                )
                media = store.list_media_outputs()
                self.assertEqual(media[0].kind, "image")
                self.assertEqual(media[0].local_content_type, "image/png")
                self.assertEqual(media[0].local_size, 12)
                self.assertEqual(media[0].metadata["alt"], "demo")
                self.assertTrue(media[0].token)
                self.assertEqual(
                    store.get_media_output_by_token(media[0].token).url,
                    "https://example.com/image.png",
                )

                store.set_media_cooldown(
                    account_id=1,
                    kind="image",
                    blocked_until=(
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    ).isoformat().replace("+00:00", "Z"),
                    reason="usage limit",
                )
                cooldown = store.get_media_cooldown(1, "image")
                self.assertEqual(cooldown.kind, "image")
                self.assertEqual(cooldown.reason, "usage limit")
                self.assertEqual(
                    [item.kind for item in store.list_media_cooldowns()],
                    ["image"],
                )
                self.assertTrue(store.clear_media_cooldown(1, "image"))
                self.assertIsNone(store.get_media_cooldown(1, "image"))

                store.set_media_cooldown(
                    account_id=1,
                    kind="video",
                    blocked_until=(
                        datetime.now(timezone.utc) - timedelta(minutes=1)
                    ).isoformat().replace("+00:00", "Z"),
                    reason="expired",
                )
                self.assertIsNone(store.get_media_cooldown(1, "video"))
                self.assertEqual(store.list_media_cooldowns(), [])

                for kind in ("image", "video", "audio"):
                    store.set_media_cooldown(
                        account_id=1,
                        kind=kind,
                        blocked_until=(
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        ).isoformat().replace("+00:00", "Z"),
                        reason="limit",
                    )
                self.assertEqual(store.clear_media_cooldowns("video"), 1)
                self.assertEqual(
                    [item.kind for item in store.list_media_cooldowns()],
                    ["audio", "image"],
                )
                self.assertEqual(store.clear_media_cooldowns(), 2)
                self.assertEqual(store.list_media_cooldowns(), [])

                log_id = store.add_request_log(
                    time="2026-01-01T00:00:01Z",
                    duration_ms=10,
                    account_id=1,
                    account_name="one",
                    endpoint="/v1/gemini/generate",
                    model="gemini",
                    ok=True,
                    output_type="video_generation_attempt",
                )
                self.assertTrue(store.delete_request_log(log_id))
                self.assertFalse(
                    any(log.id == log_id for log in store.list_request_logs(limit=10))
                )

                store.upsert_job(
                    job_id="dr-1",
                    job_type="deep_research",
                    state="planned",
                    account_id=1,
                    plan={"title": "Plan"},
                )
                store.upsert_job(
                    job_id="dr-1",
                    job_type="deep_research",
                    state="done",
                    result={"text": "Done"},
                )
                job = store.get_job("dr-1")
                self.assertEqual(job.state, "done")
                self.assertEqual(job.plan_json["title"], "Plan")
                self.assertEqual(job.result_json["text"], "Done")

                store.replace_gems_cache(
                    [
                        {
                            "id": "gem-1",
                            "name": "Writer",
                            "description": "Writes",
                            "prompt": "Be concise",
                            "predefined": False,
                        }
                    ]
                )
                gems = store.list_gems_cache()
                self.assertEqual(gems[0].name, "Writer")

                store.add_file(
                    file_id="file-1",
                    filename="a.txt",
                    content_type="text/plain",
                    path=str(Path(tmp) / "a.txt"),
                    size=3,
                )
                self.assertEqual(store.get_file("file-1").filename, "a.txt")
            finally:
                store.close()

    def test_backfills_media_tokens_for_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE media_outputs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT,
                        account_id INTEGER,
                        kind TEXT NOT NULL,
                        title TEXT,
                        url TEXT NOT NULL,
                        thumbnail TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO media_outputs (
                        request_id, account_id, kind, title, url, thumbnail, metadata_json, created_at
                    ) VALUES (
                        'req-old', 1, 'video', 'Old', 'https://example.com/video.mp4', NULL, '{}', '2026-01-01T00:00:00Z'
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            store = AccountStore(db_path)
            try:
                media = store.list_media_outputs()
                self.assertEqual(len(media), 1)
                self.assertTrue(media[0].token)
                self.assertIsNone(media[0].local_path)
                self.assertIsNone(media[0].local_content_type)
                self.assertIsNone(media[0].local_size)
                self.assertEqual(
                    store.get_media_output_by_token(media[0].token).request_id,
                    "req-old",
                )
            finally:
                store.close()

    def test_backfills_legacy_request_log_media_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy_logs.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        time TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        account_id INTEGER,
                        account_name TEXT,
                        endpoint TEXT NOT NULL,
                        model TEXT,
                        stream INTEGER NOT NULL DEFAULT 0,
                        ok INTEGER NOT NULL,
                        output_type TEXT,
                        job_id TEXT,
                        media_count INTEGER NOT NULL DEFAULT 0,
                        deep_research_state TEXT,
                        error TEXT
                    );
                    CREATE TABLE media_outputs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT,
                        account_id INTEGER,
                        kind TEXT NOT NULL,
                        title TEXT,
                        url TEXT NOT NULL,
                        thumbnail TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO request_logs (
                        time, duration_ms, account_id, account_name, endpoint, model,
                        stream, ok, output_type, job_id, media_count
                    ) VALUES
                    (
                        '2026-01-01T00:00:00Z', 10, 1, 'one',
                        '/v1/gemini/generate', 'gemini', 0, 1,
                        'gemini_image', 'req-legacy', 0
                    ),
                    (
                        '2026-01-01T00:00:01Z', 10, 1, 'one',
                        '/v1/gemini/generate', 'gemini', 0, 1,
                        'gemini_native', NULL, 0
                    );
                    INSERT INTO media_outputs (
                        request_id, account_id, kind, title, url, thumbnail, metadata_json, created_at
                    ) VALUES
                    (
                        'req-legacy', 1, 'image', 'One', 'https://example.com/one.png', NULL, '{}', '2026-01-01T00:00:00Z'
                    ),
                    (
                        'req-legacy', 1, 'video', 'Two', 'https://example.com/two.mp4', NULL, '{}', '2026-01-01T00:00:01Z'
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

            store = AccountStore(db_path)
            try:
                logs = store.list_request_logs(limit=10)
                by_job_id = {log.job_id: log for log in logs}
                self.assertEqual(by_job_id["req-legacy"].media_count, 2)
                self.assertEqual(by_job_id[None].media_count, 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
