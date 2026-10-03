# Goal Ledger

NHL anytime-goalscorer picks, rebuilt by GitHub Actions every half hour and served by GitHub Pages.

## Setup (once)

1. Create a new **public** repository and add everything in this folder to it, including `.github/workflows/refresh.yml`.
2. **Settings → Secrets and variables → Actions → New repository secret.** Name `ODDS_API_KEY`, value your Odds API key.
3. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
4. **Actions → Refresh Goal Ledger → Run workflow.** The first run takes a few minutes (it downloads three seasons).

The page is then at `https://<your-username>.github.io/<repository-name>/` and updates itself while open.

## Price requests

`ODDS_LOOKS` in `.github/workflows/refresh.yml` sets when each game is priced, in hours before puck drop.
`"24,2"` prices tomorrow's games a day ahead and again two hours out: about 2 requests a game plus 4 a day
for moneylines and totals, roughly 540 a month in a full NHL month. `"6"` is one look, roughly 330 a month.
Prices already fetched are stored in `odds.json` and never asked for again.
