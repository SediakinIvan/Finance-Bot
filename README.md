# Telegram PnL entry bot

A private Telegram bot for guided income/expense entry backed by Google Sheets. This development version requires schema v2: stable category IDs, dynamic analytics, and coordinated Apps Script installation. **Do not deploy it against the existing v1 workbook before migration.** The current deployed v1.5.1 release remains compatible with the old workbook.

See [migration and Apps Script installation](docs/category-migration.md) and [v2 schema](docs/category-schema-v2.md). Category management buttons are a later feature; this change provides their underlying services.

## Current transaction flow

1. Choose **Добавить транзакцию (сегодня)** or **Добавить транзакцию (вчера)** to use that date directly, or choose **Добавить транзакцию** (or `/add`) to enter another date as `ДД.ММ.ГГГГ` (ISO `YYYY-MM-DD` is also accepted). Today/yesterday flows skip the comment question; use **Добавить комментарий** in the confirmation if a note is needed. The main menu keyboard also offers **Аналитика**, **Аналитика по категориям**, **Версия**, **Отменить текущую запись**, **Последние 10 операций**, **Удалить последнюю**, and **Выбрать для удаления**.
2. Choose expense or income.
3. For an expense, choose its category and subcategory. For income, choose its source; the saved category is always `Доход`.
4. Enter a positive integer RUB amount using the inline numeric keypad or by typing the amount as a message. Telegram bots cannot force the phone's system keyboard to switch to numeric mode.
5. Review the transaction and confirm it. Comments are optional: press **Добавить комментарий** in the confirmation if needed (up to 1000 characters).
6. Review the full entry and confirm or cancel.

The category and subcategory choice buttons include matching emoji where known; unknown labels use a neutral marker, while the saved category values remain unchanged. **Удалить последнюю** and **Выбрать для удаления** show the selected transaction and require confirmation. Confirmed deletion removes the entire physical row from that user's `Траты` sheet, shifting subsequent rows. The main menu can be collapsed using Telegram’s keyboard controls and reopened with the keyboard icon or `/help`. It does not request persistent display when the regular keyboard is hidden. After upgrading, send `/help` once to replace the old keyboard with the updated, collapsible menu. The menu buttons have slash-command equivalents: `/cancel` cancels an in-progress entry, `/help` shows the menu, and `/version` reports the deployed release tag and short source commit. The bot accepts configured Telegram user IDs only in private chats. `TELEGRAM_ALLOWED_USER_ID` uses `GOOGLE_SHEET_ID`; optional `TELEGRAM_USER_SHEETS` adds isolated users in `TELEGRAM_ID:SPREADSHEET_ID` format, with comma-separated entries. Each spreadsheet must grant Editor access to the configured Google service account. Failed/uncertain Sheets writes are not automatically retried, to avoid duplicate transactions; inspect `Траты` before attempting again.

**Аналитика** reads the calculated monthly income, expenses, current balance, and category totals from the existing `Аналитика` sheet and sends a donut chart. **Аналитика по категориям** lists categories dynamically, using stable IDs and pages, then reports the selected category's total and subcategory totals with a chart. The bot only reads this sheet; formulas and source values are maintained in Google Sheets. Set `GOOGLE_ANALYTICS_SHEET` if the tab has a different name.

## One-time setup

### 1. Prepare Google Sheets

- Start with a native copy of the workbook and a separate native backup. Follow [the migration runbook](docs/category-migration.md), including explicit historical-name mappings and Apps Script verification.
- Keep user-facing transaction columns A:F in their existing order. Migration adds protected service columns G:K.
- Edit transactions through the bot or the sheet dropdowns. Category registries and generated views are maintained by services, not by manually adding rows to the registry.
- The bot verifies schema version, identity scope, revision and readiness before access. Old or unfinished books are rejected rather than written as v2.
- Share only the intended user spreadsheet with the configured service account. Each authorized Telegram user retains a separate spreadsheet.

### 2. Create Telegram credentials

- Create a bot with Telegram's `@BotFather` and copy its token.
- Find your own numeric Telegram user ID (for example, with a trusted user-ID bot). Do not use a username as the allowlist; usernames can change.
- Do not paste either value into source code or commit them.

### 3. Set up Google service-account access

- In Google Cloud, enable the Google Sheets API and create a service-account key in JSON format.
- Store the downloaded JSON locally at `secrets/google-service-account.json` (the `secrets/` directory is git-ignored).
- Share the spreadsheet with the service account's `client_email` as an Editor.
- Copy the spreadsheet ID from its URL: it is the portion between `/d/` and `/edit`.

### 4. Install and configure (macOS)

From the project directory:

