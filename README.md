# Školní projekty na GitHubu

Malá česká webová aplikace pro předmět OSY. Učitel připraví zadání a seznam GitHub účtů; žák se přihlásí a jedním tlačítkem získá svůj repozitář v organizaci `sposdknl`, například:

```text
sposdknl/novak-jan-osy-2026-git-vagrant
```

Obsah se vytvoří ze zvoleného template, například `sposdknl/git-vagrant`. Organizace vlastní repozitář, žák dostane oprávnění **Write**. Pokud GitHub vystaví pozvánku, žák ji musí přijmout. Žáci nepotřebují oprávnění zakládat repozitáře ani členství v organizaci, pokud organizace dovoluje externí spolupracovníky.

## Jednorázové založení pro celý ročník (bez serveru)

Pro hromadné založení nepotřebujete web, doménu ani trvale běžící aplikaci. Dávka si vezme seznam GitHub usernames, vytvoří každému žákovi soukromý repozitář ze zadaného template a přidá jej s právem **Write**. Žák přijme pozvánku přímo na GitHubu. Potom můžete kontejner vypnout a odstranit; přístup žáků i repozitáře zůstanou na GitHubu.

### Příprava

```bash
cp .env.batch.example .env.batch
cp students.example.txt students.txt
```

Do `students.txt` vložte skutečné GitHub usernames, např.:

```text
novak-jan
svobodova-eva
```

Jeden účet na řádek, bez `@` a bez hlavičky. Lze použít také čárky nebo středníky; duplicitní účty se sloučí. Řádky začínající `#` jsou komentáře. Jde o existující GitHub účty žáků, nikoli školní přihlašovací jména nebo e-mailové adresy.

Pro jednorázový provoz stačí **fine-grained personal access token správce**. Na GitHubu otevřete **Settings → Developer settings → Personal access tokens → Fine-grained tokens** a nastavte:

- **Resource owner:** `sposdknl` (musíte mít odpovídající oprávnění v organizaci).
- **Repository access:** All repositories, aby token viděl také nově vytvořená repa.
- **Repository permissions:** Administration: Read and write; Contents: Read-only; Metadata: Read-only.
- Nastavte krátkou expiraci podle plánovaného použití. Organizace může vyžadovat schválení tokenu.

Do `.env.batch` vyplňte `GITHUB_ORG=sposdknl` a `GITHUB_TOKEN=...`. Token je lokální tajný údaj a soubor se nekopíruje do Docker image. Pokud už máte GitHub App, lze místo tokenu použít její App ID, Installation ID a privátní klíč; nejsou potřeba client secret ani OAuth callback. Pro App variantu připojte klíč do kontejneru navíc jako read-only volume a nastavte odpovídající cestu.

### Spuštění v Dockeru

```bash
docker compose -f compose.batch.yaml build

# Pouze plán: žádné síťové požadavky ani změny na GitHubu.
docker compose -f compose.batch.yaml run --rm batch \
  --students /input/students.txt --cohort 2a-2026 --assignment git-vagrant

# Provedení stejné dávky:
docker compose -f compose.batch.yaml run --rm batch \
  --students /input/students.txt --cohort 2a-2026 --assignment git-vagrant --apply
```

Výsledné názvy budou `sposdknl/novak-jan-2a-2026-git-vagrant` a `sposdknl/svobodova-eva-2a-2026-git-vagrant`. Výchozí template je `sposdknl/git-vagrant`; změnit jej lze přepínačem `--template organizace/repozitar`. Výchozí viditelnost je soukromá; veřejnou vyberete `--visibility public`.

Kontejner nezpřístupňuje žádný port; potřebuje pouze odchozí HTTPS spojení ke GitHubu. Před první změnou ověří template a všechny uživatelské účty. Není-li některý účet nalezen, nic vzdáleně nezaloží. Ověření existence účtů neověřuje předem všechna pravidla organizace pro pozvánky; pozdější chyba se vypíše u konkrétního žáka a dávka pokračuje.

