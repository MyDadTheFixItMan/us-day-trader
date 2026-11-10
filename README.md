# US Day Trader

A paper-trading (no real money) day-trading bot for US shares, starting with US$10,000.
It is the ASX Day Trader's rules moved to New York hours. It runs on GitHub Actions every
5 minutes through the US trading day (about 12:30am-7am Sydney) using Yahoo Finance prices.
Its results show in the "US Day Trader" tab of the ASX Paper Trader's desktop page, which
reads the files in `data/`.

## How it trades

- Each morning, scan the S&P 500, MidCap 400 and SmallCap 600 (`data/us_codes.txt`) and keep
  shares priced $5-$300 that trade at least $20M a day and move at least 1.5% a day.
- Watch the 5 **stocks in play**: opening volume at least 3x normal, a green opening range and
  above yesterday's close (Zarattini, Barbon & Aziz 2024).
- Buy when the price breaks the high of the first 15 minutes after the 9:30 open, only above
  VWAP. Stand aside when the S&P 500 opens down more than 1%.
- Risk 1% of the account per trade, at most 25% in one share and 5 at once. Stop below the
  opening range or half a normal day's move (at least 1%).
- No new buys after 12:00; everything sold at 15:50 New York time. Stop for the day after a
  2% loss or 3 losses in a row.
- Costs: no commission (as at most US brokers) plus 0.05% slippage each way.

Every setting lives in `bot/config.json`.

## Limits

- 8% a week is a challenge target, not an expectation. Most day traders lose money.
- GitHub's scheduled runs can start late or be skipped. The bot replays missed 5-minute bars
  in order, so a late start doesn't change the results.
- The repo is public so GitHub Actions stays free. It contains only paper trades.

## Files

- `bot/trader.py`: runs each 5-minute cycle · `bot/engine.py`: trading rules · `bot/data.py`: prices
- `data/`: state, trades and equity written by the bot
- `tests/simulate_day.py`: replays a made-up trading day through the whole bot
