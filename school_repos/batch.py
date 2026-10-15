"""One-shot provisioning for a class roster. Default execution is an offline plan."""
import argparse
import csv
import re
import sys
from pathlib import Path

from .config import read_environment
from .github import GitHub, GitHubError, TokenGitHub
from .store import LOGIN, SLUG, TEMPLATE, Store


def read_students(path):
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    names = [x.lower() for line in lines if not line.strip().startswith("#")
             for x in re.split(r"[\s,;]+", line.strip()) if x]
    names = list(dict.fromkeys(names))
    if not names or len(names) > 1000 or any(not LOGIN.fullmatch(x) for x in names):
        raise ValueError("Seznam musí obsahovat 1 až 1000 GitHub usernames bez @; jeden na řádek nebo oddělené čárkami. Bez hlavičky.")
    return names


def validate(args, config):
    org = config.get("GITHUB_ORG", "sposdknl").lower()
    slug = f"{args.cohort}-{args.assignment}"
    if not LOGIN.fullmatch(org) or not SLUG.fullmatch(args.cohort) or not SLUG.fullmatch(args.assignment) or not SLUG.fullmatch(slug):
        raise ValueError("Organizace musí být platné GitHub jméno; ročník a zadání tvoří malá písmena, číslice a pomlčky (dohromady nejvýše 60 znaků).")
    if not TEMPLATE.fullmatch(args.template):
        raise ValueError("Template zadejte jako organizace/repozitar.")
    title = args.title or args.assignment
    if not title.strip() or len(title) > 120:
        raise ValueError("Název zadání musí mít 1 až 120 znaků.")
    return org, {"slug": slug, "title": title, "description": f"Ročník {args.cohort}",
                 "template": args.template, "visibility": args.visibility, "active": "1"}


def client(config):
    if config.get("GITHUB_TOKEN"):
        return TokenGitHub(config)
    required = ("GITHUB_APP_ID", "GITHUB_INSTALLATION_ID", "GITHUB_PRIVATE_KEY_FILE")
    if any(not config.get(key) for key in required):
        raise ValueError("Pro --apply nastavte GITHUB_TOKEN nebo GitHub App (APP_ID, INSTALLATION_ID, PRIVATE_KEY_FILE). Viz .env.batch.example.")
    if not Path(config["GITHUB_PRIVATE_KEY_FILE"]).is_file():
        raise ValueError("Privátní klíč GitHub App neexistuje.")
    return GitHub(config)


def execute(store, github, config, fields, names, output=print):
    # Check every account before the first remote write: a typo does not create
    # only part of the class. Bind stable IDs to prevent username reassignment.
    github.preflight(fields["template"])
    users = [github.lookup_user(name) for name in names]
    for name, user in zip(names, users):
        if not LOGIN.fullmatch(user["login"]) or user["login"].lower() != name:
            raise ValueError(f"Účet {name} změnil jméno. Opravte seznam podle GitHubu.")
    if len({u["id"] for u in users}) != len(users):
        raise ValueError("Seznam obsahuje více jmen pro stejný GitHub účet.")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS batch_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        old_org = db.execute("SELECT value FROM batch_config WHERE key='organization'").fetchone()
        if old_org and old_org["value"] != config["GITHUB_ORG"]:
            raise ValueError("Tato databáze patří jiné organizaci. Použijte samostatnou databázi.")
        db.execute("INSERT OR IGNORE INTO batch_config VALUES ('organization',?)", (config["GITHUB_ORG"],))
        for user in users:
            pinned = db.execute("SELECT github_id FROM roster WHERE assignment=? AND login=?",
                                (fields["slug"], user["login"].lower())).fetchone()
            if pinned and pinned["github_id"] not in (None, user["id"]):
                raise ValueError(f"Účet {user['login']} má jiné GitHub ID než při předchozím spuštění.")
    store.save_assignment({**fields, "roster": "\n".join(u["login"] for u in users)})
    results = []
    for user in users:
        name = f"{user['login'].lower()}-{fields['slug']}"
        result = {"username": user["login"], "github_id": user["id"], "repository": name,
                  "url": f"https://github.com/{config['GITHUB_ORG']}/{name}", "status": "error", "error": ""}
        try:
            project = store.provision(fields["slug"], user, github)
            result.update(repository=project["name"], status=project["status"])
            result["url"] = f"https://github.com/{config['GITHUB_ORG']}/{project['name']}"
        except (GitHubError, ValueError, PermissionError) as exc:
            result["error"] = str(exc)
        output(f"{result['status']:8} {result['url']}" + (f" — {result['error']}" if result["error"] else ""))
        results.append(result)
    return results


def write_report(path, results):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("username", "github_id", "repository", "url", "status", "error"))
        writer.writeheader()
        writer.writerows(results)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jednorázové založení školních repozitářů. Bez --apply pouze vypíše plán, bez přístupu k síti.")
    parser.add_argument("--students", required=True, help="Textový seznam GitHub usernames bez hlavičky")
    parser.add_argument("--cohort", required=True, help="Např. 2a-2026")
    parser.add_argument("--assignment", required=True, help="Např. git-vagrant")
    parser.add_argument("--template", default="sposdknl/git-vagrant")
    parser.add_argument("--title")
    parser.add_argument("--visibility", choices=("private", "public"), default="private")
    parser.add_argument("--env-file", default=".env.batch")
    parser.add_argument("--database", help="SQLite se záznamy pro opakované spuštění")
    parser.add_argument("--report", help="CSV s výsledky; výchozí data/vysledky-<rocnik>-<zadani>.csv")
    parser.add_argument("--apply", action="store_true", help="Opravdu vytvořit repozitáře a přidělit Write přístup")
    args = parser.parse_args(argv)
    try:
        config = read_environment(args.env_file)
        org, fields = validate(args, config)
        config["GITHUB_ORG"] = org
        names = read_students(args.students)
        print(f"Organizace: {org} | template: {fields['template']} | viditelnost: {fields['visibility']}")
        for name in names:
            print(f"  {org}/{name}-{fields['slug']} → @{name}: Write")
        if not args.apply:
            print(f"Plán: {len(names)} repozitářů. Bez změn na GitHubu. Pro provedení přidejte --apply.")
            return 0
        github = client(config)
        store = Store(args.database or config.get("DATABASE") or f"data/batch-{org}.sqlite3")
        results = execute(store, github, config, fields, names)
        report = args.report or f"data/vysledky-{fields['slug']}.csv"
        write_report(report, results)
        failed = sum(r["status"] == "error" for r in results)
        print(f"Hotovo: {len(results)-failed} úspěšných, {failed} chyb. Výsledky: {report}")
        return 1 if failed else 0
    except (ValueError, OSError, GitHubError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
