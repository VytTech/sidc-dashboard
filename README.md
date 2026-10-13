# SIDC Daily Overview

Password-protected director dashboard built from Ragic (account `SIDC10000`), refreshed every day by GitHub Actions and hosted on GitHub Pages.

- `fetch_ragic.py`: reads the Projects, Human Resources (status fields only), and Additional man power sheets through the Ragic API.
- `build.py`: aggregates the data into name-free numbers, stores an encrypted daily snapshot in `history/`, compares it with the previous day and renders `template.html`.
- `.github/workflows/daily.yml`: runs daily at 04:00 UTC (and on demand), encrypts the page with StatiCrypt and deploys it to Pages.

## Setup

1. Repository → Settings → Secrets and variables → Actions:
   - Secret `RAGIC_API_KEY`: API key of a **read-only** Ragic service account.
   - Secret `PAGE_PASSWORD`: password the directors type to open the page. It also encrypts `history/`, so changing it restarts the history.
   - Variable `RAGIC_BASE` (optional): the Ragic server from the browser address bar, e.g. `https://ap5.ragic.com`. Default `https://www.ragic.com`.
2. Settings → Pages → Source: **GitHub Actions**.
3. Actions → Daily dashboard → **Run workflow** for the first build.

## Local preview

```
py -I build.py local/raw local/history local/site/index.html
```
