#!/usr/bin/env bash
# Install or update Amazon List Watch on Debian 13 (trixie). Run as root; re-run to update.
#
#   curl -fsSL https://raw.githubusercontent.com/emilgil/list-watch/main/install.sh | bash -s -- --wishlist <url>
set -euo pipefail

REPO="https://github.com/emilgil/list-watch.git"
BRANCH="main"
PORT="8080"
WISHLIST=""
DEPLOY_KEY=""
NO_START=0

APP_DIR=/opt/listwatch
VENV="$APP_DIR/.venv"
DATA_DIR=/var/lib/listwatch
CONF_DIR=/etc/listwatch
ENV_FILE="$CONF_DIR/listwatch.env"
LOG_FILE=/var/log/listwatch-install.log
SERVICE_USER=listwatch

usage() {
    cat <<EOF
Användning: install.sh [flaggor]
  --repo <url>          repo att klona (standard: $REPO)
  --branch <gren>       gren eller tagg (standard: $BRANCH)
  --port <port>         port för webbgränssnittet (standard: $PORT; gäller bara när
                        $ENV_FILE skapas)
  --wishlist <url>      kör steg 0-proben mot denna lista efter installationen
  --deploy-key <fil>    SSH-nyckel, bara om repot görs privat
  --no-start            installera men starta inte tjänsten
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --wishlist) WISHLIST="$2"; shift 2 ;;
        --deploy-key) DEPLOY_KEY="$2"; shift 2 ;;
        --no-start) NO_START=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Okänd flagga: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Fel: kör som root" >&2
    exit 1
fi

exec > >(tee -a "$LOG_FILE") 2>&1
trap 'echo "FEL: rad $LINENO: \"$BASH_COMMAND\" avslutades med kod $?. Se $LOG_FILE" >&2' ERR

step() { echo; echo "==> $*"; }
die() { echo "Fel: $*" >&2; exit 1; }

echo "listwatch install $(date -Is)"

step "Förkontroll"
. /etc/os-release
[[ "${ID:-}" == "debian" && "${VERSION_ID:-}" == "13" ]] || die "kräver Debian 13 (trixie), hittade ${PRETTY_NAME:-okänt}"
ARCH="$(dpkg --print-architecture)"
[[ "$ARCH" == "amd64" || "$ARCH" == "arm64" ]] || die "kräver amd64 eller arm64, hittade $ARCH"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port måste vara ett tal"
if [[ -n "$DEPLOY_KEY" && ! -r "$DEPLOY_KEY" ]]; then die "kan inte läsa --deploy-key $DEPLOY_KEY"; fi
command -v curl >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates; }
for url in https://github.com https://www.amazon.se; do
    # any HTTP answer counts (amazon.se may answer curl with 503); 000 = unreachable
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url" || true)"
    [[ "$code" != "000" ]] || die "når inte $url"
    echo "$url nås (HTTP $code)"
done

step "Paket"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv sqlite3 curl ca-certificates tzdata
timedatectl set-timezone Europe/Stockholm 2>/dev/null || ln -sf /usr/share/zoneinfo/Europe/Stockholm /etc/localtime
echo "Tidszon: Europe/Stockholm"

step "Systemanvändare $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "Skapad"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$DATA_DIR"

step "Kod ($REPO, $BRANCH)"
if [[ -n "$DEPLOY_KEY" ]]; then
    export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi
if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" remote set-url origin "$REPO"
    git -C "$APP_DIR" fetch --quiet --tags origin "$BRANCH"
    git -C "$APP_DIR" reset --quiet --hard FETCH_HEAD
else
    git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
echo "Version: $(git -C "$APP_DIR" log -1 --format='%h %s')"

step "Python-miljö"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.lock"
"$VENV/bin/pip" install --quiet --disable-pip-version-check --no-deps --force-reinstall "$APP_DIR"

step "Konfiguration"
install -d -m 755 "$CONF_DIR"
if [[ -f "$ENV_FILE" ]]; then
    echo "$ENV_FILE finns, lämnas orörd"
else
    sed "s/^LISTWATCH_PORT=.*/LISTWATCH_PORT=$PORT/" "$APP_DIR/deploy/listwatch.env.example" > "$ENV_FILE"
    echo "Skapad: $ENV_FILE"
fi
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
PORT="${LISTWATCH_PORT:-$PORT}"

as_service() {
    runuser -u "$SERVICE_USER" -- env \
        LISTWATCH_DATA_DIR="${LISTWATCH_DATA_DIR:-$DATA_DIR}" \
        LISTWATCH_PORT="$PORT" \
        LISTWATCH_LOG_LEVEL="${LISTWATCH_LOG_LEVEL:-INFO}" \
        "$VENV/bin/listwatch" "$@"
}

step "Databas"
WAS_ACTIVE=0
if systemctl is-active --quiet listwatch; then
    WAS_ACTIVE=1
    systemctl stop listwatch
    echo "Tjänsten stoppad under migreringen"
fi
as_service migrate

step "systemd"
install -m 644 "$APP_DIR/deploy/listwatch.service" "$APP_DIR/deploy/listwatch-backup.service" \
    "$APP_DIR/deploy/listwatch-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now --quiet listwatch-backup.timer
systemctl enable --quiet listwatch
if [[ $NO_START -eq 1 ]]; then
    echo "Tjänsten startas inte (--no-start)"
else
    systemctl restart listwatch
fi
[[ $WAS_ACTIVE -eq 0 || $NO_START -eq 0 ]] || echo "Obs: tjänsten var igång före uppdateringen och är nu stoppad"

if [[ $NO_START -eq 0 ]]; then
    step "Hälsokontroll"
    healthy=0
    for _ in $(seq 30); do
        if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then healthy=1; break; fi
        sleep 1
    done
    if [[ $healthy -eq 0 ]]; then
        echo "Tjänsten svarar inte på http://127.0.0.1:$PORT/health efter 30 s. Senaste loggraderna:" >&2
        journalctl -u listwatch -n 30 --no-pager >&2 || true
        exit 1
    fi
    curl -fsS "http://127.0.0.1:$PORT/health"; echo
fi

if [[ -n "$WISHLIST" ]]; then
    step "Steg 0-prob"
    probe_code=0
    as_service probe "$WISHLIST" || probe_code=$?
    case $probe_code in
        0) echo "Proben: alla produkter läsbara" ;;
        1) echo "Proben: några produkter saknar pris (avslutskod 1), se rapporten ovan" ;;
        *) echo "Proben: blockerad eller fel (avslutskod $probe_code), se rapporten ovan" ;;
    esac
fi

step "Klart"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF
Webbgränssnitt: http://${IP:-<LXC-IP>}:$PORT
Kod:            $APP_DIR
Data:           ${LISTWATCH_DATA_DIR:-$DATA_DIR}
Konfiguration:  $ENV_FILE
Installationslogg: $LOG_FILE
Loggar:         journalctl -u listwatch -f
Lägg till lista: runuser -u $SERVICE_USER -- env LISTWATCH_DATA_DIR=${LISTWATCH_DATA_DIR:-$DATA_DIR} $VENV/bin/listwatch add-list <url>
Uppdatera:      kör skriptet igen
EOF
