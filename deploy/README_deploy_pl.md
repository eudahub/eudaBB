# Deploy eudaBB na serwer Hetzner — instrukcja krok po kroku

Procedura odtworzona z sesji 2026-05-15. Serwer: `ubuntu-4gb-nbg1-9` (IP 178.104.140.186), Ubuntu, Postgres 16, nginx, gunicorn.

Domeny: `eudahub.pl`, `sfinia.pl` (oba A-rekord na ten sam IP). Dwie bazy danych obsługiwane przez env `FORUM=eudahub|sfinia`.

## Założenia wstępne

Na serwerze już zainstalowane (z poprzedniej iteracji albo z provisioningu):
- nginx
- Postgres 16
- Let's Encrypt cert dla `eudahub.pl` (`/etc/letsencrypt/live/eudahub.pl/`)
- baza `eudahub` z userem `eudahub_user` (z pustym/utraconym hasłem)

Jeśli to świeży serwer — zainstaluj te rzeczy najpierw:
```bash
apt update && apt install -y nginx postgresql certbot python3-certbot-nginx python3-venv python3-pip git
certbot --nginx -d eudahub.pl -d www.eudahub.pl
sudo -u postgres psql -c "CREATE USER eudahub_user;"
sudo -u postgres psql -c "CREATE DATABASE eudahub OWNER eudahub_user;"
```

## 1. SSH alias z keepalive

Bez keepalive sesja SSH ginie na bezczynności. **Lokalnie:**

```bash
cat >> ~/.ssh/config <<'EOF'
Host hetzner eudahub
    HostName 178.104.140.186
    User root
    ServerAliveInterval 30
    ServerAliveCountMax 6
    TCPKeepAlive yes
EOF
chmod 600 ~/.ssh/config
```

Test: `ssh hetzner`.

## 2. Aktualizacja systemu + reboot

**Na serwerze:**
```bash
apt update && apt upgrade -y
reboot
# poczekaj ~30s, połącz ponownie: ssh hetzner
systemctl is-active nginx postgresql@16-main
```

## 3. Reset hasła Postgres

**Na serwerze:**
```bash
NEW_PG_PASS=$(openssl rand -base64 24 | tr -d '/=+' | head -c 24)
echo "Nowe hasło: $NEW_PG_PASS"
sudo -u postgres psql -c "ALTER USER eudahub_user PASSWORD '$NEW_PG_PASS';"
echo "$NEW_PG_PASS" > /root/eudahub_pg_pass.txt
chmod 600 /root/eudahub_pg_pass.txt
```

Hasło zapamiętane w `/root/eudahub_pg_pass.txt`.

## 4. Druga baza dla sfinia

```bash
sudo -u postgres psql -c "CREATE DATABASE sfinia OWNER eudahub_user;"
```

Jeśli baza `eudahub` była utworzona wcześniej z innym ownerem niż `eudahub_user`, dodatkowo:
```bash
sudo -u postgres psql -c "ALTER DATABASE eudahub OWNER TO eudahub_user;"
sudo -u postgres psql -d eudahub -c "ALTER SCHEMA public OWNER TO eudahub_user;"
sudo -u postgres psql -d eudahub -c "GRANT ALL ON SCHEMA public TO eudahub_user;"
```

Bez tego `migrate` walnie się błędem `permission denied for schema public` (PG 15+ nie daje CREATE non-ownerom).

## 5. Backup starego szkieletu + clone repo

```bash
mv /srv/eudahub /srv/eudahub.old.$(date +%Y%m%d) 2>/dev/null
cd /srv
git clone https://github.com/eudahub/eudaBB.git eudahub
cd eudahub
git log --oneline -3
```

## 6. Venv + requirements + gunicorn

```bash
cd /srv/eudahub
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn   # nie jest w requirements
```

## 7. Plik .env

**Lokalnie** (laptop) skopiuj swój `.env` na serwer:
```bash
scp /home/andrzej/wazne/gitmy/eudaBB/.env hetzner:/srv/eudahub/.env
```

**Na serwerze** wygeneruj brakujące sekrety i otwórz w nano:
```bash
cd /srv/eudahub
echo "SECRET_KEY:    $(python -c 'import secrets; print(secrets.token_urlsafe(50))')"
echo "DB_PASSWORD:   $(cat /root/eudahub_pg_pass.txt)"
echo "ROOT_PASSWORD: $(openssl rand -base64 18 | tr -d '/=+')"
nano /srv/eudahub/.env
```

