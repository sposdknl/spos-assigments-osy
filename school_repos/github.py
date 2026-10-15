"""GitHub App authentication and repository operations, without third-party packages."""

import base64
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


class GitHubError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def request_json(method, url, token=None, data=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "spos-school-repos",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers, method=method), timeout=25) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # GitHub response bodies and tokens must never reach HTML or logs.
        raise GitHubError(exc.code, f"GitHub API vrátilo HTTP {exc.code}.") from None
    except (OSError, ValueError) as exc:
        raise GitHubError(503, "GitHub není dostupný nebo vrátil neplatnou odpověď.") from exc


class GitHub:
    def __init__(self, config):
        self.config = config
        self._token = None
        self._expires = 0

    def user(self, code):
        _, result = request_json("POST", "https://github.com/login/oauth/access_token", data={
            "client_id": self.config["GITHUB_CLIENT_ID"],
            "client_secret": self.config["GITHUB_CLIENT_SECRET"],
            "code": code, "redirect_uri": self.config["APP_URL"] + "/auth/callback"})
        if "access_token" not in result:
            raise GitHubError(401, "Přihlášení se nezdařilo. Zkuste ho znovu.")
        # User token is used only to identify the user, never to create repositories.
        _, user = request_json("GET", "https://api.github.com/user", result["access_token"])
        return {"id": int(user["id"]), "login": user["login"]}

    def lookup_user(self, login):
        _, user = request_json("GET", "https://api.github.com/users/" + urllib.parse.quote(login), self.token())
        if user.get("type") != "User":
            raise GitHubError(400, f"Účet {login} není osobní GitHub účet.")
        return {"id": int(user["id"]), "login": user["login"]}

    def preflight(self, template):
        _, template_repo = request_json("GET", f"https://api.github.com/repos/{template}", self.token())
        if not template_repo.get("is_template"):
            raise GitHubError(400, "Zdrojový repozitář není označený jako template.")
        _, org = request_json("GET", f"https://api.github.com/orgs/{self.config['GITHUB_ORG']}", self.token())
        if org["login"].lower() != self.config["GITHUB_ORG"].lower():
            raise GitHubError(400, "Nesouhlasí cílová organizace.")

    def token(self):
        if self._token and self._expires > time.time() + 60:
            return self._token
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
        now = int(time.time())
        signing = encode({"alg": "RS256", "typ": "JWT"}) + b"." + encode({
            "iat": now - 60, "exp": now + 540, "iss": self.config["GITHUB_APP_ID"]})
        try:
            result = subprocess.run(["openssl", "dgst", "-sha256", "-sign",
                                     self.config["GITHUB_PRIVATE_KEY_FILE"]],
                                    input=signing, capture_output=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitHubError(503, "Nelze podepsat přihlášení GitHub App. Učitel musí ověřit privátní klíč.") from exc
        jwt = (signing + b"." + base64.urlsafe_b64encode(result.stdout).rstrip(b"=")).decode()
        installation = self.config["GITHUB_INSTALLATION_ID"]
        _, result = request_json("POST", f"https://api.github.com/app/installations/{installation}/access_tokens", jwt, {})
        self._token = result["token"]
        self._expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00")).timestamp()
        return self._token

    def create(self, assignment, name):
        template = assignment["template"]
        _, repo = request_json("POST", f"https://api.github.com/repos/{template}/generate", self.token(), {
            "owner": self.config["GITHUB_ORG"], "name": name,
            "description": f"Školní projekt: {assignment['title']}",
            "private": assignment["visibility"] == "private", "include_all_branches": False})
        return int(repo["id"])

    def grant(self, name, repo_id, login):
        path = f"https://api.github.com/repos/{self.config['GITHUB_ORG']}/{name}"
        _, repo = request_json("GET", path, self.token())
        if int(repo["id"]) != repo_id:
            raise GitHubError(409, "Repozitář byl nahrazen jiným. Přístup musí zkontrolovat učitel.")
        status, _ = request_json("PUT", path + "/collaborators/" + urllib.parse.quote(login),
                                 self.token(), {"permission": "push"})
        return "invited" if status == 201 else "ready"


class DemoGitHub:
    def create(self, assignment, name):
        return int(time.time_ns() % 1_000_000_000)

    def grant(self, name, repo_id, login):
        return "ready"


class TokenGitHub(GitHub):
    """One-shot operator authentication; no OAuth callback or running server."""
    def token(self):
        return self.config["GITHUB_TOKEN"]