Záznamy a CSV s výsledky zůstávají v Docker volume `batch-data`. **Volume nemažte**, pokud chcete stejné zadání znovu spustit nebo pokračovat po chybě. Opakované spuštění dokončené projekty přeskočí a u vytvořených repozitářů s neúspěšným pozváním znovu přidělí přístup. Neúspěšné či nepotvrzené vytvoření vyžaduje kontrolu; existující neznámé repo nepřevezme. Pro nové zadání změňte `--assignment`, pro jinou třídu nebo ročník `--cohort`.

CSV si stáhnete bez běžícího serveru:

```bash
docker compose -f compose.batch.yaml run --rm --entrypoint cat batch \
  /app/data/vysledky-2a-2026-git-vagrant.csv > vysledky.csv
```

`invited` znamená odeslanou pozvánku; žák ji ještě musí přijmout. `ready` znamená přidělený přístup. Pozdější přijetí pozvánky ani její vypršení dávka sama nesleduje. Změny ve vstupním seznamu nemažou dříve založená repa ani neodebírají existující oprávnění. Aby žáci neměli přístup k soukromým projektům ostatních, nastavte v organizaci **Base permissions → No permission** a zkontrolujte případné týmové přístupy. Politika organizace musí dovolovat přidání žáků jako spolupracovníků.

Stejný příkaz lze spustit také bez Dockeru:

```bash
python3 -m school_repos.batch --students students.txt --cohort 2a-2026 --assignment git-vagrant
python3 -m school_repos.batch --students students.txt --cohort 2a-2026 --assignment git-vagrant --apply
```

Dávka používá oddělenou databázi od webu. Při provozu bez Dockeru ji lze vybrat přes `--database`, report přes `--report`. Jedna databáze patří jedné organizaci. V jednom okamžiku spouštějte pouze jednu dávku a zachovejte přístup k databázi i při dalším běhu.

Pro ruční zotavení podle části **Chyby a zotavení** použijte operátorský příkaz s `--batch`. V Dockeru např.:

```bash
docker compose -f compose.batch.yaml run --rm --entrypoint python batch \
  -m school_repos.manage --batch list
```

Stejně lze použít `retry-access <identifikator> <github-id>` nebo `reset-uncreated <identifikator> <github-id>`; identifikátor je např. `2a-2026-git-vagrant`. Po zotavení opakujte dávku s `--apply`.

## Volitelné webové rozhraní: rychlé vyzkoušení

Vyžaduje Python 3.10 nebo novější; nemá žádné Python závislosti třetích stran.

```bash
python3 -m school_repos.web --demo
```

Otevřete <http://localhost:8000>. Přes tlačítko přihlášení vyberte žáka nebo učitele. Ukázka obsahuje zadání **Git a Vagrant** a žáka `zak-demo`. Učitel může přidat další zadání a žáky. Pro průchod žákem u nového zadání přidejte `zak-demo` do jeho seznamu.

Ukázka **nevolá GitHub API** a používá výhradně `data/demo.sqlite3`. Přepínání rolí je dostupné pouze s přepínačem `--demo`; tento režim nevystavujte do internetu. Smazáním ukázkové databáze při vypnuté aplikaci lze ukázku obnovit. Ostrá databáze se tím nemění.

## Co aplikace umí

- Přihlášení žáků a učitelů přes GitHub App.
- Učitelská administrace: název, popis, template, viditelnost, povolení žáci a otevření/uzavření zadání.
- Samostatný odkaz `/zadani/<identifikator>` pro každé zadání.
- Název repozitáře `<github-username>-<identifikator-zadani>`; username pochází z GitHubu, žák jej nevyplňuje.
- Přehled vytvořených projektů a stavu přidělení přístupu.
- Ochrana proti duplicitám při opakovaném či souběžném kliknutí.
- Opakování neúspěšného přidělení přístupu bez vytváření dalšího repozitáře.