Ustaw w nano:
- `DEBUG=False`
- `SECRET_KEY=<z echo SECRET_KEY>`
- `ALLOWED_HOSTS=eudahub.pl,www.eudahub.pl,sfinia.pl,www.sfinia.pl`
- `DB_USER=eudahub_user`
- `DB_PASSWORD=<z echo DB_PASSWORD>`
- `DB_HOST=localhost`
- `DB_NAME_EUDAHUB=eudahub`
- `DB_NAME_SFINIA=sfinia`
- `DATABASE_URL=postgres://eudahub_user:<DB_PASSWORD>@localhost:5432/eudahub`
- `TEST_MODE=False`
- `ROOT_PASSWORD=<z echo ROOT_PASSWORD>` (zapisz w bezpiecznym miejscu — to konto admina forum)
- `FORUM=sfinia` (lub `eudahub` w zależności co ma być domyślne)

EMAIL_* (SendGrid) — zostaw wartości z lokalnego `.env`.

## 8. Migracje obu baz + statyki

```bash
cd /srv/eudahub && source venv/bin/activate
python manage.py migrate                          # default = sfinia
FORUM=eudahub python manage.py migrate            # druga baza
python manage.py collectstatic --no-input
```

## 9. Konta root dla obu forów

```bash
python manage.py create_root
FORUM=eudahub python manage.py create_root
```

Loguj się potem na `root` + ROOT_PASSWORD z .env (na `/admin/` lub stronie logowania).

## 10. Gunicorn jako systemd

```bash
cat > /etc/systemd/system/eudabb.service <<'EOF'
[Unit]
Description=eudaBB gunicorn (Django)
After=network.target postgresql.service

[Service]
User=root
WorkingDirectory=/srv/eudahub
EnvironmentFile=/srv/eudahub/.env
ExecStart=/srv/eudahub/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:8000 config.wsgi:application
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now eudabb
systemctl status eudabb --no-pager | head -15
curl -sI http://127.0.0.1:8000 | head -3
```

**UWAGA:** nie dodawaj `--access-logfile -` / `--error-logfile -` do ExecStart — systemd parsuje `-` jako kolejny argument i gunicorn rzuca `No application module specified`.

## 11. Nginx vhost

Wyłącz domyślny vhost:
```bash
rm -f /etc/nginx/sites-enabled/default
```

Utwórz konfig:
```bash
cat > /etc/nginx/sites-available/eudahub.pl <<'EOF'
server {
    listen 80;
    server_name eudahub.pl www.eudahub.pl sfinia.pl www.sfinia.pl;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name eudahub.pl www.eudahub.pl;

    ssl_certificate     /etc/letsencrypt/live/eudahub.pl/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/eudahub.pl/privkey.pem;

    client_max_body_size 25M;

    location /static/ { alias /srv/eudahub/staticfiles/; expires 7d; }
    location /media/  { alias /srv/eudahub/media/;       expires 7d; }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
EOF

ln -s /etc/nginx/sites-available/eudahub.pl /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

**Uwaga starsze nginx:** `http2 on;` (dyrektywa) działa dopiero od 1.25; na 1.24- trzeba `listen 443 ssl http2;` (jak wyżej).

Po reload otwórz **https://eudahub.pl** — powinno pokazać forum (puste).

**Uwaga Cloudflare:** sfinia.pl/eudahub.pl idą przez Cloudflare jako reverse proxy. CF ma własny cert dla klienta, a do origin'a może iść HTTP (Flexible), HTTPS bez weryfikacji (Full) lub HTTPS z weryfikacją (Full strict). Po wydaniu cert na origin'cie ustaw w panelu CF **SSL/TLS → Full (strict)** dla obu domen.

## 12. Cert SSL dla sfinia.pl

DNS dla `www.sfinia.pl` może nie istnieć (tylko `sfinia.pl` ma A-rekord) — wtedy żądaj cert tylko dla samego `sfinia.pl`:

```bash
certbot --nginx -d sfinia.pl
```

Certbot wyda cert ale **nie zainstaluje go** automatycznie jeśli żaden istniejący vhost nie ma `server_name sfinia.pl`. Komunikat: "Could not automatically find a matching server block". Trzeba ręcznie dodać blok HTTPS:

```bash
cat >> /etc/nginx/sites-available/eudahub.pl <<'EOF'

