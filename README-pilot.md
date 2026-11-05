# Fora price collector — diagnostic pilot

This initial version **does not extract verified prices** and **does not create a price history**. It opens the three product pages and captures text, potential price fields and screenshots for validation.

## Run on your Windows PC
1. Install Python 3.11+.
2. Download the repository as a ZIP from GitHub or clone it.
3. Open a terminal in the project folder.
4. Run:
   ```powershell
   py -m pip install -r requirements.txt
   py -m playwright install chromium
   py collector.py
   ```
5. Send the resulting `diagnostics.json` and screenshots (`32550.png`, `32546.png`, `32485.png`) for review.

Do not add passwords, access tokens or personal information to this repository.

**Next:** validate selectors and meaning of the displayed price, then implement history + XLSX output and an automated schedule. Running daily before the selector is verified risks recording incorrect prices.