Žáci se povolují **pro každé zadání samostatně**. Prázdný seznam nepovoluje nikoho. Při prvním pokusu o založení se účet v seznamu spojí s číselným GitHub ID. Administrátoři se vždy určují číselným GitHub ID, nikoli měnitelným username.

## Nastavení GitHub App

Následující kroky provádí správce organizace. Privátní klíč ani client secret neposílejte žákům a neukládejte je do repozitáře.

1. Zvolte adresu serveru, např. `https://projekty.skola.cz`. Pro lokální vývoj lze použít `http://localhost:8000`.
2. V organizaci `sposdknl` otevřete **Settings → Developer settings → GitHub Apps → New GitHub App**.
3. Nastavte:
   - **GitHub App name:** vlastní jedinečný název, např. `SPOS OSY Projekty`.
   - **Homepage URL:** `https://projekty.skola.cz`.
   - **Callback URL:** `https://projekty.skola.cz/auth/callback`.
   - **Request user authorization (OAuth) during installation:** není potřeba zapínat.
   - **Webhook → Active:** vypnout; aplikace webhooky nepoužívá.
   - **Repository permissions → Administration:** Read and write.
   - **Repository permissions → Contents:** Read-only.
   - **Metadata:** Read-only, GitHub přidává automaticky.
   - Oprávnění organizace a účtu nejsou potřeba.
   - Instalaci povolte pouze na vlastním účtu/organizaci, pokud je tato možnost dostupná.
4. Po vytvoření zaznamenejte **App ID** a **Client ID**. Vygenerujte **Client secret** a **Private key**.
5. Přes **Install App** nainstalujte aplikaci do `sposdknl`. Zvolte **All repositories**, aby aplikace viděla template i nově založené projekty. Toto oprávnění zároveň dovoluje administraci ostatních repozitářů organizace; pro oddělení výuky lze použít samostatnou školní organizaci.
6. Z adresy stránky instalace zjistěte **Installation ID** (číslo za `/installations/`). Ověřte, že instalace patří do `sposdknl`.

Login používá uživatelský token pouze k načtení `/user`. Token se neukládá. Repozitáře vytváří samostatný krátkodobý instalační token GitHub App.