server {
    listen 443 ssl http2;
    server_name sfinia.pl;

    ssl_certificate     /etc/letsencrypt/live/sfinia.pl/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sfinia.pl/privkey.pem;

    client_max_body_size 25M;

    location /static/ { alias /srv/eudahub/staticfiles/; expires 7d; }
    location /media/  { alias /srv/eudahub/media/;       expires 7d; }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
EOF

nginx -t && systemctl reload nginx
```

Jeśli kiedyś dodasz A-rekord www.sfinia.pl w DNS, rozszerz cert:
```bash
certbot --nginx --expand -d sfinia.pl -d www.sfinia.pl
```

## 13. Załadowanie morfologii z lokalnej bazy

Tabele `forum_morph_form` i `forum_morph_suffix` są duże (~5M + 1M wierszy) i nie kasują się przez `flush_except_morph`. Robi się to raz po deploy.

**Lokalnie:**
```bash
pg_dump -U andrzej -d forum_db --data-only \
  --table=forum_morph_form --table=forum_morph_suffix \
  | gzip > /tmp/morph.sql.gz
scp /tmp/morph.sql.gz hetzner:/tmp/
```

**Pułapka PG17 → PG16:** lokalne pg_dump 17 dorzuca:
- linię `\restrict <token>` (nowa meta-komenda psql),
- `SET transaction_timeout = 0;` (nieznany parametr w PG16).

Format custom (`-Fc`) w ogóle nie zadziała (`unsupported version 1.16 in file header`). Plain SQL trzeba przefiltrować przed `psql`:

**Na serwerze:**
```bash
PG=$(cat /root/eudahub_pg_pass.txt)
gunzip -c /tmp/morph.sql.gz | sed '/^\\restrict/d; /^\\unrestrict/d; /transaction_timeout/d' \
  | PGPASSWORD=$PG psql -U eudahub_user -h localhost -d sfinia
gunzip -c /tmp/morph.sql.gz | sed '/^\\restrict/d; /^\\unrestrict/d; /transaction_timeout/d' \
  | PGPASSWORD=$PG psql -U eudahub_user -h localhost -d eudahub

# weryfikacja
for DB in sfinia eudahub; do
  echo "=== $DB ==="
  sudo -u postgres psql -d $DB -c "SELECT (SELECT count(*) FROM forum_morph_form) AS form, (SELECT count(*) FROM forum_morph_suffix) AS suffix;"
done
```

Spodziewane ~5129950 / 996671. Czas ładowania COPY: ~3 minuty na bazę.

**Monitoring postępu** w drugim terminalu SSH (`watch` widzi 0 do końca transakcji — COPY jest jedną transakcją; lepszy:
```bash
sudo -u postgres psql -d sfinia -c "SELECT pid, bytes_processed, tuples_processed FROM pg_stat_progress_copy;"
```

Jeśli zawiesi się "blocked" — sprawdź `pg_stat_activity` i `pg_terminate_backend(<pid>)` na sesji która drzemała w niedokończonej COPY.

## 14. Import danych z SQLite (opcjonalne, dla pełnego forum z archiwum)

Archiwa SQLite (`sfinia_full.db`, `eudaHub.db`) leżą lokalnie w `/home/andrzej/wazne/gitmy/phpbb-archiver/`. Schema już dostosowany do konwencji `boards` zamiast `forums` (zob. commit `7e91a10`).

```bash
# lokalnie — wyślij archiwa
scp /home/andrzej/wazne/gitmy/phpbb-archiver/sfinia_full.db.7z hetzner:/srv/eudahub/
scp /home/andrzej/wazne/gitmy/phpbb-archiver/eudaHub.db        hetzner:/srv/eudahub/

# na serwerze — rozpakuj sfinia
apt install -y p7zip-full
cd /srv/eudahub
7z x sfinia_full.db.7z
mkdir -p ../phpbb-archiver
mv sfinia_full.db eudaHub.db ../phpbb-archiver/

# uruchom reimport (na sfinia jest sprawdzona ścieżka 40k):
./reimport_40k.sh
# albo pełny: ./reimport_sfinia.sh
# albo eudahub: ./reimport_eudahub.sh
```

## Routine — kolejny deploy zmian

```bash
ssh hetzner
cd /srv/eudahub
git pull
source venv/bin/activate
pip install -r requirements.txt    # jeśli zmiana
python manage.py migrate
FORUM=eudahub python manage.py migrate
python manage.py collectstatic --no-input
systemctl restart eudabb
```

## Co tu nie zostało zrobione (TODO)

- gunicorn pod non-root userem (np. `www-data`) — wymaga chown/chmod `/srv/eudahub`
- fail2ban + ufw firewall
- automatyczny renewal certów certbota (zwykle już jest jako cron z paczki)
- log rotation gunicorna (na razie idzie do journalctl)
- backup PG (pg_dump w cronie na zewnętrznym storage)
