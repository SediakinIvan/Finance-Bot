# Telegram PnL entry bot

A private Telegram bot for guided income/expense entry. It **only appends confirmed rows to the existing `Траты` sheet**; existing formulas and analysis remain in Google Sheets and are not edited. The bot reads button choices from a separate `Категории` sheet. It does not archive, clear, or otherwise update any other tab.

## Current transaction flow

1. `/add` → choose today, yesterday, or enter a date as `ДД.ММ.ГГГГ` (ISO `YYYY-MM-DD` is also accepted).
2. Choose expense or income.
3. For an expense, choose its category and subcategory. For income, choose its source; the saved category is always `Доход`.
4. Enter a positive integer RUB amount.
5. Choose whether to add a note. If yes, type it (up to 1000 characters).
6. Review the full entry and confirm or cancel.

`/cancel` cancels an in-progress entry; `/help` shows the commands. The bot is allowlisted to one numeric Telegram user ID. Failed/uncertain Sheets appends are not automatically retried, to avoid duplicate rows; inspect `Траты` before attempting again.

## One-time setup

### 1. Prepare Google Sheets

- Keep the existing spreadsheet formulas and analysis as they are.
- Add a tab named `Категории` (or set `GOOGLE_CATEGORIES_SHEET` to another name). Copy the contents of [`data/categories-template.csv`](data/categories-template.csv) into it, including the header row. The template is based on the exported transaction history and analysis; edit choices in the live tab to match your canonical categories.
- The header row must contain `Тип`, `Категория`, and `Подкатегория`. Each option is one row. Supported types are `Расход` and `Доход`. For income, use `Доход` in both type and category columns; subcategory is the income source.
- The service account needs **Editor** access to the spreadsheet. The bot reads `Категории` and appends rows to `Траты`; it does not create or modify tabs.

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
- Verify the `Траты` row has exactly these columns in order: `Дата, Категория, Подкатегория, Сумма, Доход/Расход, Примечание`.
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

GitHub Actions runs unit tests and builds the Docker image on pull requests and pushes to `main`. On a successful push to `main`, it also publishes the image to GitHub Container Registry (GHCR). VPS deployment with Docker Compose over SSH remains **disabled by default** and will only run after the VPS is prepared and `ENABLE_VPS_DEPLOY` is set to `true`.

### Prepare the Ubuntu 24.04 VPS

Install Docker Engine and the Docker Compose plugin from Docker's official Ubuntu repository. Create a deployment account with SSH access and permission to use Docker. Docker access is root-equivalent, so use a dedicated account and protect its SSH key. Allow outbound HTTPS from the VPS to GHCR.

Before enabling deployment, create `/opt/financebot/.env` on the VPS with the required settings from `.env.example`. Set `GOOGLE_CREDENTIALS_FILE` to `/run/secrets/google-service-account.json`. Put the service-account key at `/opt/financebot/secrets/google-service-account.json`, readable by the container's UID `10001` (for example, owned by `10001:10001` with mode `0400`). Protect the directory and `.env`; neither is copied by deployments. The app is configured to run as a non-root user in a read-only container, with no published ports.

The first image is private in GHCR. Create a GitHub personal access token with `read:packages` for the VPS image pull and deployment workflow. In GitHub, create a `production` environment and set these **environment secrets**:

- `VPS_SSH_PRIVATE_KEY`: private SSH key authorized for the VPS deployment account.
- `VPS_KNOWN_HOSTS`: verified SSH host key for the VPS. Verify the fingerprint through a trusted channel.
- `GHCR_PULL_TOKEN`: token with permission to pull this package.

Set these **repository variables** under **Settings → Secrets and variables → Actions → Variables**:

- `VPS_HOST`: VPS hostname or IPv4 address.
- `VPS_USER`: dedicated deployment account.
- `GHCR_USERNAME`: GitHub username associated with the pull token.
- Optional `VPS_PORT` (default `22`) and `VPS_APP_DIR` (default `/opt/financebot`).
- `ENABLE_VPS_DEPLOY`: set to `true` only after Docker, the server `.env`, credentials file, and all deployment secrets are ready.

Deployment intentionally fails rather than modifying the server if `.env` or the Google key is missing. After the VPS is prepared and GitHub secrets/variables are set, setting `ENABLE_VPS_DEPLOY=true` allows pushes to `main` to deploy the exact commit image. Pull requests run tests and build the image but never publish or deploy it. The workflow does not bootstrap Docker or create server credentials.
