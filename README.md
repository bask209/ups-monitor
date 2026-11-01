# ups-monitor

Rootless Podman (Quadlet) stack that monitors a USB UPS (tested: APC Back-UPS BGM1500) with
[NUT](https://networkupstools.org/), keeps up to 365 days of history in SQLite, serves a live LAN dashboard
(TradingView Lightweight Charts, Server-Sent Events) and sends Telegram alerts to a single owner.

- `ups-nut` - NUT `usbhid-ups` driver + `upsd` (listens on the pod's localhost only)
- `ups-dashboard` - poller, SQLite storage, dashboard on port **8330**, Telegram bot (`app/app.py`, stdlib only)
- Both run from one image, `localhost/ups-monitor:latest`, in the pod `ups`

## Install

```sh
# 1. USB permissions for the rootless container (edit OWNER/GROUP to your user)
sudo install -m 644 99-ups-apc.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=051d

# 2. Config
cp .env.example .env && chmod 600 .env   # fill TELEGRAM_TOKEN + TELEGRAM_CHAT_ID (optional)
mkdir -p data

# 3. Image + quadlets (the quadlets expect this repo at ~/ups-monitor)
podman build -t localhost/ups-monitor:latest .
mkdir -p ~/.config/containers/systemd && cp quadlet/* ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start ups-pod ups-nut ups-dashboard
loginctl enable-linger "$USER"           # start at boot

# 4. LAN firewall (firewalld): allow 8330/tcp from your subnet
```

Dashboard: `http://<host>:8330` - metrics: `/metrics` (Prometheus format).

## Telegram

Create a bot with @BotFather, put the token in `.env`, message the bot, and set `TELEGRAM_CHAT_ID` to your numeric
user ID. The bot only answers that private chat (chat ID and sender ID must both match) and sends alerts only on events
(power failure/restore, battery thresholds, overload, replace battery, high load, mains voltage near limits, self-test
results, link loss). Commands: `/status /day /week /events /outages /dashboard`. `DIGEST_ENABLED=true` adds a daily summary.

## Staying alive (what watches the watcher)

- `quadlet/ups.pod` has `Restart=always`. The pod is created with `--exit-policy stop`, so when both containers die
  (e.g. a failed health check) it exits cleanly; the containers are `BindsTo=` the pod, so with `Restart=on-failure`
  systemd stopped them and nothing ever restarted the stack (it was silently down for 8 h once).
- `nut/entrypoint.sh` runs the driver and upsd in the foreground: driver logs reach `journalctl --user -u ups-nut`,
  and if either process dies the container exits and is restarted.
- The app persists "UPS link lost" in the DB, so a restart while the link is down still ends with a
  "link restored after ..." message; it repeats "link STILL down" every `LINK_REMIND_SECONDS` (30 min), reports a
  "Monitor was offline" gap on startup (`OFFLINE_ALERT_SECONDS`, 120), and restarts any worker thread that crashes.
- `watchdog/` is a systemd user timer that runs outside the pod every minute. If `/api/health` does not answer for
  3 minutes it sends a Telegram alert, starts/restarts `ups-pod.service`, and sends a message when it is back.
  Install: `cp watchdog/ups-watchdog.{service,timer} ~/.config/systemd/user/ && systemctl --user daemon-reload &&
  systemctl --user enable --now ups-watchdog.timer` (needs `TELEGRAM_*` in `.env`).

## Retention

`RAW_DAYS` (default 30) at 10 s resolution, then 1-minute averages up to `RETENTION_DAYS` (default 365), roughly 50 MB.

## Notes

- Restart the containers one at a time (`systemctl --user restart ups-dashboard`), not both in one command.
- The NUT container runs without SELinux label confinement because USB device nodes carry `usb_device_t`.
- Read-only: the app never sends commands to the UPS.
