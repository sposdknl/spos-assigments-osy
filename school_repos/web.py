import argparse
import hashlib
import html
import json
import os
import secrets
import sqlite3
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .github import DemoGitHub, GitHub, GitHubError
from .config import read_environment
from .store import LOGIN, Store

ROOT = Path(__file__).resolve().parent.parent
STATUS = {"creating": "Zakládání", "invited": "Pozvánka odeslána", "ready": "Přístup připraven", "error": "Vyžaduje kontrolu"}


def esc(value):
    return html.escape(str(value), quote=True)


def load_config(demo=False):
    config = read_environment()
    config.setdefault("APP_URL", "http://localhost:8000")
    config["APP_URL"] = config["APP_URL"].rstrip("/")
    config.setdefault("GITHUB_ORG", "sposdknl")
    config.setdefault("DATABASE", str(ROOT / "data" / "school.sqlite3"))
    config.setdefault("PORT", "8000")
    config["DEMO"] = demo
    if demo:
        config["DATABASE"] = str(ROOT / "data" / "demo.sqlite3")
        config["ADMIN_IDS"] = {1}
    else:
        required = ("GITHUB_APP_ID", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_INSTALLATION_ID", "GITHUB_PRIVATE_KEY_FILE", "ADMIN_GITHUB_IDS")
        missing = [key for key in required if not config.get(key)]
        if missing:
            raise ValueError("Chybí konfigurace: " + ", ".join(missing) + ". Viz .env.example; pro ukázku použijte --demo.")
        config["ADMIN_IDS"] = {int(x.strip()) for x in config["ADMIN_GITHUB_IDS"].split(",") if x.strip()}
        if not config["ADMIN_IDS"]:
            raise ValueError("Nastavte alespoň jedno číselné ID učitele.")
        if not Path(config["GITHUB_PRIVATE_KEY_FILE"]).is_file():
            raise ValueError("Soubor s privátním klíčem GitHub App neexistuje.")
    url = urllib.parse.urlsplit(config["APP_URL"])
    if url.scheme not in ("http", "https") or not url.netloc or url.path or url.query or url.fragment:
        raise ValueError("APP_URL musí být základní URL bez cesty, např. https://projekty.skola.cz.")
    if not demo and url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("Mimo localhost musí APP_URL používat HTTPS.")
    if not LOGIN.fullmatch(config["GITHUB_ORG"]):
        raise ValueError("Neplatný název GitHub organizace.")
    return config


class Application:
    def __init__(self, config, github=None):
        self.config = config
        self.store = Store(config["DATABASE"])
        self.github = github or (DemoGitHub() if config["DEMO"] else GitHub(config))
        if config["DEMO"] and not self.store.assignment("git-vagrant"):
            self.store.save_assignment({"slug": "git-vagrant", "title": "Git a Vagrant",
                "description": "Vytvořte svůj školní projekt a procvičte si práci s Gitem a virtuálním strojem.",
                "template": "sposdknl/git-vagrant", "visibility": "private", "active": "1", "roster": "zak-demo"})

    def new_session(self, user=None):
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (int(time.time()),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,NULL,?)", (
                digest, user["id"] if user else None, user["login"] if user else None, csrf, int(time.time()) + 8 * 3600))
        return raw, {"token": digest, "user_id": user["id"] if user else None,
                     "login": user["login"] if user else None, "csrf": csrf}

    def handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SchoolRepos"

            def log_message(self, format, *args):
                # OAuth callback query strings contain short-lived codes: never log URLs.
                pass

            def respond(self, body="", status=200, content_type="text/html; charset=utf-8", location=None):
                payload = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
                if getattr(self, "new_cookie", None):
                    secure = "; Secure" if app.config["APP_URL"].startswith("https://") else ""
                    self.send_header("Set-Cookie", f"school_session={self.new_cookie}; HttpOnly; SameSite=Lax; Path=/; Max-Age=28800{secure}")
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(payload)

            def redirect(self, location):
                self.respond(status=303, location=location)

            def session(self):
                self.new_cookie = None
                cookies = SimpleCookie()
                try:
                    cookies.load(self.headers.get("Cookie", ""))
                except Exception:
                    cookies = SimpleCookie()
                raw = cookies.get("school_session")
                if raw:
                    digest = hashlib.sha256(raw.value.encode()).hexdigest()
                    with app.store.connect() as db:
                        row = db.execute("SELECT * FROM sessions WHERE token=? AND expires>?", (digest, int(time.time()))).fetchone()
                    if row:
                        self.sess = dict(row)
                        return
                self.new_cookie, self.sess = app.new_session()

            @property
            def user(self):
                return {"id": self.sess["user_id"], "login": self.sess["login"]} if self.sess["user_id"] is not None else None

            @property
            def admin(self):
                return bool(self.user and self.user["id"] in app.config["ADMIN_IDS"])

            def csrf(self):
                return f'<input type="hidden" name="csrf" value="{esc(self.sess["csrf"])}">'

            def layout(self, title, body, status=200):
                identity = (f'<span class="identity">@{esc(self.user["login"])}</span><form action="/logout" method="post">{self.csrf()}<button class="quiet">Odhlásit</button></form>'
                            if self.user else '<a class="button small" href="/auth/login">Přihlásit přes GitHub</a>')
                demo = '<div class="demo">UKÁZKOVÝ REŽIM · Repozitáře se na GitHubu nevytvářejí.</div>' if app.config["DEMO"] else ""
                self.respond(f'''<!doctype html><html lang="cs"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
                    <title>{esc(title)} · Školní projekty</title><link rel="stylesheet" href="/static/style.css"></head>
                    <body>{demo}<header><a class="brand" href="/"><span class="mark">S</span> Školní projekty <span class="org">/ {esc(app.config['GITHUB_ORG'])}</span></a>
                    <nav>{'<a href="/admin">Správa zadání</a>' if self.admin else ''}{identity}</nav></header>
                    <main>{body}</main><footer>SPŠ a SOŠ Dvůr Králové nad Labem <span>GitHub projekty pro výuku OSY</span></footer></body></html>''', status)

            def do_GET(self):
                try:
                    self.session()
                    self.get_route()
                except (ValueError, PermissionError, GitHubError) as exc:
                    self.error_page(str(exc), 403 if isinstance(exc, PermissionError) else 400)
                except Exception:
                    self.error_page("Nastala chyba serveru. Obraťte se na správce.", 500)

            def error_page(self, message, status):
                self.layout("Požadavek se nezdařil", f'<section class="panel"><p class="eyebrow">Požadavek se nezdařil</p><h1>Je potřeba kontrola</h1><p>{esc(message)}</p><a class="button secondary" href="/">Zpět na zadání</a></section>', status)

            def get_route(self):
                url = urllib.parse.urlsplit(self.path)
                path = url.path
                if path == "/static/style.css":
                    return self.respond((ROOT / "static" / "style.css").read_bytes(), content_type="text/css; charset=utf-8")
                if path == "/health":
                    return self.respond('{"status":"ok"}', content_type="application/json")
                if path == "/auth/login":
                    if app.config["DEMO"]:
                        return self.layout("Vyzkoušet aplikaci", f'''<section class="panel narrow"><p class="eyebrow">Ukázka aplikace</p><h1>Vyberte si roli</h1><p>Vyzkoušejte celý průchod bez GitHub účtu. Data zůstávají jen v ukázkové databázi.</p>
                            <form method="post" action="/demo/login">{self.csrf()}<button name="role" value="student">Pokračovat jako žák</button><button class="secondary" name="role" value="teacher">Pokračovat jako učitel</button></form></section>''')
                    state = secrets.token_urlsafe(32)
                    with app.store.connect() as db:
                        db.execute("UPDATE sessions SET oauth_state=?,expires=? WHERE token=?", (state, int(time.time()) + 600, self.sess["token"]))
                    params = urllib.parse.urlencode({"client_id": app.config["GITHUB_CLIENT_ID"], "state": state,
                        "redirect_uri": app.config["APP_URL"] + "/auth/callback"})
                    return self.redirect("https://github.com/login/oauth/authorize?" + params)
                if path == "/auth/callback" and not app.config["DEMO"]:
                    query = urllib.parse.parse_qs(url.query)
                    state = query.get("state", [""])[0]
                    code = query.get("code", [""])[0]
                    with app.store.connect() as db:
                        cursor = db.execute("UPDATE sessions SET oauth_state=NULL WHERE token=? AND oauth_state=? AND expires>?",
                                            (self.sess["token"], state, int(time.time())))
                        valid = bool(state and cursor.rowcount == 1)
                    if not valid or not code:
                        raise ValueError("Přihlášení vypršelo nebo nebylo potvrzeno. Přihlaste se znovu.")
                    user = app.github.user(code)
                    self.rotate_session(user)
                    return self.redirect("/")
                if path == "/":
                    return self.home()
                if path == "/admin":
                    self.require_admin()
                    return self.admin_page()
                if path == "/admin/new":
                    self.require_admin()
                    return self.assignment_form()
                if path.startswith("/admin/edit/"):
                    self.require_admin()
                    assignment = app.store.assignment(path.removeprefix("/admin/edit/"))
                    if assignment:
                        return self.assignment_form(assignment)
                if path.startswith("/zadani/"):
                    assignment = app.store.assignment(path.removeprefix("/zadani/"))
                    if assignment:
                        return self.assignment_page(assignment)
                return self.error_page("Stránka neexistuje.", 404)

            def rotate_session(self, user):
                with app.store.connect() as db:
                    db.execute("DELETE FROM sessions WHERE token=?", (self.sess["token"],))
                self.new_cookie, self.sess = app.new_session(user)

            def require_admin(self):
                if not self.admin:
                    raise PermissionError("Tato stránka je dostupná pouze učitelům.")

            def home(self):
                assignments = [a for a in app.store.assignments() if self.admin or
                    (self.user and app.store.allowed(a["slug"], self.user))]
                if not self.user:
                    intro = '<p class="eyebrow">OSY / školní pracovna</p><h1>Vaše zadání.<br>Váš vlastní projekt.</h1><p class="lead">Přihlaste se GitHub účtem. Vyberte zadání a vytvořte si projekt ve školní organizaci.</p><a class="button" href="/auth/login">Přihlásit přes GitHub <span>↗</span></a>'
                else:
                    intro = f'<p class="eyebrow">OSY / školní pracovna</p><h1>{"Přehled výuky" if self.admin else "Moje zadání"}</h1><p class="lead">{"Spravujte zadání, seznamy žáků a jejich školní projekty." if self.admin else "Zadání máte připravená. Každý projekt vznikne pod vaším GitHub jménem."}</p>'
                cards = "".join(self.card(a) for a in assignments)
                if self.user and not assignments:
                    cards = '<div class="empty">Zatím tu nemáte žádné zadání. Požádejte učitele o přidání vašeho GitHub username do seznamu žáků.</div>'
                steps = '<section class="steps"><div><b>01</b><h3>Přihlaste se</h3><p>Použijte svůj vlastní GitHub účet.</p></div><div><b>02</b><h3>Vytvořte projekt</h3><p>Dostanete vlastní kopii školního template.</p></div><div><b>03</b><h3>Začněte pracovat</h3><p>Přijměte pozvánku a stáhněte si repozitář.</p></div></section>'
                self.layout("Zadání", f'<section class="hero">{intro}<div class="hero-art" aria-hidden="true"><span>git clone</span><strong>~/moje-prace</strong><code>main <i>●</i> připraveno</code></div></section><section class="cards">{cards}</section>{steps if not self.user else ""}')

            def card(self, a):
                project = app.store.project(a["slug"], self.user["id"]) if self.user else None
                badge = STATUS[project["status"]] if project else ("Otevřeno" if a["active"] else "Uzavřeno")
                return f'''<a class="card" href="/zadani/{esc(a['slug'])}"><div class="card-top"><span class="folder">⌘</span><span class="badge">{badge}</span></div>
                    <h2>{esc(a['title'])}</h2><p>{esc(a['description'][:170])}</p><div class="card-bottom"><code>{esc(a['template'])}</code><span>Otevřít →</span></div></a>'''

            def assignment_page(self, a):
                project = app.store.project(a["slug"], self.user["id"]) if self.user else None
                permitted = self.user and app.store.allowed(a["slug"], self.user)
                name = project["name"] if project else f'{self.user["login"].lower() if self.user else "username"}-{a["slug"]}'
                action = ""
                if project:
                    action += f'<div class="notice"><strong>{STATUS[project["status"]]}</strong>'
                    if project["repo_id"]:
                        if app.config["DEMO"]:
                            action += '<p>Ukázkový projekt je připravený. Na GitHubu žádný repozitář nevznikl.</p>'
                        else:
                            href = f'https://github.com/{app.config["GITHUB_ORG"]}/{project["name"]}'
                            action += f'<p><a href="{esc(href)}" rel="noreferrer">Otevřít repozitář ↗</a></p>'
                            action += f'<p>Přijměte případnou pozvánku v e-mailu nebo na <a href="{esc(href)}/invitations">stránce pozvánky</a>. Přístup k zápisu získáte po jejím přijetí.</p>'
                    if project["error"]:
                        action += f'<p>{esc(project["error"])}</p>'
                    action += '</div>'
                if not self.user:
                    action += '<a class="button" href="/auth/login">Přihlásit přes GitHub</a>'
                elif not permitted:
                    action += '<p class="notice">Váš účet není na seznamu žáků tohoto zadání. Obraťte se na učitele.</p>'
                elif not a["active"]:
                    action += '<p class="notice">Zakládání nových projektů je uzavřené.</p>'
                elif not project or (project["status"] == "error" and project["repo_id"]):
                    action += f'<form action="/projects/create" method="post">{self.csrf()}<input type="hidden" name="slug" value="{esc(a["slug"])}"><button>{"Zopakovat přidělení přístupu" if project else "Vytvořit můj projekt →"}</button></form>'
                self.layout(a["title"], f'''<a class="back" href="/">← Všechna zadání</a><section class="detail"><div><p class="eyebrow">Zadání / OSY</p><h1>{esc(a['title'])}</h1><p class="description">{esc(a['description'])}</p>{action}</div><aside class="panel"><h3>Váš repozitář</h3><code class="repo-name">{esc(app.config['GITHUB_ORG'])}/{esc(name)}</code><dl><dt>Zdrojový template</dt><dd>{esc(a['template'])}</dd><dt>Viditelnost</dt><dd>{'Soukromý' if a['visibility']=='private' else 'Veřejný'}</dd><dt>Vaše oprávnění</dt><dd>Zápis (Write)</dd></dl><p class="muted">Projekt vlastní škola. Commity vytváříte pod svým účtem.</p></aside></section>''')

            def admin_page(self):
                cards = "".join(f'<div class="admin-item">{self.card(a)}<div class="admin-links"><a href="/admin/edit/{esc(a["slug"])}">Upravit zadání a žáky</a><span>{len(app.store.roster(a["slug"]))} žáků · {len(app.store.projects(a["slug"]))} projektů</span></div></div>' for a in app.store.assignments())
                rows = ""
                for p in app.store.projects():
                    repo = esc(p["name"])
                    if p["repo_id"] and not app.config["DEMO"]:
                        repo = f'<a href="https://github.com/{esc(app.config["GITHUB_ORG"])}/{esc(p["name"])}">{repo} ↗</a>'
                    rows += f'<tr><td>@{esc(p["login"])}</td><td>{esc(p["assignment"])}</td><td>{repo}</td><td>{STATUS[p["status"]]}<small>{esc(p["error"])}</small></td></tr>'
                table = f'<div class="table-wrap"><table><thead><tr><th>Žák</th><th>Zadání</th><th>Repozitář</th><th>Stav</th></tr></thead><tbody>{rows}</tbody></table></div>' if rows else '<p class="empty">Žáci zatím nevytvořili žádné projekty.</p>'
                self.layout("Správa zadání", f'<div class="page-title"><div><p class="eyebrow">Učitelská administrace</p><h1>Správa zadání</h1></div><a class="button" href="/admin/new">+ Nové zadání</a></div><section class="cards">{cards}</section><section class="panel"><h2>Žákovské projekty</h2>{table}</section>')

            def assignment_form(self, assignment=None, error="", fields=None):
                a = fields if fields is not None else dict(assignment) if assignment else {
                    "slug": "", "title": "", "description": "", "template": "sposdknl/git-vagrant", "visibility": "private", "active": 1}
                roster = a.get("roster", "\n".join(r["login"] for r in app.store.roster(a["slug"])))
                locked = 'readonly' if assignment else ''
                banner = f'<p class="notice">{esc(error)}</p>' if error else ''
                self.layout("Upravit zadání" if assignment else "Nové zadání", f'''<a class="back" href="/admin">← Správa zadání</a><section class="panel form-panel"><p class="eyebrow">Učitelská administrace</p><h1>{'Upravit zadání' if assignment else 'Nové zadání'}</h1>{banner}<form method="post" action="/admin/save">{self.csrf()}
                    <label>Název zadání<input name="title" required maxlength="120" value="{esc(a['title'])}" placeholder="Git a Vagrant"></label>
                    <label>Identifikátor zadání<input name="slug" required maxlength="60" pattern="[a-z0-9][a-z0-9-]*" {locked} value="{esc(a['slug'])}" placeholder="osy-2026-git-vagrant"><small>Určuje odkaz a název repozitáře: username-identifikator. Pro další ročník použijte nový identifikátor.</small></label>
                    <label>Popis zadání<textarea name="description" maxlength="5000" rows="4">{esc(a['description'])}</textarea></label>
                    <div class="form-grid"><label>Zdrojový template<input name="template" required value="{esc(a['template'])}"></label><label>Viditelnost<select name="visibility"><option value="private" {'selected' if a['visibility']=='private' else ''}>Soukromý</option><option value="public" {'selected' if a['visibility']=='public' else ''}>Veřejný</option></select></label></div>
                    <label>Povolení žáci<textarea name="roster" rows="7" placeholder="novak-jan\nsvobodova-eva">{esc(roster)}</textarea><small>GitHub usernames bez @, jeden na řádek. Lze vložit také seznam oddělený čárkami. Prázdný seznam nepovolí žádného žáka.</small></label>
                    <label class="checkbox"><input type="checkbox" name="active" value="1" {'checked' if str(a.get('active'))=='1' else ''}> Povolit zakládání projektů</label>
                    <div class="form-actions"><button>Uložit zadání</button><a href="/admin">Zrušit</a></div></form></section>''', 400 if error else 200)

            def do_POST(self):
                try:
                    self.session()
                    if self.headers.get("Origin") and self.headers["Origin"] != app.config["APP_URL"]:
                        raise PermissionError("Požadavek pochází z jiné stránky.")
                    if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                        raise ValueError("Nepodporovaný formát formuláře.")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 100_000:
                        return self.error_page("Formulář je příliš velký nebo prázdný.", 413)
                    fields = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(size).decode(), keep_blank_values=True).items()}
                    if not secrets.compare_digest(fields.get("csrf", ""), self.sess["csrf"]):
                        raise PermissionError("Platnost formuláře vypršela. Obnovte stránku.")
                    path = urllib.parse.urlsplit(self.path).path
                    if path == "/logout":
                        self.rotate_session(None)
                        return self.redirect("/")
                    if path == "/demo/login" and app.config["DEMO"]:
                        teacher = fields.get("role") == "teacher"
                        self.rotate_session({"id": 1 if teacher else 2, "login": "ucitel-demo" if teacher else "zak-demo"})
                        return self.redirect("/admin" if teacher else "/")
                    if path == "/admin/save":
                        self.require_admin()
                        try:
                            app.store.save_assignment(fields)
                        except ValueError as exc:
                            return self.assignment_form(app.store.assignment(fields.get("slug", "")), str(exc), fields)
                        return self.redirect("/admin")
                    if path == "/projects/create":
                        if not self.user:
                            raise PermissionError("Nejprve se přihlaste.")
                        slug = fields.get("slug", "")
                        try:
                            app.store.provision(slug, self.user, app.github)
                        except GitHubError:
                            # Durable error and retry UI are on the assignment page.
                            pass
                        return self.redirect("/zadani/" + urllib.parse.quote(slug, safe=""))
                    self.error_page("Stránka neexistuje.", 404)
                except (ValueError, PermissionError, GitHubError, sqlite3.IntegrityError) as exc:
                    self.error_page(str(exc) if not isinstance(exc, sqlite3.IntegrityError) else "Tento název projektu už je obsazený.", 403 if isinstance(exc, PermissionError) else 400)
                except Exception:
                    self.error_page("Nastala chyba serveru. Obraťte se na správce.", 500)

        return Handler


def main():
    parser = argparse.ArgumentParser(description="Školní projekty na GitHubu")
    parser.add_argument("--demo", action="store_true", help="Ukázka bez přístupu ke GitHubu, s oddělenou databází")
    parser.add_argument("--host", default="127.0.0.1", help="Adresa naslouchání (výchozí 127.0.0.1)")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    try:
        config = load_config(args.demo)
        app = Application(config)
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")
    port = args.port or int(config["PORT"])
    server = ThreadingHTTPServer((args.host, port), app.handler())
    print(f"Školní projekty: http://{args.host}:{port}" + (" (UKÁZKA, bez GitHubu)" if args.demo else ""), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
