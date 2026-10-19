# Google AI Overview Scraper

A desktop GUI tool (built with [NiceGUI](https://nicegui.io/) + Selenium/Firefox) that collects Google "AI Overview" answers for a list of search queries, including:

- the full AI Overview answer text
- the single highlighted "main answer" claim
- the list of sources shown at the bottom of the AI Overview
- the individual bullet-point claims inside the answer, each paired with the specific source(s) cited for that claim
- the "AI responses may include mistakes" disclaimer text

Optionally, it can also save a raw HTML snapshot of each cited source page and a screenshot of the AI Overview itself, for later manual verification if Google changes its markup.

## Installation instructions

Prebuilt installers are attached to each [release](https://github.com/tomkuncoro-yk/GAOS/releases/latest). Firefox still needs to be installed separately either way.

**Windows**

1. Download `GoogleAIOverviewScraperSetup.exe` from the release page.
2. Double-click it. Windows SmartScreen will likely show a blue "Windows protected your PC" screen, because the installer isn't from a registered publisher.
3. Click **More info**, then **Run anyway**.
4. Follow the setup wizard (Next → Next → Install). It adds a Start Menu shortcut and a normal uninstaller.

**macOS**

1. Download `GoogleAIOverviewScraper.dmg` from the release page.
2. Double-click it to mount the disk image, then drag the app into **Applications**.
3. Open it. macOS will say it's "from an unidentified developer" (or, on newer macOS, "is damaged and can't be opened" — despite the wording, the app isn't actually damaged, it's the same Gatekeeper block).
4. Go to the Apple menu → **System Settings** → **Privacy & Security**, scroll down to the **Security** section, and click **Open Anyway** next to the message about the blocked app.
5. Try opening it again and confirm **Open** in the dialog that appears.
6. If step 4 doesn't show an "Open Anyway" option at all, open **Terminal** and run:
   ```bash
   xattr -cr /Applications/GoogleAIOverviewScraper.app
   ```
   then try opening it again.
7. The first time you start a scrape, macOS may also ask for permission for the app to control/automate Firefox. Click **Allow** — this is normal; it's how the app drives the browser.

## Run from source

Needs Python 3.10+ (plus Firefox, as above). Selenium downloads the matching `geckodriver` automatically the first time the app runs — nothing else to install for that.

```bash
git clone https://github.com/tomkuncoro-yk/GAOS.git
cd GAOS

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
python main.py
```

A native app window opens. Solve any CAPTCHA that appears in the Firefox window that launches, then click "Start scraping" in the app window.

## Credit

This tool owes its existence to **Kenniskrabber**, the excellent AI Overview/AI Mode scraper built by the [Digital Methods Initiative (DMI)](https://digitalmethods.net/): https://github.com/digitalmethodsinitiative/kenniskrabber.

Kenniskrabber is the reference this project was built from and tested against. This is a separate, from-scratch codebase rather than a fork, built to focus specifically on AI Overview claims and sources. To match each cited source back to the specific claim it supports, GAOS hovers each source badge and walks up the DOM from that *exact* badge element to find its enclosing claim, using Google's own per-segment `data-hveid` attribute as the anchor.

Full credit to the DMI team and contributors for the original tool and the groundwork it lays out.

## Output

For each query you get one row in a CSV (and JSONL) results file, under `~/Documents/GoogleAIOverviewScraper/scrapes/`, plus optionally:
- a saved raw HTML snapshot of each cited source page
- a full-page screenshot of the AI Overview

## CSS selectors

All the CSS selectors this scraper relies on live in `css-selectors.json` next to `main.py`, and are also editable live in the app's "CSS selectors" panel. If Google changes its markup and the scraper stops finding things, that file (or the panel) is the first place to fix it — no need to touch the Python code, with one exception: the claims-matching logic (see `get_claims()` in `main.py`) is intentionally not selector-driven, for the reason explained above.
