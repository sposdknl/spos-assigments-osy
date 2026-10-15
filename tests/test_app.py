import base64
import concurrent.futures
import io
import json
import re
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from school_repos.github import GitHub, GitHubError, request_json
from school_repos.manage import recover
from school_repos.store import Store
from school_repos.web import Application


class FakeGitHub:
    def __init__(self):
        self.created = []
        self.granted = []
        self.create_error = None
        self.grant_error = None

    def create(self, assignment, name):
        self.created.append(name)
        if self.create_error:
            raise self.create_error
        return 9876

    def grant(self, name, repo_id, login):
        self.granted.append((name, repo_id, login))
        if self.grant_error:
            raise self.grant_error
        return "invited"


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"DATABASE": str(Path(self.temp.name) / "test.sqlite3"), "DEMO": False,
                       "GITHUB_ORG": "sposdknl", "APP_URL": "https://projekty.example", "ADMIN_IDS": {1},
                       "GITHUB_CLIENT_ID": "client", "GITHUB_CLIENT_SECRET": "secret"}
        self.github = FakeGitHub()
        self.app = Application(self.config, self.github)
        self.store = self.app.store
        self.fields = {"slug": "git-vagrant", "title": "Git a Vagrant", "description": "Školní práce",
                       "template": "sposdknl/git-vagrant", "visibility": "private", "active": "1", "roster": "Zak-Demo, dalsi-zak"}
        self.store.save_assignment(self.fields)
        self.user = {"id": 2, "login": "Zak-Demo"}


class ProvisionTests(BaseTest):
    def test_creates_once_and_reuses(self):
        project = self.store.provision("git-vagrant", self.user, self.github)
        again = self.store.provision("git-vagrant", self.user, self.github)
        self.assertEqual(project["repo_id"], again["repo_id"])
        self.assertEqual(self.github.created, ["zak-demo-git-vagrant"])
        self.assertEqual(self.github.granted, [("zak-demo-git-vagrant", 9876, "Zak-Demo")])
        self.assertEqual(project["status"], "invited")

    def test_not_on_roster_cannot_create(self):
        with self.assertRaises(PermissionError):
            self.store.provision("git-vagrant", {"id": 77, "login": "outsider"}, self.github)
        self.assertEqual(self.github.created, [])

    def test_closed_assignment_cannot_create(self):
        self.store.save_assignment({**self.fields, "active": "0"})
        with self.assertRaises(PermissionError):
            self.store.provision("git-vagrant", self.user, self.github)

    def test_username_is_bound_to_github_id(self):
        self.store.provision("git-vagrant", self.user, self.github)
        impersonator = {"id": 99, "login": "Zak-Demo"}
        self.assertFalse(self.store.allowed("git-vagrant", impersonator))
        with self.assertRaises(PermissionError):
            self.store.provision("git-vagrant", impersonator, self.github)
        self.store.save_assignment(self.fields)
        self.assertFalse(self.store.allowed("git-vagrant", impersonator))

    def test_roster_change_enforced_for_existing_project(self):
        self.store.provision("git-vagrant", self.user, self.github)
        self.store.save_assignment({**self.fields, "roster": "dalsi-zak"})
        with self.assertRaises(PermissionError):
            self.store.provision("git-vagrant", self.user, self.github)

    def test_invitation_failure_can_retry_without_new_repository(self):
        self.github.grant_error = GitHubError(503, "Nedostupné")
        with self.assertRaises(GitHubError):
            self.store.provision("git-vagrant", self.user, self.github)
        project = self.store.project("git-vagrant", 2)
        self.assertEqual(project["status"], "error")
        self.assertEqual(project["repo_id"], 9876)
        self.github.grant_error = None
        self.store.provision("git-vagrant", self.user, self.github)
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(len(self.github.granted), 2)

    def test_collision_or_unknown_creation_never_adopted(self):
        self.github.create_error = GitHubError(422, "Název existuje")
        with self.assertRaises(GitHubError):
            self.store.provision("git-vagrant", self.user, self.github)
        self.github.create_error = None
        with self.assertRaises(ValueError):
            self.store.provision("git-vagrant", self.user, self.github)
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(self.github.granted, [])

    def test_concurrent_requests_create_one_repository(self):
        started, finish = threading.Event(), threading.Event()
        original = self.github.create

        def slow_create(assignment, name):
            started.set()
            self.assertTrue(finish.wait(timeout=5))
            return original(assignment, name)

        self.github.create = slow_create
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.store.provision, "git-vagrant", self.user, self.github)
            self.assertTrue(started.wait(timeout=5))
            try:
                with self.assertRaises(ValueError):
                    self.store.provision("git-vagrant", self.user, self.github)
            finally:
                finish.set()
            self.assertEqual(first.result()["status"], "invited")
        self.assertEqual(len(self.github.created), 1)

    def test_template_and_visibility_frozen_after_first_attempt(self):
        self.store.provision("git-vagrant", self.user, self.github)
        for fields in ({"template": "sposdknl/other"}, {"visibility": "public"}):
            with self.assertRaises(ValueError):
                self.store.save_assignment({**self.fields, **fields})
        self.store.save_assignment({**self.fields, "title": "Nový popis"})
        self.assertEqual(self.store.assignment("git-vagrant")["title"], "Nový popis")

    def test_validation_and_empty_roster(self):
        for fields in ({"slug": "../x"}, {"template": "http://evil.test"}, {"roster": "@user"}, {"visibility": "invalid"}):
            with self.assertRaises(ValueError):
                self.store.save_assignment({**self.fields, **fields})
        self.store.save_assignment({**self.fields, "roster": ""})
        self.assertFalse(self.store.allowed("git-vagrant", self.user))

    def test_recovery_refuses_existing_repository(self):
        self.github.create_error = GitHubError(503, "Nejisté vytvoření")
        with self.assertRaises(GitHubError):
            self.store.provision("git-vagrant", self.user, self.github)
        self.github.token = lambda: "installation-token"
        with patch("school_repos.manage.request_json", return_value=(200, {"id": 555})):
            with self.assertRaises(ValueError):
                recover(self.store, self.github, self.config, "git-vagrant", 2, "reset-uncreated")
        self.assertIsNotNone(self.store.project("git-vagrant", 2))
        with patch("school_repos.manage.request_json", side_effect=GitHubError(404, "Absent")):
            recover(self.store, self.github, self.config, "git-vagrant", 2, "reset-uncreated")
        self.assertIsNone(self.store.project("git-vagrant", 2))

    def test_recovery_requires_verified_absence(self):
        self.github.create_error = GitHubError(503, "Nejisté vytvoření")
        with self.assertRaises(GitHubError):
            self.store.provision("git-vagrant", self.user, self.github)
        self.github.token = lambda: "token"
        with patch("school_repos.manage.request_json", side_effect=GitHubError(403, "Forbidden")):
            with self.assertRaises(GitHubError):
                recover(self.store, self.github, self.config, "git-vagrant", 2, "reset-uncreated")
        self.assertIsNotNone(self.store.project("git-vagrant", 2))


