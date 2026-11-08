#!/usr/bin/env bash
# Serve a sandbox at test.fcvino.no from this Mac, for as long as this runs.
#
#     scripts/test_site.sh            # copy the newest backup, serve it on :8001
#     scripts/test_site.sh --fresh    # take a backup off the server first
#     scripts/test_site.sh --keep     # carry on with last time's sandbox
#
# The club's site runs on the Linux box; this is the other half — the branch
# you are working on, against a copy of the club's data, behind the same
# Cloudflare Access as the real thing. Ctrl-C takes the tunnel down with it,
# so the test site only exists while you are actually working on it.
#
# Its own tunnel, `fcvino-test`, never `fcvino-app`. Two machines running one
# named tunnel are replicas as far as Cloudflare is concerned, and it would
# spread app.fcvino.no's traffic across both — some of the club landing on a
# sandbox on a laptop. The README has the one-time setup.
#
# Anything this does not recognise goes on to serve.py (`--keep`, `--port`).

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${FCVINO_PYTHON:-python}"
TUNNEL_CONFIG="${FCVINO_TEST_TUNNEL_CONFIG:-$HOME/.cloudflared/test.yml}"
TUNNEL_LOG="$REPO/data/test-tunnel.log"

fresh=0
passthrough=()
for arg in "$@"; do
    case "$arg" in
        --fresh) fresh=1 ;;
        *) passthrough+=("$arg") ;;
    esac
done

if ! command -v cloudflared >/dev/null; then
    echo "cloudflared is not installed — brew install cloudflared" >&2
    exit 2
fi
if [[ ! -f "$TUNNEL_CONFIG" ]]; then
    echo "no tunnel config at $TUNNEL_CONFIG — see 'A test site' in the README" >&2
    exit 2
fi
# Refuse the production tunnel outright rather than trust that nobody ever
# copies config.yml over test.yml. By name or by the hostname it routes, since
# a config can name its tunnel by UUID instead.
if grep -Eq '^tunnel:[[:space:]]*fcvino-app[[:space:]]*$|app\.fcvino\.no' "$TUNNEL_CONFIG"; then
    echo "$TUNNEL_CONFIG is the club's tunnel (fcvino-app / app.fcvino.no)," >&2
    echo "which runs on the Linux box. Point this at the fcvino-test config." >&2
    exit 2
fi

if (( fresh )); then
    "$PYTHON" "$REPO/scripts/backup_db.py"
fi

mkdir -p "$REPO/data"
cloudflared tunnel --config "$TUNNEL_CONFIG" run >"$TUNNEL_LOG" 2>&1 &
tunnel=$!
trap 'kill "$tunnel" 2>/dev/null; wait "$tunnel" 2>/dev/null || true; echo "test.fcvino.no is down."' EXIT
echo "tunnel up (pid $tunnel), logging to $TUNNEL_LOG"
echo "https://test.fcvino.no"

"$PYTHON" "$REPO/scripts/serve.py" --sandbox --from-backup --behind-cloudflare \
    ${passthrough[@]+"${passthrough[@]}"}
