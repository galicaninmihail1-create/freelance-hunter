# Beget deployment plan (manual-mode gate)

This plan assumes a Beget VPS/VDS with systemd and Nginx. Run `audit.sh` first and do not install or reload anything until its output and the existing Nginx/n8n layout have been reviewed.

## Isolation design

- Runtime user/group: `freelance-hunter` (no shell login required).
- Application: `/opt/freelance-hunter/current`, root-owned and read-only to the service.
- Virtual environment: `/opt/freelance-hunter/venv`.
- SQLite: `/var/lib/freelance-hunter/freelance_hunter.db`, writable only by the service user.
- Secrets: `/etc/freelance-hunter/hunter.env`, mode `0600`, root-owned.
- Process manager: one native systemd unit bound to `127.0.0.1:8765`.
- HTTPS: a dedicated subdomain and Nginx virtual host exposing only `/health` and `/telegram/webhook`.
- Logs: journald. No Docker network, container, volume, or database is shared with n8n.

Python 3.12+ is required by `pyproject.toml`.

## Read-only audit

```sh
sh deploy/audit.sh
```

Also inspect the existing Nginx configuration with `sudo nginx -T` before choosing a filename, domain, or port. Do not print Docker container environments or secrets.

## Change gate

The following steps modify the server and must be approved only after the audit:

1. Create the dedicated service account and directories.
2. Install Python 3.12 only if the audit shows it is absent.
3. Upload application files without `.env`, `.venv`, caches, or unrelated databases.
4. Copy the verified 19-job SQLite database to the state directory without overwriting an existing file.
5. Create `hunter.env` manually from the example, leaving `HUNTER_MODE=manual`.
6. Install and start the systemd unit.
7. Add a new, separately named Nginx server block and run `nginx -t` before reload.
8. Point a dedicated DNS name at the VPS and issue a trusted TLS certificate.

Never overwrite an existing unit, Nginx file, SQLite file, user, or directory without stopping for review.

## Manual verification order

With `HUNTER_MODE=manual`:

```sh
systemctl status freelance-hunter --no-pager
curl --fail http://127.0.0.1:8765/health
curl --fail https://HUNTER_DOMAIN/health
/opt/freelance-hunter/venv/bin/python -m pytest -q
/opt/freelance-hunter/venv/bin/python -m app.report_analyzer_v1
```

Expected database invariant: 19 jobs and 19 current `analyzer_v1` results.

## Telegram order

Do not paste tokens into chat or command-line arguments. Put them directly into `/etc/freelance-hunter/hunter.env` using a server-side editor.

1. Put `TELEGRAM_BOT_TOKEN` in the environment file.
2. Send `/start` to the bot from the intended private chat.
3. Before setting a webhook, run `python -m app.telegram_admin discover-chat`; put the single expected ID into `TELEGRAM_ALLOWED_CHAT_ID`.
4. Add a random `TELEGRAM_WEBHOOK_SECRET` using only letters, digits, `_`, and `-`.
5. Restart the service while it is still in manual mode.
6. Run `python -m app.telegram_admin get-me`.
7. Register `https://HUNTER_DOMAIN/telegram/webhook` with `python -m app.telegram_admin set-webhook --url https://HUNTER_DOMAIN/telegram/webhook`.
8. Run `python -m app.telegram_admin webhook-info`.
9. Send one at-most-once card with `python -m app.telegram_admin smoke-card`.
10. Press only `🔥 Интересно` and `👎 Не подходит`; do not press the paid draft button during smoke testing.

The admin helper never prints the bot token and never calls Polza. The smoke card writes a notification reservation before sending, so a service restart cannot duplicate it.

## Restart check

Restart only the `freelance-hunter` unit, never n8n:

```sh
sudo systemctl restart freelance-hunter
curl --fail https://HUNTER_DOMAIN/health
```

Confirm that the previously sent smoke card is not sent again and its notification record remains `sent`.

## Live switch (not part of deployment preparation)

Keep `HUNTER_MODE=manual` through all checks. Switching it to `live` requires a separate explicit user instruction, followed by a restart of only `freelance-hunter`.
