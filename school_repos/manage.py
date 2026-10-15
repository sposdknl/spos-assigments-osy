"""Explicit operator recovery after interrupted external operations."""
import argparse
import json

from .github import DemoGitHub, GitHub, GitHubError, request_json
from .store import Store
from .web import load_config


def recover(store, github, config, slug, user_id, action):
    with store.connect() as db:
        # Keep a write lock during verification, so a concurrent request cannot
        # start provisioning while the operator repairs the reservation.
        db.execute("BEGIN IMMEDIATE")
        project = db.execute("SELECT * FROM projects WHERE assignment=? AND user_id=?", (slug, user_id)).fetchone()
        if not project:
            raise ValueError("Projekt neexistuje.")
        if action == "retry-access":
            if project["repo_id"] is None:
                raise ValueError("Chybí potvrzené ID repozitáře; nelze přidělit přístup.")
            db.execute("UPDATE projects SET status='error',error='Přístup lze znovu přidělit ze stránky zadání.' WHERE assignment=? AND user_id=?", (slug, user_id))
        elif action == "reset-uncreated":
            if project["repo_id"] is not None:
                raise ValueError("Repozitář má potvrzené ID. Použijte retry-access.")
            if not config["DEMO"]:
                try:
                    request_json("GET", f"https://api.github.com/repos/{config['GITHUB_ORG']}/{project['name']}", github.token())
                except GitHubError as exc:
                    if exc.status != 404:
                        raise
                else:
                    raise ValueError("Repozitář existuje. Reset odmítnut; nejprve ho ručně prověřte na GitHubu.")
            db.execute("DELETE FROM projects WHERE assignment=? AND user_id=?", (slug, user_id))


def main():
    parser = argparse.ArgumentParser(description="Správa a zotavení školních projektů; nic na GitHubu nemaže.")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--batch", action="store_true", help="Použít .env.batch a dávkovou databázi bez webové konfigurace")
    parser.add_argument("--database", help="Explicitní cesta k databázi")
    parser.add_argument("action", choices=("list", "retry-access", "reset-uncreated"))
    parser.add_argument("slug", nargs="?")
    parser.add_argument("user_id", nargs="?", type=int)
    args = parser.parse_args()
    try:
        if args.batch:
            if args.demo:
                parser.error("--batch a --demo nelze kombinovat.")
            from .batch import client
            from .config import read_environment
            config = read_environment(".env.batch")
            config["GITHUB_ORG"] = config.get("GITHUB_ORG", "sposdknl").lower()
            config["DEMO"] = False
            config["DATABASE"] = config.get("DATABASE") or f"data/batch-{config['GITHUB_ORG']}.sqlite3"
        else:
            config = load_config(args.demo)
        if args.database:
            config["DATABASE"] = args.database
        store = Store(config["DATABASE"])
        if args.action == "list":
            print(json.dumps([dict(row) for row in store.projects(args.slug)], ensure_ascii=False, indent=2))
        else:
            if not args.slug or args.user_id is None:
                parser.error("Zadejte identifikátor zadání a číselné GitHub ID žáka.")
            github = client(config) if args.batch else DemoGitHub() if args.demo else GitHub(config)
            recover(store, github, config, args.slug, args.user_id, args.action)
            print("Hotovo. Žák může pokračovat na stránce zadání.")
    except (ValueError, GitHubError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