Oficiální dokumentace: [registrace GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app), [autentizace instalace](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation), [vytvoření repozitáře z template](https://docs.github.com/en/rest/repos/repos#create-a-repository-using-a-template), [přidání spolupracovníka](https://docs.github.com/en/rest/collaborators/collaborators#add-a-repository-collaborator).

## Konfigurace a ostré spuštění

Na serveru musí být Python 3.10+ a příkaz `openssl` pro podpis JWT. Privátní klíč musí být RSA klíč stažený z GitHub App.

```bash
cp .env.example .env
```

Doplňte `.env`:

```dotenv
APP_URL=https://projekty.skola.cz
PORT=8000
DATABASE=data/school.sqlite3
GITHUB_ORG=sposdknl
ADMIN_GITHUB_IDS=123456,789012
GITHUB_APP_ID=...
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
GITHUB_INSTALLATION_ID=...
GITHUB_PRIVATE_KEY_FILE=/bezpecna/cesta/github-app.pem
```

ID učitele je pole `id` z veřejného API `https://api.github.com/users/JEHO_USERNAME`; při použití GitHub CLI také `gh api users/JEHO_USERNAME --jq .id`. Je to číselné ID účtu, nikoli App ID nebo username.

```bash
python3 -m school_repos.web
```

V ostrém režimu není přepínač rolí ani ukázkové zadání. Aplikace skončí s vysvětlením, pokud chybí potřebná konfigurace. Proměnné prostředí mají přednost před `.env`. Soubor `.env` se čte z kořene projektu; relativní cesty k databázi a klíči se vyhodnocují vůči pracovnímu adresáři procesu.

Server standardně poslouchá pouze na `127.0.0.1:8000`. Na internet jej vystavte přes reverzní proxy s HTTPS, např. Caddy podle [Caddyfile.example](Caddyfile.example). Doména v `APP_URL` a callback URL musí přesně souhlasit s adresou v prohlížeči. Proxy přesměrujte na lokální port, neservírujte pracovní složku jako statické soubory. HTTP server je určen pro malou školní aplikaci za proxy; nemá být přímo veřejně vystavený.

### Docker Compose

V kořeni projektu připravte `.env` a privátní klíč `github-app.pem`. Compose nastaví cestu ke klíči a databázi uvnitř kontejneru; ponechte `PORT=8000`. Klíč musí být čitelný pro UID `10001`, pod kterým aplikace běží. Použijte oprávnění souboru či ACL tak, aby byl klíč dostupný pouze správci a tomuto uživateli.

```bash
docker compose up -d --build
```

Port 8000 je dostupný pouze lokálně pro reverzní proxy. Data jsou v pojmenovaném volume `school-data`. Klíč se připojuje jen ke čtení a nekopíruje se do image. Image nemá Python závislosti k instalaci; při sestavení stáhne systémový OpenSSL a certifikáty.

### Doporučená oprávnění organizace

- Žákům nemusíte povolit vytváření repozitářů.
- **Base permissions → No permission**, pokud nemají mít přístup k projektům spolužáků.
- Povolte externí spolupracovníky, pokud žáci nejsou členy organizace. Omezující pravidla organizace nebo enterprise mohou přidělení přístupu zablokovat.
- U soukromých projektů ověřte kapacitu/tarif organizace a pravidla pro spolupracovníky.
- Učitele, kteří mají kontrolovat kód přímo na GitHubu, přidejte jako vlastníky nebo jim přidělte přístup přes učitelský tým. Role učitele v této aplikaci sama o sobě nezakládá jeho oprávnění na GitHubu.

## Postup učitele a žáka

1. Učitel se přihlásí GitHub účtem uvedeným v `ADMIN_GITHUB_IDS`.
2. Ve **Správě zadání** vytvoří zadání, např. identifikátor `osy-2026-git-vagrant`, template `sposdknl/git-vagrant` a seznam GitHub usernames žáků bez `@`.
3. Žákům předá odkaz `https://projekty.skola.cz/zadani/osy-2026-git-vagrant`.
4. Žák se přihlásí, zadání najde mezi svými úkoly a klikne **Vytvořit můj projekt**.
5. Přijme případnou pozvánku na GitHubu a naklonuje si svůj repozitář.

Po přihlášení se zobrazuje přehled všech povolených zadání; odkaz na konkrétní zadání lze znovu otevřít. Popis zadání je prostý text, nikoli HTML nebo Markdown.

Identifikátor se po vytvoření nemění. Pro jiný ročník nebo nové kolo práce vytvořte nové zadání. Template a viditelnost se po prvním pokusu o založení projektu uzamknou. Změny template se do existujících žákovských projektů automaticky nesynchronizují.

Ve **Správě zadání** lze přes **Smazat zadání** otevřít potvrzení trvalého smazání. Odstraní se zadání, seznam žáků a místní záznamy projektů; repozitáře, pozvánky a oprávnění na GitHubu zůstanou zachované. Pro další pokus použijte nový identifikátor, pokud na GitHubu zůstala repa se stejnými názvy. Smazání je blokované při stavu Zakládání; přerušenou operaci nejprve vyřešte podle části **Chyby a zotavení**.

Uzavření zadání zabrání dalšímu zakládání a opakování přidělení přístupu. **Odebrání žáka ze seznamu ani uzavření zadání neodebere jeho existující oprávnění na GitHubu**; to spravujte přímo na GitHubu. Pozvánky jsou v aplikaci označeny jako odeslané; jejich pozdější přijetí aplikace automaticky nesleduje.

## Chyby a zotavení

Aplikace nesmaže ani automaticky nepřevezme existující repozitář stejného jména. Vytváření a přidělení přístupu jsou dva kroky: potvrzené GitHub ID se uloží ještě před pozváním žáka. Pokud selže pouze pozvánka, žák může na stránce zadání použít **Zopakovat přidělení přístupu**.

Pokud GitHub vrátí chybu během vytváření nebo proces skončí před uložením odpovědi, aplikace nemůže bezpečně poznat, zda repozitář vznikl. Ponechá záznam ke kontrole místo dalšího zakládání nebo přidělení přístupu k neznámému repozitáři.

Operátorské příkazy spouštějte na serveru se stejnou konfigurací a databází. Před obnovou **zastavte obsluhu webových požadavků**, aby současně neběžela rozpracovaná operace. Nic z následujícího nemaže repozitáře na GitHubu.

```bash
# Přehled záznamů, včetně assignment, user_id, repo_id a error:
python3 -m school_repos.manage list

# Přerušené přidělení přístupu u projektu s potvrzeným repo_id:
python3 -m school_repos.manage retry-access osy-2026-git-vagrant 123456

# Zrušení rezervace po nepotvrzeném vytvoření:
python3 -m school_repos.manage reset-uncreated osy-2026-git-vagrant 123456
```

`retry-access` zpřístupní žákovi opakování pozvánky. `reset-uncreated` odmítne projekt s potvrzeným ID; jinak ověří přes GitHub API, že cílový název vrací 404, a teprve potom odstraní rezervaci v databázi. Instalace musí mít přístup ke **všem repozitářům**, protože při omezeném přístupu může 404 znamenat i skrytý repozitář. Pokud repozitář existuje, nejprve jej ručně prověřte na GitHubu a rozhodněte, zda ho zachovat pod jiným názvem. Automatické přivlastnění neznámého repozitáře není podporováno.

Při 403 ověřte oprávnění GitHub App a pravidla organizace; při 404 instalaci a přístup k template; při 422 dostupnost názvu a platnost template. Po vyřešení chyby je pro neúspěšné vytváření nutný uvedený reset. Soukromé template musí být přístupné instalaci; nejjednodušší je držet je přímo v `sposdknl`.

Příkazy mají také variantu `--demo`, např. `python3 -m school_repos.manage --demo list`.

## Provoz a ověření

- Zálohujte celou SQLite databázi konzistentním SQLite backupem nebo při zastavené aplikaci. Při běhu databáze ve WAL režimu nekopírujte samotný `.sqlite3` bez zajištění konzistence.
- Udržujte jednu instanci aplikace a databázi na místním disku. SQLite rezervace chrání souběžné požadavky; distribuované nasazení tato verze nepokrývá.
- Změny `GITHUB_ORG` nebo přepojení instalace dělejte s novou databází; existující projekty jsou svázány s původní organizací.
- Přihlášení vyprší po osmi hodinách. OAuth pokus platí deset minut. Sessions jsou na serveru, cookies mají HttpOnly a SameSite=Lax, na HTTPS také Secure. Zápisové formuláře používají CSRF token.
- `GET /health` vrací základní stav procesu, nikoli ověření GitHub konfigurace.
- Server nezapisuje HTTP URL do logů, aby nelogoval OAuth kódy. Uživatelské ani instalační tokeny neukládá do databáze.

```bash
python3 -m unittest discover -s tests -v
```

Testy pokrývají skutečný HTTP handler bez síťového socketu, autorizaci, OAuth state a rotaci sessions, CSRF, izolaci ukázky, vytvoření projektu, souběh, kolize, opakování pozvánek, kontrolu GitHub ID a skutečný RSA podpis JWT. GitHub API je v testech nahrazené; ověření proti skutečné organizaci vyžaduje vlastní GitHub App. Před výukou proveďte průchod jedním testovacím žákovským účtem a ověřte obsah template, pozvánku a možnost push.
