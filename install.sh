#!/usr/bin/env bash
# Install the systemd user units (bot timer + dashboard) and the desktop launcher
# for this checkout. Re-run after moving the directory. Nothing is enabled yet:
# the bot timer only starts when you run `systemctl --user enable --now mrr-bot.timer`.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNITS="$HOME/.config/systemd/user"
APPS="$HOME/.local/share/applications"

if [ ! -f "$DIR/config.json" ]; then
  cp "$DIR/config.example.json" "$DIR/config.json"
  chmod 600 "$DIR/config.json"
  echo "created $DIR/config.json from the example: fill in api_key, api_secret and profile_id"
fi

mkdir -p "$UNITS" "$APPS"
# The units assume the checkout lives at ~/mrr-bot; rewrite the path if it is elsewhere.
for u in mrr-bot.service mrr-bot.timer mrr-dashboard.service; do
  sed "s|%h/mrr-bot|$DIR|g" "$DIR/systemd/$u" > "$UNITS/$u"
done
sed "s|__DIR__|$DIR|g" "$DIR/launcher/mrr-command-center.desktop" > "$APPS/mrr-command-center.desktop"
chmod +x "$DIR/launcher/mrr-command-center.sh"
systemctl --user daemon-reload

cat <<MSG
installed:
  $UNITS/mrr-bot.service, mrr-bot.timer, mrr-dashboard.service
  $APPS/mrr-command-center.desktop

next:
  python3 -I $DIR/mrr_bot.py status            # check API keys and balance
  python3 -I $DIR/mrr_bot.py pools             # check the pool profile points at your gateway
  python3 -I $DIR/mrr_bot.py plan              # dry run
  systemctl --user enable --now mrr-dashboard  # dashboard on http://127.0.0.1:7160/
  systemctl --user enable --now mrr-bot.timer  # start spending (one pass every 15 min)
MSG
