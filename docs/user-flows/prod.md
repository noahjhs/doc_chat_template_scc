# Production

Promoted 2026-10-13 (from `dev` at `b1d0b51`). Prod is the third Casper
stack on the mini, built from the same parameterized `docker-compose.yml` as
dev and the test stack (`tools/test_stack.sh`); only its env file differs.

## Layout

- Checkout: `~/doc_chat_template_scc-prod` on `casper-mini`, on `main`.
- Compose project `casper-prod`, env file `.env.prod` (mode 600, not in
  git). Containers: `casper-www-prod`, `casper-auth-prod`,
  `casper-catcher-prod`, `casper-relay-prod`. It shares dev's Syncthing
  relay (`casper-st-relay`, port 22067), like the test stack.
- Volumes: `casper-prod_auth_db` (database, started fresh at promotion),
  `casper-prod_catcher_data`. The old Streamlit-era prod's volume
  `doc_chat_template_scc-prod_auth_db` is kept, unused; a copy of it and of
  the old `.env`/compose file is in `~/casper-backups/prod-2026-10-13/`
  and on the Time Capsule (see Backups).
- Ports: debug (localhost only) www 8082, auth 8110, relay 8610; catcher sync
  port 22102 (dev 22100, test 22101).
- Addresses (cloudflared, `~/casper-infra/cloudflared/config.yml`):
  `www` and `app.casperagent.dev` -> `casper-www-prod:8080`,
  `auth` -> `casper-auth-prod:8100`, `relay` -> `casper-relay-prod:8600`.
- `.env.prod` is dev's `.env` with the domains changed to the prod ones,
  minus `DEV_ADMIN_TOKEN` (so the `/dev/*` endpoints don't exist) and
  `MAIL_SINK` (prod sends real email through SMTP), with prod's own
  `TELEGRAM_*`, plus the stack settings (`ENV_SUFFIX=-prod`,
  `PAIR_SCHEME=casper`, `PUBLIC_*`, its own
  `CATCHER_API_KEY`/`CATCHER_URL`/`CATCHER_ADDRESSES`).
- Telegram (2026-10-13): its own bot, **@Casper_svc_bot** (dev has a
  different one). `.env.prod` holds `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_BOT_USERNAME=Casper_svc_bot` and a random
  `TELEGRAM_WEBHOOK_SECRET` (`openssl rand -hex 24`). The webhook was
  registered from the mini with the Bot API's `setWebhook`:
  `url=https://auth.casperagent.dev/telegram/webhook`,
  `secret_token=$TELEGRAM_WEBHOOK_SECRET`,
  `allowed_updates=["message","callback_query"]` (all `/telegram/webhook`
  reads), `drop_pending_updates=true`; `getWebhookInfo` should show that URL
  and no `last_error`. Make the calls with a small Python script on the mini
  that reads `.env.prod` itself (the file isn't shell-sourceable, and that
  keeps the token off command lines). The bot's profile (description,
  commands) is set by hand in @BotFather; Casper doesn't set it. People link
  their chat with `Casper setup notifications` on a paired Mac (their agent
  runs it, and it turns notifications on), or `harness login --prod`, then
  `harness profile telegram-link` and `harness profile set
  --telegram-notifications`.
- The app: "Casper", `casper://` scheme, built with
  `DEPLOY_ENV=prod ./build/build_go_macos.sh` (reads `auth_server.txt`,
  `relay_server.txt`, `app_server.txt`) and served from the checkout's
  `dist/Casper-macos.zip` at `https://auth.casperagent.dev/download/casper/macos`.

## Backups

Backups belong on the Time Capsule (2.7 TB), not the mini's own disk:
`/Volumes/Data/casper-backups/<name>/` on the mini, one folder per backup
(first: `prod-2026-10-13`, copied 2026-10-13 and checked with `shasum -a 256`;
the original in `~/casper-backups/` stays as a local copy).

- The Time Capsule is `Noahs-AirPort-Time-Capsule.local` (192.168.0.107 on
  the home LAN), SMB share `Data`, user `admin`. The password is in the
  mini's login keychain (internet password, server
  `Noahs-AirPort-Time-Capsule.local`, protocol smb), saved when the
  Principal first connected.
- It does not mount itself after a reboot (the mini has no auto-login, and
  the share isn't a login item). A backup job checks first and mounts it if
  needed:

  ```bash
  [ -d /Volumes/Data/casper-backups ] || {
    open 'smb://admin@Noahs-AirPort-Time-Capsule.local/Data'   # uses the keychain
    sleep 10; [ -d /Volumes/Data/casper-backups ]; }
  ```

  `open` needs someone logged in to the mini's desktop (it does the mount
  through Finder, which reads the keychain). Over SSH with no one logged in,
  `mount_smbfs //admin@Noahs-AirPort-Time-Capsule.local/Data <dir>` is the
  alternative but may not reach a locked keychain. If neither works, ask the
  Principal to connect once in Finder (Go > Connect to Server).
- Copy with `cp -Rp` and compare `shasum -a 256` of both sides. SMB adds
  `._*` files for extended attributes; they're harmless.

## Redeploying

`main` fast-forwards to `dev` (`git checkout main && git merge --ff-only dev
&& git push origin main`). Then:

```bash
ssh casper-mini 'source ~/.orbstack/shell/init.zsh; cd ~/doc_chat_template_scc-prod &&
  git fetch -q origin && git checkout -q main && git merge -q --ff-only origin/main &&
  docker compose -p casper-prod --env-file .env.prod up -d --build www auth catcher relay'
```

Never start `st-relay` from this project (dev owns it). If `agent/` or
`build/` changed, rebuild the app locally and copy it up:

```bash
DEPLOY_ENV=prod ./build/build_go_macos.sh
scp dist/Casper-macos-go.zip casper-mini:doc_chat_template_scc-prod/dist/Casper-macos.zip
```

Check afterwards: `www`, `app/guide`, `auth/agents.md` and
`auth/invites/info?code=x` answer 200; `auth/dev/users` answers 404;
`docker logs casper-auth-prod` shows a clean start.

## Not there yet

Nothing listed. (DMARC was tightened by the Principal on 2026-10-13:
`p=quarantine`, reports through Cloudflare DMARC Management.)
