import tempfile
import unittest
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
                    media_count=2,
                )
                logs = store.list_request_logs()
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0].endpoint, "/v1/gemini/generate")
                self.assertEqual(logs[0].media_count, 2)

                store.add_media_output(
                    request_id="req-1",
                    account_id=1,
                    kind="image",
                    title="Image",
                    url="https://example.com/image.png",
                    metadata={"alt": "demo"},
                )
                media = store.list_media_outputs()
                self.assertEqual(media[0].kind, "image")
                self.assertEqual(media[0].metadata["alt"], "demo")

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


if __name__ == "__main__":
    unittest.main()
