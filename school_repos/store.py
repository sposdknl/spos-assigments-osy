import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

LOGIN = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?\Z")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,59}\Z")
TEMPLATE = re.compile(r"[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+\Z")


class Store:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assignments (
                    slug TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                    template TEXT NOT NULL, visibility TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS roster (
                    assignment TEXT NOT NULL REFERENCES assignments(slug), login TEXT NOT NULL,
                    github_id INTEGER, PRIMARY KEY(assignment, login));
                CREATE TABLE IF NOT EXISTS projects (
                    assignment TEXT NOT NULL REFERENCES assignments(slug), user_id INTEGER NOT NULL,
                    login TEXT NOT NULL, name TEXT NOT NULL UNIQUE, repo_id INTEGER,
                    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                    PRIMARY KEY(assignment, user_id));
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user_id INTEGER, login TEXT,
                    csrf TEXT NOT NULL, oauth_state TEXT, expires INTEGER NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def assignments(self):
        with self.connect() as db:
            return db.execute("SELECT * FROM assignments ORDER BY title").fetchall()

    def assignment(self, slug):
        with self.connect() as db:
            return db.execute("SELECT * FROM assignments WHERE slug=?", (slug,)).fetchone()

    def roster(self, slug):
        with self.connect() as db:
            return db.execute("SELECT * FROM roster WHERE assignment=? ORDER BY login", (slug,)).fetchall()

    def allowed(self, slug, user):
        with self.connect() as db:
            row = db.execute("SELECT * FROM roster WHERE assignment=? AND login=?", (slug, user["login"].lower())).fetchone()
            return bool(row and (row["github_id"] is None or row["github_id"] == user["id"]))

    def save_assignment(self, fields):
        slug = fields.get("slug", "").strip()
        title = fields.get("title", "").strip()
        template = fields.get("template", "").strip()
        visibility = fields.get("visibility", "private")
        description = fields.get("description", "").strip()
        logins = sorted(set(x.lower() for x in re.split(r"[\s,;]+", fields.get("roster", "").strip()) if x))
        if not SLUG.fullmatch(slug) or not title or len(title) > 120:
            raise ValueError("Vyplňte název (nejvýše 120 znaků) a identifikátor z malých písmen, číslic a pomlček (nejvýše 60 znaků).")
        if not TEMPLATE.fullmatch(template) or visibility not in ("private", "public"):
            raise ValueError("Zadejte template ve tvaru organizace/repozitar a platnou viditelnost.")
        if len(description) > 5000 or len(logins) > 1000 or any(not LOGIN.fullmatch(x) for x in logins):
            raise ValueError("Zkontrolujte GitHub usernames (bez @). Nejvýše 1000 žáků a 5000 znaků popisu.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM assignments WHERE slug=?", (slug,)).fetchone()
            if old and db.execute("SELECT 1 FROM projects WHERE assignment=?", (slug,)).fetchone():
                if old["template"] != template or old["visibility"] != visibility:
                    raise ValueError("Po zahájení zakládání projektů nelze změnit template ani viditelnost. Založte nové zadání.")
            db.execute("""INSERT INTO assignments VALUES (?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET
                title=excluded.title, description=excluded.description, template=excluded.template,
                visibility=excluded.visibility, active=excluded.active""",
                       (slug, title, description, template, visibility, int(fields.get("active") == "1")))
            retained = {r["login"]: r["github_id"] for r in db.execute("SELECT * FROM roster WHERE assignment=?", (slug,))}
            db.execute("DELETE FROM roster WHERE assignment=?", (slug,))
            db.executemany("INSERT INTO roster VALUES (?,?,?)", [(slug, x, retained.get(x)) for x in logins])

    def projects(self, slug=None):
        with self.connect() as db:
            return db.execute("SELECT * FROM projects" + (" WHERE assignment=?" if slug else "") +
                              " ORDER BY created_at DESC", (slug,) if slug else ()).fetchall()

    def project(self, slug, user_id):
        with self.connect() as db:
            return db.execute("SELECT * FROM projects WHERE assignment=? AND user_id=?", (slug, user_id)).fetchone()

    def provision(self, slug, user, github):
        # Reserve in SQLite before any external side effect. Concurrent processes cannot
        # generate duplicates. A crashed/uncertain creation is deliberately not adopted.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assignment = db.execute("SELECT * FROM assignments WHERE slug=?", (slug,)).fetchone()
            roster = db.execute("SELECT * FROM roster WHERE assignment=? AND login=?", (slug, user["login"].lower())).fetchone()
            if not assignment or not assignment["active"] or not roster or roster["github_id"] not in (None, user["id"]):
                raise PermissionError("Pro toto zadání nemáte povolené zakládání projektů.")
            project = db.execute("SELECT * FROM projects WHERE assignment=? AND user_id=?", (slug, user["id"])).fetchone()
            if project and project["status"] == "creating":
                raise ValueError("Projekt se zakládá nebo předchozí pokus nebyl dokončen. Stav musí ověřit učitel.")
            if project and project["status"] in ("ready", "invited"):
                return project
            if project and project["repo_id"] is None:
                raise ValueError("Předchozí vytvoření nebylo potvrzeno. Učitel musí zkontrolovat GitHub; aplikace nepřevezme existující repozitář.")
            name = project["name"] if project else f"{user['login'].lower()}-{slug}"
            if project:
                db.execute("UPDATE projects SET status='creating', error='' WHERE assignment=? AND user_id=?", (slug, user["id"]))
            else:
                db.execute("INSERT INTO projects VALUES (?,?,?,?,NULL,'creating','',?)",
                           (slug, user["id"], user["login"], name, int(time.time())))
            db.execute("UPDATE roster SET github_id=? WHERE assignment=? AND login=?", (user["id"], slug, user["login"].lower()))
        try:
            repo_id = project["repo_id"] if project else None
            if repo_id is None:
                repo_id = github.create(assignment, name)
                with self.connect() as db:
                    db.execute("UPDATE projects SET repo_id=? WHERE assignment=? AND user_id=?", (repo_id, slug, user["id"]))
            status = github.grant(name, repo_id, user["login"])
            with self.connect() as db:
                db.execute("UPDATE projects SET status=?,error='' WHERE assignment=? AND user_id=?", (status, slug, user["id"]))
        except Exception as exc:
            with self.connect() as db:
                db.execute("UPDATE projects SET status='error',error=? WHERE assignment=? AND user_id=?",
                           (str(exc)[:500], slug, user["id"]))
            raise
        return self.project(slug, user["id"])
