# rh-emailer

Compliant AI-assisted outreach backend for RedHat Funding MCA lending. This FastAPI
service parses financial documents, generates templated email previews, and executes
rate-limited SendGrid delivery with human-in-the-loop controls.

## Features

- **/prepare** – Upload PDFs/CSVs, parse financial metrics, redact sensitive data, and
  return previews for human approval.
- **/send** – Queue email sends with conservative or assertive tone templates, supporting
  dry-run workflows and SendGrid delivery.
- **/direct_send** – Deliver a single templated HTML email or dry-run a GPT-crafted draft.
- **/status** – Poll job records to monitor preparation and send progress.
- **/unsubscribe** – Suppress recipients to ensure CAN-SPAM compliance (GET and POST).
- **/health** – Lightweight health probe for infrastructure checks.

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Visit `http://localhost:8000/docs` for Swagger UI.

## API Quickstart

```bash
# health
curl -s http://localhost:8000/health

# single (dry run)
curl -s -X POST http://localhost:8000/direct_send \
  -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
  -d '{"to_email":"test@example.com","subject":"Hello","body_html":"<p>Hi</p>","dry_run":true}'

# prepare batch with multipart tone override
curl -s -X POST http://localhost:8000/prepare \
  -H "Authorization: Bearer dev" \
  -F "tone=assertive" -F "files=@samples/sample_leads.csv"

# unsubscribe via POST (auth required)
curl -s -X POST http://localhost:8000/unsubscribe \
  -H "Authorization: Bearer dev" -H "Content-Type: application/json" \
  -d '{"email":"optout@example.com"}'
```

### Environment Variables

| Name | Description | Default |
| --- | --- | --- |
| `API_BEARER_TOKEN` | GPT Actions auth | *(unset)* |
| `SENDGRID_API_KEY` | SendGrid delivery | *(unset)* |
| `FROM_EMAIL` | CAN-SPAM sender | `funding@rhfunding.io` |
| `FROM_NAME` | Inbox display | `RedHat Funding` |
| `BUSINESS_NAME` | Footer brand | `RedHat Funding` |
| `BUSINESS_ADDRESS` | Physical address | `123 Main St, Fort Lauderdale, FL 33301, USA` |
| `OPTOUT_MODE` | Unsub style | `link` |
| `OPTOUT_LINK` | Opt-out URL | `https://rhfunding.io/unsubscribe` |
| `MPS_LIMIT` | Emails/min | `60` |
| `WINDOW_SECONDS` | Rate window | `60` |
| `DB_PATH` | SQLite path | `sqlite:////tmp/emailer.db` |
| `REPLY_TO_EMAIL` | Reply-to address for outreach messages. | `funding@rhfunding.io` |
| `LOG_LEVEL` | Application log verbosity. | `INFO` |

## Docker Usage

```bash
docker build -t rh-emailer .
docker run -p 8000:8000 --env-file .env rh-emailer
```

## Deployment

Deploy via Render using the provided `render.yaml`. Environment variables should be
configured in the Render dashboard with secrets for SendGrid and API bearer token.

## Compliance

- Dry_run=true default; human "CONFIRM SEND" in GPT.