1. Create and activate a virtual environment: `python3 -m venv .venv` then `source .venv/bin/activate`.
2. Install dependencies: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and fill in the Telegram token, your numeric Telegram ID, Google service-account JSON path, and spreadsheet ID. Keep `.env` private; it is git-ignored.
4. Set `BOT_TIMEZONE` to your local IANA timezone if it is not `Europe/Moscow`.
5. Run `python main.py`. Keep this process running to receive bot updates.

Telegram's long-polling mode does not need a public URL. Only run one polling process for the bot token at a time.

## Verify before live use

- Run automated tests with `python -m unittest discover -s tests`.
- First test against a copy of the spreadsheet, with the bot token and allowlisted ID configured locally.
- Verify the `Траты` row has exactly these columns in order: `Дата, Категория, Подкатегория, Сумма, Доход/Расход, Примечание`. New transactions reuse the first row whose `Дата` cell is empty.
- Check that formulas recalculate after an appended row. The bot writes the date as text in `ДД.ММ.ГГГГ` format and uses RAW values so the positive integer amount stays numeric.

## Git and CI

### Connect the project to GitHub

If this directory has not been initialized as a Git repository yet:

```sh
git init -b main
```

Create an empty GitHub repository, add it as `origin`, then commit and push the project:

```sh
git remote add origin git@github.com:OWNER/REPOSITORY.git
git add .
git status --short
git commit -m "Initial project setup"
git push -u origin main
```

Review `git status` before committing. The `.gitignore` excludes `.env`, credentials in `secrets/`, virtual environments, and CSV exports in `data/`; only `data/categories-template.csv` is kept. Do not force-add private files.

GitHub Actions runs unit tests on pull requests, pushes to `main`, and version tags (`v*`). Pushes to `main` publish a development image without deploying it; pushing a version tag deploys that release image and publishes a GitHub Release.

### Prepare the Ubuntu 24.04 VPS

The target Ubuntu 24.04 VPS has Docker Engine and Docker Compose installed, and a dedicated `financebot-deploy` account with SSH-key access and Docker permissions. Docker access is root-equivalent, so protect its SSH key. Allow outbound HTTPS from the VPS to GHCR.

Before enabling deployment, create `/opt/financebot/.env` on the VPS with the required settings from `.env.example`. Set `GOOGLE_CREDENTIALS_FILE` to `/run/secrets/google-service-account.json`. Put the service-account key at `/opt/financebot/secrets/google-service-account.json`, readable by the container's UID `10001` (for example, owned by `10001:10001` with mode `0400`). Protect the directory and `.env`; neither is copied by deployments. The app is configured to run as a non-root user in a read-only container, with no published ports.

The first image is private in GHCR. Create a GitHub personal access token with `read:packages` for the VPS image pull and deployment workflow. In GitHub, create a `production` environment and set these **environment secrets**:

- `VPS_SSH_PRIVATE_KEY`: private SSH key authorized for the VPS deployment account.
- `VPS_KNOWN_HOSTS`: verified SSH host key for the VPS. The currently verified known-hosts line is:

  ```text
  89.125.90.156 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK1u1JstGWLGXCUy3OV0hckBF9C4Cwc5d4r/aW3Sp16u
  ```

  Its fingerprint, verified against the key on the VPS, is `SHA256:buwlUuqRTfopPVWID7YhJ0ZlQLp1WP4cS9Xga2zgBok`.
- `GHCR_PULL_TOKEN`: token with permission to pull this package.

Set these **repository variables** under **Settings → Secrets and variables → Actions → Variables**:

- `VPS_HOST`: VPS hostname or IPv4 address.
- `VPS_USER`: dedicated deployment account.
- `GHCR_USERNAME`: GitHub username associated with the pull token.
- Optional `VPS_PORT` (default `22`) and `VPS_APP_DIR` (default `/opt/financebot`).
- `ENABLE_VPS_DEPLOY`: set to `true` only after Docker, the server `.env`, credentials file, and all deployment secrets are ready.

Deployment intentionally fails rather than modifying the server if `.env` or the Google key is missing. With `ENABLE_VPS_DEPLOY=true`, only version tags (`v*`) deploy the exact commit image to the VPS; a manual workflow run deploys only when its ref is a version tag. Pushes to `main` run tests and publish a development image but do not change production. Pull requests run tests and build the image but never publish or deploy it. The workflow does not bootstrap Docker or create server credentials.

### Category management (v2)

`/categories` or «Категории» opens creation, renaming, moving and deactivation with explicit confirmation. Existing operations and archive amounts are preserved. See [setup, persistent journal and recovery](docs/category-management.md) before enabling this version. Prepare `secrets/category-changes` for UID 10001; the Compose mount must survive container replacement. Validate on a separate test bot and a migrated v2 copy before the production migration/release.
