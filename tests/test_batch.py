import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from school_repos.batch import client, execute, main, read_students, write_report
from school_repos.github import GitHubError, TokenGitHub
from school_repos.store import Store


class BatchGitHub:
    def __init__(self):
        self.created = []
        self.granted = []
        self.fail = None
        self.users = {"alice": {"id": 10, "login": "Alice"}, "bob": {"id": 20, "login": "bob"}}

    def preflight(self, template):
        pass

    def lookup_user(self, login):
        if login not in self.users:
            raise GitHubError(404, "Účet neexistuje")
        return self.users[login]

    def create(self, assignment, name):
        self.created.append(name)
        return len(self.created)

    def grant(self, name, repo_id, login):
        if login == self.fail:
            raise GitHubError(403, "Pozvání se nezdařilo")
        self.granted.append(login)
        return "invited"


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.students = self.root / "students.txt"
        self.students.write_text("# Třída\nAlice\nbob, ALICE\n", encoding="utf-8")
        self.store = Store(str(self.root / "state.sqlite3"))
        self.github = BatchGitHub()
        self.config = {"GITHUB_ORG": "sposdknl"}
        self.fields = {"slug": "2a-2026-git-vagrant", "title": "Git a Vagrant", "description": "",
                       "template": "sposdknl/git-vagrant", "visibility": "private", "active": "1"}

    def run_batch(self, names=None):
        return execute(self.store, self.github, self.config, self.fields, names or ["alice", "bob"], output=lambda _: None)

    def test_roster_comments_case_dedup_and_validation(self):
        self.assertEqual(read_students(self.students), ["alice", "bob"])
        for invalid in ("", "@alice", "bob@example.com"):
            self.students.write_text(invalid)
            with self.assertRaises(ValueError):
                read_students(self.students)

    def test_default_plan_is_offline_and_does_not_create_database(self):
        database = self.root / "absent.sqlite3"
        with patch("school_repos.batch.client", side_effect=AssertionError("Network client must not be used")), contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["--students", str(self.students), "--cohort", "2a-2026", "--assignment", "git-vagrant", "--database", str(database), "--env-file", str(self.root / "absent.env")])
        self.assertEqual(code, 0)
        self.assertIn("alice-2a-2026-git-vagrant", output.getvalue())
        self.assertFalse(database.exists())

    def test_accounts_are_all_checked_before_writes(self):
        with self.assertRaises(GitHubError):
            self.run_batch(["alice", "missing"])
        self.assertEqual(self.github.created, [])
        self.assertEqual(self.store.assignments(), [])

    def test_entire_class_provisioned_and_repeat_skips(self):
        first = self.run_batch()
        second = self.run_batch()
        self.assertTrue(all(r["status"] == "invited" for r in first + second))
        self.assertEqual(self.github.created, ["alice-2a-2026-git-vagrant", "bob-2a-2026-git-vagrant"])
        self.assertEqual(self.github.granted, ["Alice", "bob"])

    def test_failure_does_not_stop_class_and_retry_only_grants(self):
        self.github.fail = "Alice"
        results = self.run_batch()
        self.assertEqual([r["status"] for r in results], ["error", "invited"])
        self.github.fail = None
        self.assertTrue(all(r["status"] == "invited" for r in self.run_batch()))
        self.assertEqual(len(self.github.created), 2)

    def test_changed_github_id_or_organization_rejected(self):
        self.run_batch()
        self.github.users["alice"] = {"id": 999, "login": "Alice"}
        with self.assertRaises(ValueError):
            self.run_batch()
        self.github.users["alice"] = {"id": 10, "login": "Alice"}
        self.config["GITHUB_ORG"] = "other"
        with self.assertRaises(ValueError):
            self.run_batch()
        self.assertEqual(len(self.github.created), 2)

    def test_username_redirect_is_rejected(self):
        self.github.users["alice"] = {"id": 10, "login": "renamed"}
        with self.assertRaises(ValueError):
            self.run_batch()
        self.assertEqual(self.github.created, [])

    def test_report_has_links_and_status(self):
        report = self.root / "results.csv"
        write_report(report, self.run_batch())
        content = report.read_text(encoding="utf-8-sig")
        self.assertIn("https://github.com/sposdknl/alice-2a-2026-git-vagrant", content)
        self.assertIn("invited", content)

    def test_batch_auth_does_not_require_web_oauth_configuration(self):
        github = client({"GITHUB_TOKEN": "test-token", "GITHUB_ORG": "sposdknl"})
        self.assertIsInstance(github, TokenGitHub)
        self.assertEqual(github.token(), "test-token")
        with self.assertRaises(ValueError):
            client({"GITHUB_ORG": "sposdknl"})


if __name__ == "__main__":
    unittest.main()