class FakeSocket:
    """Exercise the real HTTP parser/handler without binding a network socket."""
    def __init__(self, raw):
        self.input = io.BytesIO(raw)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class HTTPTests(BaseTest):
    def request(self, path="/", method="GET", fields=None, cookie=None, origin=None):
        body = urllib.parse.urlencode(fields or {}).encode() if method == "POST" else b""
        headers = [f"{method} {path} HTTP/1.1", "Host: projekty.example", "Connection: close"]
        if cookie:
            headers.append("Cookie: school_session=" + cookie)
        if origin:
            headers.append("Origin: " + origin)
        if method == "POST":
            headers += ["Content-Type: application/x-www-form-urlencoded", f"Content-Length: {len(body)}"]
        socket = FakeSocket(("\r\n".join(headers) + "\r\n\r\n").encode() + body)
        self.app.handler()(socket, ("127.0.0.1", 1234), None)
        head, response_body = bytes(socket.output).split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:]), response_body.decode()

    def login(self, admin=False):
        return self.app.new_session({"id": 1, "login": "teacher"} if admin else self.user)

    def test_public_home_and_security_headers(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn("Přihlásit přes GitHub", body)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "strict-origin")

    def test_admin_authorization(self):
        self.assertEqual(self.request("/admin")[0], 403)
        cookie, _ = self.login()
        self.assertEqual(self.request("/admin", cookie=cookie)[0], 403)
        cookie, _ = self.login(admin=True)
        self.assertEqual(self.request("/admin", cookie=cookie)[0], 200)

    def test_admin_create_assignment(self):
        cookie, session = self.login(admin=True)
        status, headers, _ = self.request("/admin/save", "POST", {
            **self.fields, "slug": "osy-2026", "csrf": session["csrf"]}, cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin")
        self.assertIsNotNone(self.store.assignment("osy-2026"))

    def test_students_cannot_save_assignments(self):
        cookie, session = self.login()
        self.assertEqual(self.request("/admin/save", "POST", {
            **self.fields, "csrf": session["csrf"]}, cookie)[0], 403)

    def test_delete_assignment_requires_admin_csrf_and_confirmation(self):
        cookie, session = self.login()
        fields = {"slug": "git-vagrant", "confirm": "1", "csrf": session["csrf"]}
        self.assertEqual(self.request("/admin/delete/git-vagrant", cookie=cookie)[0], 403)
        self.assertEqual(self.request("/admin/delete", "POST", fields, cookie)[0], 403)
        cookie, session = self.login(admin=True)
        self.assertEqual(self.request("/admin/delete", "POST", fields, cookie)[0], 403)
        fields["csrf"] = session["csrf"]
        self.assertEqual(self.request("/admin/delete", "POST", {**fields, "confirm": ""}, cookie)[0], 400)
        self.assertEqual(self.request("/admin/delete", "POST", fields, cookie, "https://evil.example")[0], 403)
        self.assertIsNotNone(self.store.assignment("git-vagrant"))

    def test_delete_assignment_removes_local_records_only_after_confirmation(self):
        self.store.provision("git-vagrant", self.user, self.github)
        cookie, session = self.login(admin=True)
        status, _, body = self.request("/admin/delete/git-vagrant", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Repozitáře a přístupy na GitHubu zůstanou zachované", body)
        self.assertIsNotNone(self.store.assignment("git-vagrant"))
        status, headers, _ = self.request("/admin/delete", "POST", {
            "slug": "git-vagrant", "confirm": "1", "csrf": session["csrf"]}, cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin")
        self.assertIsNone(self.store.assignment("git-vagrant"))
        self.assertEqual(self.store.roster("git-vagrant"), [])
        self.assertEqual(self.store.projects("git-vagrant"), [])
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(len(self.github.granted), 1)

    def test_delete_assignment_refuses_in_progress_project(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO projects VALUES ('git-vagrant',2,'Zak-Demo','zak-demo-git-vagrant',NULL,'creating','',0)")
        cookie, session = self.login(admin=True)
        status, _, _ = self.request("/admin/delete", "POST", {
            "slug": "git-vagrant", "confirm": "1", "csrf": session["csrf"]}, cookie)
        self.assertEqual(status, 400)
        self.assertIsNotNone(self.store.assignment("git-vagrant"))
        self.assertIsNotNone(self.store.project("git-vagrant", 2))

    def test_csrf_and_origin_prevent_side_effects(self):
        cookie, session = self.login()
        self.assertEqual(self.request("/projects/create", "POST", {"slug": "git-vagrant"}, cookie)[0], 403)
        self.assertEqual(self.request("/projects/create", "POST", {
            "slug": "git-vagrant", "csrf": session["csrf"]}, cookie, "https://evil.example")[0], 403)
        self.assertEqual(self.github.created, [])

    def test_student_creation_and_invitation_page(self):
        cookie, session = self.login()
        status, headers, _ = self.request("/projects/create", "POST", {
            "slug": "git-vagrant", "csrf": session["csrf"]}, cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/zadani/git-vagrant")
        status, _, body = self.request(headers["Location"], cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Pozvánka odeslána", body)
        self.assertIn("https://github.com/sposdknl/zak-demo-git-vagrant", body)
        self.assertNotIn("Vytvořit můj projekt", body)

    def test_html_escaped_and_unknown_route(self):
        self.store.save_assignment({**self.fields, "title": '<script>alert("x")</script>'})
        status, _, body = self.request("/zadani/git-vagrant")
        self.assertEqual(status, 200)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertEqual(self.request("/nonexistent")[0], 404)

    def test_oauth_state_one_time_and_session_rotation(self):
        status, headers, _ = self.request("/auth/login")
        self.assertEqual(status, 303)
        old_cookie = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["state"][0]
        self.assertEqual(self.request("/auth/callback?state=bad&code=x", cookie=old_cookie)[0], 400)
        self.github.user = lambda code: self.user
        status, headers, _ = self.request(f"/auth/callback?state={state}&code=verified", cookie=old_cookie)
        self.assertEqual(status, 303)
        new_cookie = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        self.assertNotEqual(old_cookie, new_cookie)
        self.assertEqual(self.request(f"/auth/callback?state={state}&code=verified", cookie=old_cookie)[0], 400)
        self.assertIn("@Zak-Demo", self.request(cookie=new_cookie)[2])

    def test_oauth_code_and_tokens_not_in_responses(self):
        _, headers, _ = self.request("/auth/login")
        cookie = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["state"][0]
        self.github.user = lambda code: self.user
        response = self.request(f"/auth/callback?state={state}&code=SECRET_CODE", cookie=cookie)
        self.assertNotIn("SECRET_CODE", str(response))

    def test_logout_invalidates_old_session(self):
        cookie, session = self.login(admin=True)
        self.assertEqual(self.request("/logout", "POST", {"csrf": session["csrf"]}, cookie)[0], 303)
        self.assertEqual(self.request("/admin", cookie=cookie)[0], 403)

    def test_demo_login_disabled_in_live_mode(self):
        cookie, session = self.app.new_session()
        self.assertEqual(self.request("/demo/login", "POST", {"role": "teacher", "csrf": session["csrf"]}, cookie)[0], 404)

    def test_demo_flow_cannot_touch_github(self):
        self.config["DEMO"] = True
        self.config["DATABASE"] = str(Path(self.temp.name) / "demo.sqlite3")
        self.app = Application(self.config)
        cookie, session = self.app.new_session()
        status, headers, _ = self.request("/demo/login", "POST", {"role": "student", "csrf": session["csrf"]}, cookie)
        self.assertEqual(status, 303)
        new_cookie = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        _, _, body = self.request("/zadani/git-vagrant", cookie=new_cookie)
        csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]
        self.request("/projects/create", "POST", {"slug": "git-vagrant", "csrf": csrf}, new_cookie)
        _, _, body = self.request("/zadani/git-vagrant", cookie=new_cookie)
        self.assertIn("Na GitHubu žádný repozitář nevznikl", body)
        self.assertNotIn('href="https://github.com/sposdknl/zak-demo-git-vagrant"', body)


class GitHubTests(unittest.TestCase):
    def test_forbidden_diagnostic_identifies_operation_without_leaking_body(self):
        from urllib.error import HTTPError
        for suffix, operation in (("generate", "Vytvoření repozitáře"),
                                  ("collaborators/student", "Přidělení přístupu")):
            url = "https://api.github.com/repos/org/template/" + suffix
            error = HTTPError(url, 403, "Forbidden", {}, io.BytesIO(json.dumps({
                "message": "Resource not accessible by integration", "secret": "hidden-token"}).encode()))
            with patch("school_repos.github.urllib.request.urlopen", side_effect=error):
                with self.assertRaises(GitHubError) as caught:
                    request_json("POST", url, "hidden-token", {})
            self.assertEqual(caught.exception.status, 403)
            self.assertIn(operation, str(caught.exception))
            self.assertIn("schválení", str(caught.exception))
            self.assertNotIn("hidden-token", str(caught.exception))

    def test_repository_id_must_match_before_invitation(self):
        github = GitHub({"GITHUB_ORG": "sposdknl"})
        github.token = lambda: "token"
        with patch("school_repos.github.request_json", return_value=(200, {"id": 999})) as request:
            with self.assertRaises(GitHubError):
                github.grant("student-task", 123, "student")
            self.assertEqual(request.call_count, 1)

    def test_template_generation_and_write_permission(self):
        github = GitHub({"GITHUB_ORG": "sposdknl"})
        github.token = lambda: "token"
        with patch("school_repos.github.request_json", side_effect=[(201, {"id": 123}), (200, {"id": 123}), (201, {})]) as request:
            repo_id = github.create({"template": "sposdknl/git-vagrant", "title": "Git", "visibility": "private"}, "student-task")
            self.assertEqual(github.grant("student-task", repo_id, "student"), "invited")
            self.assertEqual(request.call_args_list[0].args[1], "https://api.github.com/repos/sposdknl/git-vagrant/generate")
            self.assertTrue(request.call_args_list[0].args[3]["private"])
            self.assertEqual(request.call_args_list[-1].args[3], {"permission": "push"})

    def test_real_rsa_jwt_signature_and_token_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key.pem"
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)], capture_output=True, check=True)
            github = GitHub({"GITHUB_APP_ID": "123", "GITHUB_INSTALLATION_ID": "456", "GITHUB_PRIVATE_KEY_FILE": str(key)})
            with patch("school_repos.github.request_json", return_value=(201, {"token": "installation", "expires_at": "2099-01-01T00:00:00Z"})) as request:
                self.assertEqual(github.token(), "installation")
                self.assertEqual(github.token(), "installation")
                self.assertEqual(request.call_count, 1)
                jwt = request.call_args.args[2]
            header, payload, signature = jwt.split(".")
            decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            self.assertEqual(json.loads(decode(header))["alg"], "RS256")
            self.assertEqual(json.loads(decode(payload))["iss"], "123")
            public = Path(directory) / "pub.pem"
            subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)], capture_output=True, check=True)
            sig_file = Path(directory) / "sig"
            sig_file.write_bytes(decode(signature))
            verify = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig_file)],
                                    input=f"{header}.{payload}".encode(), capture_output=True)
            self.assertEqual(verify.returncode, 0)


if __name__ == "__main__":
    unittest.main()
