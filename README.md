# Trades Exam Prep Prospect Finder

Discovers YouTube-based trades exam prep educators, extracts founder information, verifies email addresses via Hunter.io, and logs results to Google Sheets. Deduplication is handled by a Neon PostgreSQL database.

## Supported Trades

- **HVAC** — EPA 608, journeyman, certification
- **Electrical** — journeyman, master, NEC code
- **Plumbing** — journeyman, master license
- **CDL** — permit, skills, air brakes

## How to Run (Normal Use)

1. Go to the **Actions** tab in this GitHub repository
2. Click **Run Prospect Finder** in the left sidebar
3. Click **Run workflow**
4. Select a trade from the dropdown
5. Click the green **Run workflow** button

Results appear in the Google Sheet within 15–30 minutes. Each run appends new rows to the "Founder Identified" and "Founder Unknown" tabs. Duplicates are automatically skipped.

## GitHub Secrets Setup (One-Time)

Go to: Repository → Settings → Secrets and variables → Actions → New repository secret

Add all six secrets:

| Secret Name | Where to get it |
|---|---|
| `YOUTUBE_API_KEY` | Google Cloud Console → APIs & Services → Credentials |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `HUNTER_API_KEY` | hunter.io → Dashboard → API |
| `GOOGLE_SHEET_ID` | The long ID in your Google Sheet's URL |
| `GOOGLE_SHEETS_CREDENTIALS_JSON` | Service account JSON (minified — see below) |
| `NEON_DATABASE_URL` | Neon dashboard → Connection Details |

### Google Service Account Setup

1. Google Cloud Console → IAM & Admin → Service Accounts → Create Service Account
2. Enable the **Google Sheets API** and **Google Drive API** in your project
3. Create a JSON key: Service Account → Keys → Add Key → JSON → Download
4. Minify the JSON to a single line:
   ```bash
   python -c "import json,sys; print(json.dumps(json.load(open('your-creds.json'))))"
   ```
5. Paste the output as the `GOOGLE_SHEETS_CREDENTIALS_JSON` secret
6. Share your Google Sheet with the service account email address (Editor access)

## Local Development Setup

```bash
git clone <repo>
cd lead-finder
cp .env.example .env
# Fill in .env with your API keys
uv sync
```

### Dry Run (no API costs, no writes)

```bash
uv run prospect-finder run --trade hvac --dry-run
```

Runs discovery and extraction, prints results as JSON. Skips Hunter.io and all database/sheet writes.

### Full Run (small test)

```bash
uv run prospect-finder run --trade hvac --limit 5
```

### View Stats

```bash
uv run prospect-finder stats
uv run prospect-finder stats --trade hvac
```

## Google Sheet Structure

Two tabs are created automatically on first run:

**Founder Identified** — prospects where a founder name and email were found

**Founder Unknown** — prospects where extraction or email verification failed (YouTube channel still valuable)

Columns: `run_date`, `trade`, `company_name`, `website`, `founder_name`, `founder_role`, `founder_email`, `email_confidence_score`, `youtube_channel_url`, `youtube_subscriber_count`, `team_size_signal`, `has_newsletter_signal`, `has_lead_magnet_signal`, `country`, `notes`

## Adding a New Trade

1. Add keywords to `config/keywords.yaml` under a new trade key
2. Add the trade to `.github/workflows/run-finder.yml` under `inputs.trade.options`
3. Add it to the `click.Choice` list in `src/prospect_finder/cli.py`

## Running Tests

```bash
uv run pytest
```

## Cost Estimate Per Run (~400 candidates)

| Service | Cost |
|---|---|
| YouTube Data API | $0 (free quota) |
| Claude Haiku | ~$1.00 |
| Hunter.io | Starter plan ($34/month flat) |
| Google Sheets API | $0 |
| Neon PostgreSQL | $0 (free tier) |
| GitHub Actions | $0 (free tier) |
