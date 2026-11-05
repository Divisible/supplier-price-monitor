# Fora price monitor — pilot

This is a **diagnostic pilot**, not yet an automated price history. It collects rendered page text, candidate price fields, and screenshots from the three URLs in `products.csv`. No unverified prices are saved.

## Run on Windows

1. Install Python 3.11+.
2. Download this branch as ZIP or clone the repository.
3. Open a terminal in the extracted project directory and run:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py collector.py
```

4. Share `diagnostics.json` and the three product screenshots for review. These files are ignored by Git. Check that the diagnostics contain no personal information before sharing.

## Next

Validate price selectors and whether site prices depend on region. Only then add XLSX history, automated schedule and notifications. Do not store secrets in this repo.
