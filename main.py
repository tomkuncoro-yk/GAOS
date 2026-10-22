"""
Search Engines AI Overviewer
========================

A GUI tool (built with NiceGUI + Selenium/Firefox) that collects Google
"AI Overview" answers for a list of search queries, including:

  - the full AI Overview answer text
  - the single highlighted "main answer" claim
  - the list of sources shown at the bottom of the AI Overview
  - the individual bullet-point claims inside the answer, each paired
    with the specific source(s) cited for that claim
  - the "AI responses may include mistakes" disclaimer text

This tool owes its existence to "Kenniskrabber", the excellent AI
Overview/AI Mode scraper built by the Digital Methods Initiative (DMI):
https://github.com/digitalmethodsinitiative/kenniskrabber. Kenniskrabber
is the reference this project was built from and tested against. This
is a separate, from-scratch codebase rather than a fork, built to
focus specifically on AI Overview claims and sources. To match each
cited source back to the specific claim it supports, this scraper
hovers each source badge and walks UP the DOM from that *exact* badge
element to find its enclosing claim, using Google's own per-segment
`data-hveid` attribute as the anchor. Full credit to the DMI team and
contributors for the original tool and the groundwork it lays out.

Requirements
------------
    pip install -r requirements.txt

You also need Mozilla Firefox installed, and a matching 'geckodriver'
on your PATH (download from
https://github.com/mozilla/geckodriver/releases and drop it somewhere
on PATH, e.g. C:\\Windows or alongside this script).

Usage
-----
    python google_ai_overview_scraper.py

A browser window opens automatically. Solve any CAPTCHA that appears,
then click "Start scraping" in the app window.

Output
------
For each query you get one row in `ai_overview_results.csv` /
`.jsonl`, plus (optionally) a saved raw HTML snapshot and a screenshot
of the AI Overview, for later manual verification if Google changes
its markup again.

CSS selectors
-------------
All the CSS selectors this scraper relies on live in `css-selectors.json`
next to this script, and are also editable live in the app's "CSS
selectors" panel. If Google changes its markup again in the future and
this scraper stops finding things, that file (or the panel) is the
first place to fix it -- no need to touch the Python code, with ONE
exception: the claims-matching logic (see `get_claims()` below) is
intentionally NOT selector-driven, for the reason explained above.
"""

import csv
import hashlib
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, parse_qs, quote_plus
import base64

# --- Crash logging for packaged builds -----------------------------------
# A packaged .app/.exe has no console attached, so stdout/stderr normally
# go nowhere -- if anything raises before the window opens (a bundled
# dependency that didn't make it into the build, a resource path that
# resolves differently inside the .app bundle than it does when running
# from source, etc.) the process just exits and it *looks* like the
# window "opens and immediately closes", with nothing to go on. When
# frozen, redirect stdout/stderr to a log file and install an excepthook
# that writes any uncaught exception's full traceback there before the
# process exits, so a report like that comes with an actual log instead
# of nothing. This has to run before the heavier third-party imports
# below (nicegui, selenium, ...), since a packaging problem is most
# likely to surface as one of THOSE imports failing.
LOG_PATH = None
if getattr(sys, "frozen", False):
    log_dir = os.path.join(os.path.expanduser("~"), "Documents", "SearchEnginesAIOverviewer", "logs")
    os.makedirs(log_dir, exist_ok=True)
    LOG_PATH = os.path.join(log_dir, "latest.log")
    _log_file = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    sys.stdout = _log_file
    sys.stderr = _log_file
    print(f"\n=== Started {datetime.now().isoformat()} ===", flush=True)

    def _log_uncaught(exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
        sys.stderr.flush()

    sys.excepthook = _log_uncaught

import requests
from nicegui import app, ui, run

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    WebDriverException,
    TimeoutException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    NoSuchElementException,
    ElementClickInterceptedException,
)
from selenium.webdriver.support.wait import WebDriverWait

try:
    from markdownify import markdownify as md
except ImportError:  # markdownify is optional; fall back to plain text
    def md(html, **kwargs):
        return re.sub("<[^>]+>", " ", html)


if getattr(sys, "frozen", False):
    BASE_PATH = sys._MEIPASS
else:
    BASE_PATH = os.path.abspath(os.path.dirname(__file__))

LOCAL_SELECTORS_PATH = os.path.join(BASE_PATH, "css-selectors.json")
ASSETS_DIR = os.path.join(BASE_PATH, "assets")
if os.path.isdir(ASSETS_DIR):
    app.add_static_files("/assets", ASSETS_DIR)
APP_VERSION = "0.2"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"

# These are the selectors we actually verified against live Google AI
# Overview markup while building this tool. "ao_claim" deliberately does
# NOT exist here -- see the module docstring and get_claims() below.
DEFAULT_SELECTORS = {
    "ao_container": "#eKIzJc",
    "ao_show_more": "div[aria-controls='m-x-content']",
    "ao_failed_elements": ".YWpX0d[style='']",
    "ao_inner_text": "div[data-container-id='main-col']",
    "ao_source_badge": "button[data-amic='true']",
    "ao_main_claim": "div > mark[jsuid][data-sfc-root]",
    "ao_claim_source_box": "div[data-skip-highlighting] div[data-src-id][data-crb-el]",
    "ao_show_more_urls": "#rw0ISc",
    "ao_url_divs": "div[dir][tabindex] ul > li",
    "ao_url_divs_not_expandable": "ul > li > div[data-src-id]",
    "ao_url_divs_carousel": "div[role='listitem'] > div[data-src-id]",
    "ao_url_title": "div[id]",
    "ao_url_description": "span[data-crb-snippet-text]",
    "ao_url_description_fallback": ".dMCttd",
    "ao_disclaimer": "div:has(> a[href*='p=ai_overviews'])",
    # Best-effort / cosmetic only: strips the "Ask anything" follow-up
    # input box out of the saved HTML/screenshot before saving. Not
    # critical to the actual data extraction -- if this stops matching,
    # nothing breaks except the saved HTML keeps the input box in it.
    "ao_prompt_input": "div[data-active='input-plate-active']",

    # Bing's Copilot Search answer (internally "QnATuringGen"). Unlike
    # Google's AI Overview, Bing renders each claim's citation(s) as a
    # plain inline <a class="md_citlink"> right inside the claim's own
    # <li>/<p> -- no hovering or DOM-ancestry walking needed at all, see
    # get_bing_claims() below.
    #
    # Bing appears to be running two different wrapper variants of this
    # same feature at once (confirmed from real captured pages, not just
    # a one-time rename): some sessions get
    # div.answer_container[aria-label='Copilot Search'] (the older
    # style), others get div.cht_container[aria-label='AI Overview'] (a
    # newer rebrand). Both wrap the exact same inner markup
    # (.cht_mdroot, a.md_citlink, .cht_cit_root, etc.), so the selector
    # below matches either wrapper rather than picking one.
    "bg_container": "div.cht_container[aria-label='AI Overview'], div.answer_container[aria-label='Copilot Search']",
    "bg_mdroot": ".cht_mdroot",
    "bg_claim_items": "li, p",
    "bg_citlink": "a.md_citlink",
    "bg_cit_root": ".cht_cit_root",
    "bg_cit_item": ".cht_cit",
    "bg_cit_title": ".cht_cit_title_text",
    "bg_cit_site": ".cht_cit_site",
    "bg_cit_url": ".cht_cit_siteurl",
    "bg_main_heading": "h2",
    "bg_search_box": "#sb_form_q",
    # Bing visually clips the answer to ~335px (.cht_txt_obs { overflow:
    # hidden; max-height: 335px }) even though the full markdown is
    # already in the DOM -- that's why the scraped text/claims are
    # already complete without clicking anything, but a screenshot taken
    # before this "Show more" control is clicked only captures the
    # truncated box. Scoped via its wrapper class rather than the
    # button's (locale-dependent) "Show more" text.
    "bg_show_more": ".b_acf_expansion_control button[aria-expanded='false']",
    # Same two-variant split for the disclaimer tooltip: the newer
    # wrapper exposes it via aria-label="AI disclaimer", the older one
    # via a .gs_aidisc trigger holding a .gs_infobbl tooltip.
    "bg_disclaimer": "[aria-label='AI disclaimer'] [role='tooltip'], .gs_aidisc .gs_infobbl",

    # DuckDuckGo's "DuckAssist" answer. Structurally much flatter than
    # either Google's or Bing's: there's no per-sentence/per-claim inline
    # citation at all -- just one answer (one or more <p> paragraphs) and
    # ONE shared list of source links after it, with no markup tying any
    # specific source to any specific sentence. See parse_duckduckgo_
    # answer() below for how that's represented. React's CSS-module class
    # names here (the random-looking strings) are expected to change
    # between DuckDuckGo deploys, so these selectors deliberately anchor
    # on `data-testid` (built for automated targeting, far more stable)
    # and plain element types instead.
    "dk_container": "[data-testid='duckassist-answer-content']",
    "dk_paragraphs": "p",
    "dk_main_strong": "[data-streamdown='strong']",
    "dk_source_links": "a[href]",
    "dk_disclaimer": "footer:has([data-testid='feedback-prompt']) p",
}

SELECTOR_META = {
    "ao_container": ("Container", "Main AI Overview container element"),
    "ao_show_more": ("Show more button", "Button to expand the full AI Overview"),
    "ao_failed_elements": ("Failed elements", "Elements indicating the AI Overview failed to load"),
    "ao_inner_text": ("Inner text", "Element containing the AI Overview's own text (scopes everything else)"),
    "ao_source_badge": ("Source badge", "The small clickable source pill next to a claim"),
    "ao_main_claim": ("Main answer", "The single highlighted summary claim at the top"),
    "ao_claim_source_box": ("Claim source box", "Source box in the pop-up that opens when hovering a source badge"),
    "ao_show_more_urls": ("Show more URLs button", "Button to expand the full source list"),
    "ao_url_divs": ("Sources (expanded box)", "List items for sources after clicking 'show more'"),
    "ao_url_divs_not_expandable": ("Sources (not expandable)", "List items for sources with no 'show more' button"),
    "ao_url_divs_carousel": ("Sources (carousel)", "List items for sources shown as a horizontal carousel"),
    "ao_url_title": ("Source title", "Element containing a source's title"),
    "ao_url_description": ("Source description", "Element containing a source's description/snippet"),
    "ao_url_description_fallback": ("Source description (fallback)", "Fallback element for the description"),
    "ao_disclaimer": ("Disclaimer", "Element with the 'AI responses may include mistakes' text"),
    "ao_prompt_input": ("Prompt input (cosmetic)", "The 'Ask anything' follow-up box, stripped before saving"),
}

BG_SELECTOR_META = {
    "bg_container": ("Container", "Main Bing Copilot Search answer container"),
    "bg_mdroot": ("Answer root", "Element holding the rendered answer markdown (headings/lists/paragraphs)"),
    "bg_claim_items": ("Claim items", "The list items / paragraphs that count as one claim each"),
    "bg_citlink": ("Citation link", "Inline citation link inside a claim, pointing at one reference"),
    "bg_cit_root": ("References root", "Container for the References list at the bottom of the answer"),
    "bg_cit_item": ("Reference item", "One entry in the References list"),
    "bg_cit_title": ("Reference title", "Element with a reference's page title"),
    "bg_cit_site": ("Reference site", "Element with a reference's site name"),
    "bg_cit_url": ("Reference URL", "Element with a reference's plain display URL"),
    "bg_main_heading": ("Main answer heading", "The heading level that wraps the top-line summary answer"),
    "bg_search_box": ("Homepage search box", "The query input on bing.com's homepage, used to type the search in rather than linking straight to a results URL"),
    "bg_show_more": ("Show more button", "Button to expand the visually-clipped answer box, used before saving a screenshot"),
    "bg_disclaimer": ("Disclaimer", "The 'This summary was generated by AI...' tooltip text"),
}

DK_SELECTOR_META = {
    "dk_container": ("Container", "Main DuckAssist answer container"),
    "dk_paragraphs": ("Paragraphs", "The <p> elements making up the answer text"),
    "dk_main_strong": ("Main answer (bold lead-in)", "The bolded lead-in phrase used as the main answer"),
    "dk_source_links": ("Source links", "Links in the shared source list below the answer"),
    "dk_disclaimer": ("Disclaimer", "Element with the 'Auto-generated...' disclaimer text"),
}


def load_selectors(log=print) -> dict:
    """Merge the bundled DEFAULT_SELECTORS with css-selectors.json next to
    this script, if it exists -- so selectors can be tweaked there without
    touching the GUI or the Python code."""
    selectors = dict(DEFAULT_SELECTORS)
    if os.path.isfile(LOCAL_SELECTORS_PATH):
        try:
            with open(LOCAL_SELECTORS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            selectors.update({k: v for k, v in data.items() if not k.startswith("_")})
            log("Loaded css-selectors.json")
        except Exception as e:
            log(f"Could not read css-selectors.json ({e}); using built-in defaults.")
    return selectors


def get_default_output_dir(input_file=False):
    user_home = os.path.expanduser("~")
    base_dir = os.path.join(user_home, "Documents", "SearchEnginesAIOverviewer")
    os.makedirs(base_dir, exist_ok=True)
    if input_file:
        return os.path.join(base_dir, "query_file.csv")
    # Second-granularity (not just minute) so two tabs/instances started
    # moments apart don't land on the same folder and fight over the same
    # results file -- a likely cause of "Permission denied" on the CSV if
    # more than one tab/run was active at once.
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    default_path = os.path.join(base_dir, "scrapes", f"scrape_{timestamp}")
    os.makedirs(default_path, exist_ok=True)
    return default_path


class GoogleAIOverviewScraper:
    """Drives Firefox to collect Google AI Overview answers, claims and sources."""

    def __init__(
        self,
        base_url=None,
        profile="",
        iterate_queries=None,
        inserted_queries=None,
        query_file=None,
        results_file="",
        output_dir="",
        offset=0,
        shuffle_queries=False,
        save_html=True,
        save_screenshots=True,
        save_sources_html=False,
        follow_goto_links=True,
        throttled_strings=None,
        selectors=None,
        scrape_google=True,
        scrape_bing=False,
        scrape_duckduckgo=False,
        progress_callback=None,
        log_callback=None,
    ):
        self.base_url = base_url or "https://www.google.com/"
        self.bing_base_url = "https://www.bing.com/"
        self.duckduckgo_base_url = "https://duckduckgo.com/"
        self.scrape_google = scrape_google
        self.scrape_bing = scrape_bing
        self.scrape_duckduckgo = scrape_duckduckgo
        self.driver = None
        self.wait = None
        self.iterate_queries = iterate_queries or []
        self.inserted_queries = inserted_queries
        self.query_file = query_file
        self.results_file = results_file
        self.output_dir = output_dir
        self.offset = offset
        self.shuffle_queries = shuffle_queries
        self.save_html_enabled = save_html
        self.save_screenshots_enabled = save_screenshots
        self.save_sources_enabled = save_sources_html
        self.follow_goto_links = follow_goto_links
        self.throttled_strings = (
            [t.strip() for t in throttled_strings.split(",")]
            if throttled_strings
            else ["Try again later", "Something went wrong"]
        )
        self.selectors = {**DEFAULT_SELECTORS, **(selectors or {})}
        self.progress_callback = progress_callback
        self.log_callback: Optional[Callable] = log_callback
        self.profile = profile
        self.is_running = False

        # Tracked per engine -- the same query can be (re)scraped for
        # Google, Bing and/or DuckDuckGo independently, and a resumed run
        # needs to know which engine(s) a given query id already has a
        # result for, not just whether the query itself was touched at all.
        self.finished_google_queries = set()
        self.finished_bing_queries = set()
        self.finished_duckduckgo_queries = set()
        self.source_urls_seen = set()
        self.sources_fetched = set()
        self.resolved_goto_links = {}
        self.goto_notice_logged = False
        self.http_session = requests.Session()
        self.http_session.headers["User-Agent"] = USER_AGENT
        self.query_extra_data = {}

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def log(self, msg, classes=None):
        if self.log_callback and self.log_callback is not print:
            self.log_callback(f"> {msg}", classes=classes)
        else:
            print(msg)

    def debug(self, msg):
        """Write one timestamped line to this run's debug log file (set up
        in prepare(), one fresh file per run under <output_dir>/debug_logs/
        so separate runs never mix together). Much more granular than the
        summary lines log() sends to the GUI -- covers every navigation,
        every cookie-banner/CAPTCHA check, which Bing container variant
        actually matched (or didn't), poll attempt counts, and the live
        navigator.webdriver/User-Agent values the page itself sees -- so a
        run where the AI answer appeared and one where it didn't can be
        diffed side by side to see exactly where they parted ways. Never
        raises; a logging failure shouldn't take down a scrape.
        """
        if not getattr(self, "debug_log_path", None):
            return
        try:
            with open(self.debug_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().isoformat()}] {msg}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Browser setup
    # ------------------------------------------------------------------
    def get_driver(self, custom_profile=""):
        options = webdriver.FirefoxOptions()
        # Deliberately NOT overriding the User-Agent here (this used to
        # force USER_AGENT, a hardcoded "Firefox/120.0" string that's
        # frozen at whatever version it was written against). That's a
        # bot-detection tell on its own: the UA header would claim an old,
        # specific Firefox build while the actual engine driving the page
        # -- TLS handshake, JS engine behavior, navigator.buildID, HTTP/2
        # characteristics -- is whatever Firefox version is really
        # installed, which drifts newer over time as Firefox auto-updates.
        # A mismatch between "what the UA claims" and "what the browser
        # actually behaves like" is exactly the kind of signal a site can
        # key off of for an expensive, selectively-served feature (Bing's
        # AI answer), even with a real, cookie-laden, logged-in profile.
        # Leaving the UA alone means it always matches the real browser.

        # navigator.webdriver is the single biggest automation tell, and
        # it's Firefox's own preference -- not page JS -- that decides
        # whether it's exposed. geckodriver turns it on by default; turning
        # it back off here applies at the browser level, so unlike the
        # execute_script() stealth injection below (which only ever
        # touched the blank startup page and was wiped the moment the
        # browser navigated anywhere), this actually survives every page
        # load, including the real google.com/bing.com search pages where
        # it matters. This is very likely why the AI answer never showed
        # up for the automated run even though the same search worked
        # fine done by hand.
        options.set_preference("dom.webdriver.enabled", False)

        if custom_profile:
            self.log(f"Using custom Firefox profile: {custom_profile}")
            options.add_argument(f"--profile={custom_profile}")

        driver = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                driver = webdriver.Firefox(options=options)
                break
            except WebDriverException as e:
                msg = str(e).lower()
                if "process unexpectedly closed with status 0" in msg and attempt < max_retries - 1:
                    wait_time = 10 * (attempt + 1)
                    self.log(
                        f"Firefox seems to be updating. Waiting {wait_time}s to retry "
                        f"({attempt + 1}/{max_retries})...",
                        classes="text-orange",
                    )
                    time.sleep(wait_time)
                    continue
                if "binary location" in msg or "not found" in msg or "firefox" in msg:
                    raise Exception(
                        "Firefox (and/or geckodriver) could not be found. Install Firefox and make sure "
                        "geckodriver.exe is on your PATH."
                    )
                raise Exception(f"Failed to start browser: {e}")

        time.sleep(1)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(120)
        driver.implicitly_wait(0.1)

        # Best-effort extra anti-detection: hides plugins/mimeTypes/WebGL
        # vendor strings, etc. (see stealthify.js next to this script).
        # NOTE: this only runs on this blank startup page -- Selenium has
        # no cross-browser equivalent of Chrome DevTools' "run this script
        # on every new document" for Firefox, so none of it (nor the
        # navigator.webdriver line below) actually reaches google.com or
        # bing.com once open_page() navigates there. It's kept as a no-cost
        # extra layer in case a future Selenium/geckodriver version adds
        # that capability; the dom.webdriver.enabled preference above is
        # what actually does the job today.
        stealth_path = os.path.join(BASE_PATH, "stealthify.js")
        if os.path.exists(stealth_path):
            with open(stealth_path, "r", encoding="utf-8") as f:
                driver.execute_script(f.read())
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return driver

    def restart_driver(self):
        self.log("Restarting browser...", classes="text-orange")
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = self.get_driver(custom_profile=self.profile)
        self.wait = WebDriverWait(self.driver, timeout=2)
        self.log("Browser restarted.", classes="text-orange")

    # ------------------------------------------------------------------
    # Prepare / queries
    # ------------------------------------------------------------------
    def prepare(self):
        if not self.scrape_google and not self.scrape_bing and not self.scrape_duckduckgo:
            self.log("Select at least one search engine to scrape (Google, Bing and/or DuckDuckGo).", classes="text-red")
            return False

        n_urls = 0
        self.log("Reading queries...")
        # A list, not a set: order needs to be controllable (see the
        # shuffle step below) and a set's iteration order is arbitrary.
        # Dedup is still handled explicitly via seen_ids.
        self.iterate_queries = []
        seen_ids = set()

        if self.query_file and os.path.isfile(self.query_file):
            with open(self.query_file, "r", encoding="utf-8") as in_csv:
                reader = csv.DictReader(in_csv)
                for row in reader:
                    n_urls += 1
                    if self.offset and n_urls < self.offset:
                        continue
                    query = row.get("query", list(row.values())[0])
                    query_id = row.get("id", hashlib.md5(query.encode()).hexdigest())
                    if query_id not in seen_ids:
                        seen_ids.add(query_id)
                        self.iterate_queries.append((query_id, query))
                    extra = {k: v for k, v in row.items() if k not in ("query", "id")}
                    if extra:
                        self.query_extra_data[query_id] = extra
        elif self.inserted_queries:
            for line in self.inserted_queries.split("\n"):
                n_urls += 1
                if self.offset and n_urls < self.offset:
                    continue
                query = line.strip()
                if not query:
                    continue
                query_id = hashlib.md5(query.encode()).hexdigest()
                if query_id not in seen_ids:
                    seen_ids.add(query_id)
                    self.iterate_queries.append((query_id, query))

        if self.shuffle_queries:
            random.shuffle(self.iterate_queries)

        if not self.iterate_queries:
            self.log("No queries found. Add some queries and try again.", classes="text-red")
            return False

        self.log(f"Found {len(self.iterate_queries)} unique queries.", classes="text-blue")

        self.log("Launching Firefox. Please wait...", classes="text-orange")
        try:
            self.driver = self.get_driver(custom_profile=self.profile)
            self.wait = WebDriverWait(self.driver, timeout=2)

            if not os.path.isdir(self.output_dir):
                os.makedirs(self.output_dir)
            self.results_file = os.path.join(self.output_dir, "ai_overview_results.jsonl")

            debug_dir = os.path.join(self.output_dir, "debug_logs")
            os.makedirs(debug_dir, exist_ok=True)
            self.debug_log_path = os.path.join(debug_dir, f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
            self.log(f"Detailed debug log for this run: {self.debug_log_path}", classes="text-blue")
            try:
                real_ua = self.driver.execute_script("return navigator.userAgent")
                real_webdriver = self.driver.execute_script("return navigator.webdriver")
                self.debug(f"Browser launched. navigator.userAgent={real_ua!r} navigator.webdriver={real_webdriver!r}")
            except Exception as e:
                self.debug(f"Could not read navigator.userAgent/webdriver after launch: {e}")

            # Warm up on Google only, even when Bing is also/only selected.
            # (This used to also warm up on Bing so a Bing-only run got a
            # chance to pre-solve a Bing CAPTCHA before "Start scraping" --
            # reverted, because a fresh browser visiting google.com and
            # bing.com back-to-back within seconds of launching, with no
            # real browsing history or human interaction in between, is
            # close to a textbook bot fingerprint and was actually
            # *causing* Bing to serve a CAPTCHA it wasn't serving before.
            # Bing still gets its own check_for_captcha() the first time
            # open_page() visits it for real, during actual scraping --
            # just later in the session, once it looks like an organic
            # visit rather than a cold-start double-visit.)
            self.driver.get(f"{self.base_url}search?q=is+scraping+legal")
            self.dismiss_cookie_consent()
            self.check_for_captcha()

            if os.path.isfile(self.results_file):
                self.log("Found an existing results file -- will skip already-scraped queries.", classes="text-orange")
                with open(self.results_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                            # "engine" is a newer field -- older results files
                            # (from before Bing/DuckDuckGo support) won't
                            # have it, and every row in one of those is
                            # necessarily Google.
                            engine = row.get("engine", "google")
                            if engine == "bing":
                                self.finished_bing_queries.add(row["id"])
                            elif engine == "duckduckgo":
                                self.finished_duckduckgo_queries.add(row["id"])
                            else:
                                self.finished_google_queries.add(row["id"])
                        except Exception as e:
                            self.log(f"Warning: could not parse a results line: {e}", classes="text-orange")
                self.log(
                    f"Already collected {len(self.finished_google_queries)} Google, "
                    f"{len(self.finished_bing_queries)} Bing and "
                    f"{len(self.finished_duckduckgo_queries)} DuckDuckGo results previously.",
                    classes="text-orange",
                )

            self.log("Browser ready.", classes="text-blue")
            self.log(
                "Solve any CAPTCHA/consent screen in the Firefox window, then click 'Start scraping'.",
                classes="text-orange",
            )
            return True
        except Exception as e:
            self.log(f"ERROR: {e}", classes="text-red")
            if "Firefox" not in str(e):
                import traceback

                self.log(traceback.format_exc())
            return False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def process(self):
        self.is_running = True
        try:
            count = 0
            total = len(self.iterate_queries)
            for page_id, query in self.iterate_queries:
                count += 1
                if not self.is_running:
                    self.log("Stopped by user.", classes="text-red")
                    break
                if self.progress_callback:
                    self.progress_callback(count, total)

                self.log(f"[{count}/{total}] {query}")
                start = time.time()

                # Google, then Bing, then DuckDuckGo, always -- when more
                # than one is selected, each query's result is scraped and
                # written for one engine fully before the next engine is
                # even attempted for that same query.
                if self.scrape_google:
                    self._scrape_one_engine("google", page_id, query)
                if self.scrape_bing:
                    self._scrape_one_engine("bing", page_id, query)
                if self.scrape_duckduckgo:
                    self._scrape_one_engine("duckduckgo", page_id, query)

                self.print_elapsed(start)

            if self.is_running and os.path.isfile(self.results_file):
                self.convert_to_csv()
                self.log("Done.", classes="text-blue")
        except Exception as e:
            self.log(f"Fatal error: {e}", classes="text-red")
            import traceback

            self.log(traceback.format_exc())
        finally:
            self.stop()

    def _scrape_one_engine(self, engine, page_id, query):
        """Scrape one query for one engine ("google", "bing" or
        "duckduckgo"): navigate, parse, write the result row, and save
        HTML/screenshot/sources for it. Shared by all three engines so
        process() just calls this once per selected engine per query
        instead of duplicating this whole block -- the engines only
        differ in the URL and parse method.
        """
        finished = {
            "google": self.finished_google_queries,
            "bing": self.finished_bing_queries,
            "duckduckgo": self.finished_duckduckgo_queries,
        }[engine]
        if page_id in finished:
            self.log(f"  [{engine}] Already scraped, skipping.", classes="text-orange")
            return

        try:
            if engine == "google":
                query_url = f"{self.base_url}search?q={self.get_url_string(query)}"
                self.open_page(query_url)
                result = self.parse_ai_overview(page_id, query)
            elif engine == "bing":
                self.search_bing(query)
                result = self.parse_bing_answer(page_id, query)
            else:
                # Plain search URL, same as a normal DuckDuckGo search --
                # DuckAssist appears inline above the organic results on
                # its own; no special "ia=chat" param needed (that one
                # routes to the separate full-page Duck.ai chat UI, not
                # this inline box).
                query_url = f"{self.duckduckgo_base_url}?q={self.get_url_string(query)}"
                self.open_page(query_url)
                result = self.parse_duckduckgo_answer(page_id, query)
        except Exception as e:
            self.log(f"  [{engine}] Error scraping '{query}': {e}", classes="text-red")
            import traceback

            self.log(traceback.format_exc())
            result = None

        if result:
            for k, v in self.query_extra_data.get(page_id, {}).items():
                result.setdefault(k, v)
            with open(self.results_file, "a", encoding="utf-8") as out:
                out.write(json.dumps(result) + "\n")
            self.log(
                f"  [{engine}] -> {len(result.get('claims', []))} claims, {len(result.get('sources', []))} sources",
                classes="text-blue",
            )
        else:
            self.log(f"  [{engine}] -> No answer found for this query.", classes="text-orange")

        if self.save_html_enabled:
            self.save_html(query, page_id, engine)
        if self.save_screenshots_enabled:
            self.save_screenshot(query, page_id, engine)
        if self.save_sources_enabled:
            self.save_all_sources(result)

        finished.add(page_id)

    def stop(self):
        self.is_running = False
        if self.driver:
            try:
                self.driver.quit()
                self.log("Firefox closed.", classes="text-blue")
            except Exception:
                pass
            self.driver = None

    # ------------------------------------------------------------------
    # Page navigation / consent / captcha
    # ------------------------------------------------------------------
    def open_page(self, url):
        retries = 0
        max_retries = 3
        while retries <= max_retries:
            try:
                self.debug(f"open_page: navigating to {url}")
                self.driver.get(url)
                self.debug(f"open_page: landed on {self.driver.current_url}")
                self.dismiss_cookie_consent()
                self.check_for_captcha()
                return
            except TimeoutException:
                retries += 1
                self.log(f"Page timed out, retrying in 10s ({retries}/{max_retries})...", classes="text-orange")
                time.sleep(10)
                try:
                    self.driver.refresh()
                except WebDriverException:
                    self.restart_driver()
            except WebDriverException as e:
                if "marionette" in str(e).lower() or "decode" in str(e).lower():
                    retries += 1
                    self.log(f"Lost connection to browser, restarting ({retries}/{max_retries})...", classes="text-orange")
                    self.restart_driver()
                else:
                    raise
        raise Exception("Couldn't load page after max retries")

    def wait_for_page_ready(self, timeout=10):
        """Poll document.readyState instead of a flat sleep -- a real
        person waits for the page to actually finish loading before doing
        anything on it (clicking a cookie banner included), rather than
        reacting to it on a fixed clock the instant a script says so.
        Doesn't care whether anything in particular rendered (an AI
        answer, say) -- just whether the page itself is done loading.
        """
        start = time.time()
        end = start + timeout
        while time.time() < end:
            try:
                if self.driver.execute_script("return document.readyState") == "complete":
                    self.debug(f"wait_for_page_ready: complete after {time.time() - start:.2f}s")
                    return
            except Exception:
                pass
            time.sleep(0.2)
        self.debug(f"wait_for_page_ready: timed out after {timeout}s, readyState never reached 'complete'")

    def search_bing(self, query):
        """Go to bing.com's homepage and type the query into the real
        search box, the way someone using Bing actually would, instead of
        jumping straight to a constructed bing.com/search?q=... URL.

        This doesn't touch anything selector-driven about the *answer
        itself* -- parse_bing_answer() is unchanged -- it only changes how
        the query gets there, on the theory that building search URLs
        directly (never visiting the homepage, never using the search
        box, no keystroke events for Bing's own JS to see) is itself a
        pattern a bot-detection system can key off of, on top of
        everything else already tried (profile, UA). Falls back to the
        old direct-URL approach if the search box can't be found, e.g.
        because Bing changed its homepage markup -- bg_search_box is its
        own selector in the CSS selectors panel for exactly that reason.

        Homepage navigation still goes through open_page() as before
        (consent dialog + CAPTCHA check right after load, unchanged). The
        results page is different: it waits for the page to actually
        finish loading before touching the cookie banner, rather than
        reacting on a fixed clock the instant the Enter keypress is sent
        -- closer to how a person actually uses a page.
        """
        sel = self.selectors
        self.debug(f"search_bing: query={query!r}")
        self.open_page(self.bing_base_url)

        try:
            search_box = self.driver.find_element(By.CSS_SELECTOR, sel["bg_search_box"])
            self.debug("search_bing: found homepage search box, typing query")
            search_box.click()
            search_box.clear()
            search_box.send_keys(query)
            time.sleep(0.3)
            search_box.send_keys(Keys.ENTER)
            self.debug("search_bing: submitted, waiting for results page to load")
            self.wait_for_page_ready()
            self.debug(f"search_bing: results page url={self.driver.current_url}")
            self.dismiss_cookie_consent()
            self.check_for_captcha()
        except (NoSuchElementException, ElementNotInteractableException) as e:
            self.debug(f"search_bing: homepage search box not found/interactable ({e}); falling back to direct URL")
            self.log(
                f"Couldn't find Bing's homepage search box ({e}); falling back to a direct search URL.",
                classes="text-orange",
            )
            query_url = f"{self.bing_base_url}search?q={self.get_url_string(query)}"
            self.open_page(query_url)

    def dismiss_cookie_consent(self):
        """Best-effort click on a cookie/consent dialog, if one appears.

        Doesn't rely on an external extension -- just tries a handful of
        commonly-used button ids/text, covering both Google's own consent
        interstitial and the generic "OneTrust"-style banner Bing/Microsoft
        sites commonly use. Silently does nothing if no dialog is present.

        Polls for a couple of seconds rather than checking once: the
        banner is injected by its own JS slightly after the rest of the
        page, so an immediate single check can run before it exists yet
        and miss it entirely -- which is what let it sit there, uncleared,
        through to a saved screenshot (the banner itself never blocks the
        *scrape*, since that reads the answer's own DOM elements directly,
        regardless of what's visually on top of them).
        """
        candidates = [
            "#L2AGLb",  # "Accept all" button id Google has used for a long time
            "button[aria-label='Accept all']",
            "button[aria-label='Reject all']",
            "div[aria-label='Accept all']",
            "#onetrust-accept-btn-handler",  # common OneTrust consent banner (Bing/Microsoft sites)
            "#bnp_btn_accept",  # Bing's own cookie-notice button id
        ]
        for attempt in range(5):
            for sel in candidates:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in elements:
                        if el.is_displayed():
                            el.click()
                            self.debug(f"dismiss_cookie_consent: clicked {sel!r} on attempt {attempt + 1}")
                            time.sleep(0.5)
                            return
                except Exception:
                    continue
            time.sleep(0.4)
        self.debug("dismiss_cookie_consent: no banner found/displayed after 5 attempts")

    def check_for_captcha(self):
        """Detect a few known CAPTCHA/bot-check patterns and wait for a
        human to solve them in the Firefox window. Covers Google's own
        reCAPTCHA as well as the generic providers (Cloudflare, hCaptcha,
        FunCaptcha) that Bing and plenty of other sites also use.
        """
        selectors = [
            "#recaptcha",
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='hcaptcha.com']",
            "iframe[src*='arkoselabs.com']",
        ]
        for sel in selectors:
            captcha = self.driver.find_elements(By.CSS_SELECTOR, sel)
            if captcha and captcha[0].is_displayed():
                self.debug(f"check_for_captcha: detected via {sel!r} on {self.driver.current_url}")
                self.log("CAPTCHA detected -- please solve it in the Firefox window...", classes="text-orange")
                wait_start = time.time()
                while captcha and captcha[0].is_displayed():
                    time.sleep(2)
                    captcha = self.driver.find_elements(By.CSS_SELECTOR, sel)
                self.debug(f"check_for_captcha: cleared after {time.time() - wait_start:.1f}s")
                return
        self.debug(f"check_for_captcha: none detected on {self.driver.current_url}")

    # ------------------------------------------------------------------
    # Core parsing
    # ------------------------------------------------------------------
    def parse_ai_overview(self, page_id, query) -> Optional[dict]:
        sel = self.selectors
        time.sleep(1)

        container = self.driver.find_elements(By.CSS_SELECTOR, sel["ao_container"])
        if not container or not container[0].is_displayed():
            return None
        container = container[0]

        result = {
            "id": page_id,
            "query": query,
            "engine": "google",
            "from_url": self.driver.current_url,
            "timestamp_scraped": datetime.now().isoformat(),
            "not_available": False,
            "text": "",
            "main_answer": "",
            "claims": [],
            "sources": [],
            "disclaimer": "",
        }

        failed_elements = container.find_elements(By.CSS_SELECTOR, sel["ao_failed_elements"])
        inner_text_el = container.find_elements(By.CSS_SELECTOR, sel["ao_inner_text"])
        throttled = None
        if inner_text_el:
            inner_html = inner_text_el[0].get_attribute("innerHTML") or ""
            for t in self.throttled_strings:
                if t in inner_html:
                    throttled = t
                    break

        show_more = self.driver.find_elements(By.CSS_SELECTOR, sel["ao_show_more"])
        if not show_more and (failed_elements or throttled):
            result["text"] = container.text
            result["not_available"] = True
            if throttled:
                self.log(f"Throttled ('{throttled}'), waiting 10 minutes before retrying...", classes="text-orange")
                time.sleep(600)
                self.driver.refresh()
                self.check_for_captcha()
                return self.parse_ai_overview(page_id, query)
            return result

        # Expand the full answer
        for attempt in range(3):
            try:
                if show_more:
                    show_more[0].click()
                break
            except (StaleElementReferenceException, ElementNotInteractableException,
                    NoSuchElementException, ElementClickInterceptedException, IndexError):
                show_more = self.driver.find_elements(By.CSS_SELECTOR, sel["ao_show_more"])
                time.sleep(1)

        # Wait for the inner text container to be present (retry a few times)
        inner_text_el = None
        for _ in range(6):
            inner_text_el = container.find_elements(By.CSS_SELECTOR, sel["ao_inner_text"])
            if inner_text_el:
                break
            time.sleep(0.5)
        if not inner_text_el:
            self.log("AI Overview content never appeared, skipping.", classes="text-orange")
            return None
        inner_text_el = inner_text_el[0]

        # Strip the "ask anything" follow-up input box before reading text (cosmetic)
        prompt_input = self.driver.find_elements(By.CSS_SELECTOR, sel["ao_prompt_input"])
        if prompt_input:
            self.driver.execute_script(
                "arguments[0].parentNode && arguments[0].parentNode.removeChild(arguments[0]);",
                prompt_input[0],
            )

        answer_html = self.html_without_source_buttons(inner_text_el)
        result["text"] = md(answer_html, strip=["a", "img", "button"]).strip()

        main_answer = self.driver.find_elements(By.CSS_SELECTOR, sel["ao_main_claim"])
        result["main_answer"] = main_answer[0].text if main_answer else ""

        # Sources (full bottom list)
        urls, url_divs = [], []
        show_more_urls_btn = container.find_elements(By.CSS_SELECTOR, sel["ao_show_more_urls"])
        if show_more_urls_btn:
            try:
                self.wait.until(lambda d: any(e.is_displayed() for e in d.find_elements(By.CSS_SELECTOR, sel["ao_show_more_urls"])))
                show_more_urls_btn[0].click()
                url_divs = self.driver.find_elements(By.CSS_SELECTOR, sel["ao_url_divs"])
            except (TimeoutException, ElementNotInteractableException):
                url_divs = self.driver.find_elements(By.CSS_SELECTOR, sel["ao_url_divs_not_expandable"])
        else:
            carousel_divs = container.find_elements(By.CSS_SELECTOR, sel["ao_url_divs_carousel"])
            url_divs = carousel_divs or self.driver.find_elements(By.CSS_SELECTOR, sel["ao_url_divs_not_expandable"])

        for url_div in url_divs:
            if not url_div.is_displayed():
                continue
            try:
                title_el = url_div.find_elements(By.CSS_SELECTOR, sel["ao_url_title"] + " > span")
                title = title_el[0].text if title_el else url_div.text.split("\n")[0]
                a_el = url_div.find_element(By.CSS_SELECTOR, "a")
                href = a_el.get_attribute("href")
                desc_el = url_div.find_elements(By.CSS_SELECTOR, sel["ao_url_description"])
                desc_el += url_div.find_elements(By.CSS_SELECTOR, sel["ao_url_description_fallback"])
                description = desc_el[0].text if desc_el else ""
                url = {
                    "title": title,
                    "description": description,
                    "url_id": hashlib.md5((href or "").encode()).hexdigest(),
                    **self.source_link_fields(href),
                }
                urls.append(url)
                self.source_urls_seen.add(url.get("translated_url") or url["url"])
            except Exception:
                continue
        result["sources"] = urls

        # Claims (the whole point of this tool)
        result["claims"] = self.get_claims(inner_text_el)

        disclaimers = self.driver.find_elements(By.CSS_SELECTOR, sel["ao_disclaimer"])
        for d in disclaimers:
            if d.is_displayed():
                result["disclaimer"] = d.text
                break

        self.driver.execute_script("window.scrollTo(0, 0);")
        try:
            self.driver.execute_script("document.elementFromPoint(0, 0).click();")
        except Exception:
            pass
        return result

    def get_claims(self, ai_overview_root) -> list:
        """Hover each source badge and read off ITS OWN claim via DOM ancestry.

        This is the important part. Earlier attempts at this kind of
        scraper tried to find "the currently highlighted claim" via a
        CSS selector (various classes/attributes Google toggles on
        hover). That turned out to be unreliable: Google does not mark
        the hovered claim distinctly in a way that survives across
        Google's frequent markup changes, and in some cases a hover
        triggers a whole-answer-wide DOM change rather than a
        claim-specific one.

        Instead, we only trust ONE thing: that `link_badge` (a Selenium
        element handle on the exact badge we just hovered) is correct.
        So we walk UP the DOM from that exact element to find its own
        enclosing claim block. This is correct by construction,
        regardless of whatever else Google's markup does elsewhere on
        the page.

        The claim "container" to climb to is the enclosing `<li>` for a
        bulleted claim, since that's the actual boundary of one claim
        regardless of how Google nests `data-hveid` wrappers inside it
        (the badge's immediate parent sometimes carries its own, narrower
        `data-hveid`, e.g. on the citation button itself -- climbing to
        just the first `data-hveid` ancestor can stop there instead of
        reaching the whole bullet). For the lead-in sentence, which has
        no `<li>` wrapper at all, we fall back to the nearest ancestor
        carrying a `data-hveid` attribute, starting from the badge's
        *parent* (the badge itself also carries `data-hveid`, so
        starting from the badge would just match itself).

        Once that container is found, its text is read via
        `textContent` rather than by walking leaf elements: Google is
        inconsistent about whether the sentence after a bullet's bold
        lead-in phrase gets its own wrapper element or is left as a bare
        text node, and both forms appear within the same answer. Reading
        only leaf elements misses bare text nodes outright, silently
        truncating the claim to just its heading whenever Google omits
        the wrapper -- `textContent` picks up both forms uniformly. The
        citation badge/button is stripped from a clone first so its own
        link labels (e.g. "National Geographic +2") don't leak into the
        claim text.
        """
        sel = self.selectors
        claims = []
        actions = ActionChains(self.driver)

        try:
            all_badges = [
                b for b in ai_overview_root.find_elements(By.CSS_SELECTOR, sel["ao_source_badge"])
                if b.is_displayed()
            ]

            # Reverse order: scrolling a badge to the top of the viewport
            # dismisses whatever popup is currently open, and processing
            # last-to-first means we never obscure the next badge.
            for link_badge in reversed(all_badges):
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'start'});", link_badge)
                    self.driver.execute_script("window.scrollBy(0, -200);")
                    time.sleep(0.2)
                    actions.move_to_element(link_badge).perform()
                    time.sleep(0.4)

                    claim_text = self.driver.execute_script(
                        """
                        const badge = arguments[0];
                        const scopeRoot = arguments[1];

                        // Prefer the enclosing <li> if there is one -- that's
                        // the actual boundary of a bulleted claim. Google
                        // sometimes nests a SECOND, narrower data-hveid
                        // wrapper inside the <li> that covers only the bold
                        // lead-in phrase before the colon (e.g. "Rising
                        // Temperatures:"), separate from the wrapper around
                        // the rest of the sentence. Climbing to just the
                        // first data-hveid ancestor can land on that inner
                        // wrapper instead of the whole bullet, cutting the
                        // claim short and losing its sources (the actual
                        // citation badge/popup is scoped to the sentence
                        // that follows, not the heading alone). Bounded to
                        // scopeRoot so this can't accidentally climb out of
                        // the AI Overview box into some unrelated page <li>.
                        let root = badge.closest('li');
                        if (root && !scopeRoot.contains(root)) {
                            root = null;
                        }

                        if (!root) {
                            // No enclosing <li> -- this is the lead-in
                            // sentence, which has no <li> wrapper at all.
                            // Fall back to the nearest ancestor carrying
                            // Google's own per-segment data-hveid id,
                            // starting from the badge's PARENT (the badge
                            // itself also has a data-hveid and would
                            // otherwise match first).
                            root = badge.parentElement;
                            while (root && !root.hasAttribute('data-hveid')) {
                                root = root.parentElement;
                            }
                        }
                        if (!root) return '';
                        // Google is inconsistent about whether the text
                        // after the bold lead-in phrase gets its own
                        // wrapper element or is left as a bare text node
                        // (e.g. "<strong>Rising Temperatures:</strong> The
                        // global average..." with no wrapper, vs.
                        // "<strong>Melting Ice...:</strong><div>Glaciers
                        // and...</div>" with one) -- both appear in the
                        // SAME answer. Walking only leaf ELEMENTS misses
                        // bare text nodes entirely, truncating the claim to
                        // just its heading whenever Google omits the
                        // wrapper. Clone the subtree, strip out the
                        // citation badge/button (so its link labels like
                        // "National Geographic +2" don't leak into the
                        // claim text), and read textContent -- that picks
                        // up every text node, wrapped or not.
                        const clone = root.cloneNode(true);
                        clone.querySelectorAll('button').forEach(b => b.remove());
                        return clone.textContent.replace(/\\s+/g, ' ').trim();
                        """,
                        link_badge,
                        ai_overview_root,
                    )

                    claim_sources = []
                    for box in self.driver.find_elements(By.CSS_SELECTOR, sel["ao_claim_source_box"]):
                        if not box.is_displayed():
                            continue
                        try:
                            title_el = box.find_elements(By.CSS_SELECTOR, sel["ao_url_title"])
                            title = title_el[0].text if title_el else ""
                            a_el = box.find_element(By.CSS_SELECTOR, "a")
                            href = a_el.get_attribute("href")
                            desc_el = box.find_elements(By.CSS_SELECTOR, sel["ao_url_description"])
                            description = desc_el[0].text if desc_el else ""
                            claim_sources.append({
                                "title": title,
                                "description": description,
                                "url_id": hashlib.md5((href or "").encode()).hexdigest(),
                                **self.source_link_fields(href),
                            })
                        except Exception:
                            continue

                    claims.append({"claim": claim_text, "sources": claim_sources})
                except Exception as e:
                    self.log(f"  (skipped one badge: {e})", classes="text-orange")
                    continue
        finally:
            dismiss = ActionChains(self.driver)
            dismiss.w3c_actions.pointer_action.move_to_location(0, 0)
            dismiss.perform()
            time.sleep(0.3)

        claims.reverse()
        return claims

    def html_without_source_buttons(self, element) -> str:
        return self.driver.execute_script(
            """
            const clone = arguments[0].cloneNode(true);
            clone.querySelectorAll('button').forEach(b => b.remove());
            return clone.outerHTML;
            """,
            element,
        )

    # ------------------------------------------------------------------
    # Bing ("Copilot Search") parsing
    # ------------------------------------------------------------------
    def parse_bing_answer(self, page_id, query) -> Optional[dict]:
        """Bing's AI answer is a much easier scrape than Google's: each
        claim's citation(s) are already inline <a class="md_citlink">
        elements sitting right inside that claim's own <li>/<p> -- no
        hovering, no popups, no walking up the DOM to find the right
        container. The only wrinkle is that the citation link's href is
        an obfuscated bing.com/ck/a redirect, not the real URL -- but
        unlike Google's /goto redirect, it doesn't need a network request
        to resolve: the real URL is base64-encoded directly in the
        link's own `u=` query parameter (see decode_bing_redirect_url()).
        """
        sel = self.selectors
        self.debug(f"parse_bing_answer: starting for query={query!r} on {self.driver.current_url}")
        try:
            page_text_snippet = (self.driver.execute_script("return document.title") or "")[:120]
            self.debug(f"parse_bing_answer: page title={page_text_snippet!r}")
        except Exception:
            pass
        time.sleep(1)

        # Poll for a few seconds rather than checking once: Copilot Search
        # streams in asynchronously after the rest of the page has already
        # loaded (same reasoning as Google's ao_inner_text retry loop in
        # parse_ai_overview() above), so a single immediate check risks
        # misreading "hasn't rendered yet" as "no answer for this query" --
        # a false negative, not a real absence.
        container = mdroot = None
        for attempt in range(6):
            container_els = self.driver.find_elements(By.CSS_SELECTOR, sel["bg_container"])
            if container_els and container_els[0].is_displayed():
                variant = container_els[0].get_attribute("class")
                mdroot_els = container_els[0].find_elements(By.CSS_SELECTOR, sel["bg_mdroot"])
                if mdroot_els:
                    container, mdroot = container_els[0], mdroot_els[0]
                    self.debug(f"parse_bing_answer: container found on attempt {attempt + 1} (class={variant!r})")
                    break
                else:
                    self.debug(f"parse_bing_answer: container matched (class={variant!r}) but no bg_mdroot inside it yet, attempt {attempt + 1}")
            time.sleep(0.5)
        if container is None or mdroot is None:
            self.debug("parse_bing_answer: no container found after 6 attempts -- no answer for this query/session")
            return None

        result = {
            "id": page_id,
            "query": query,
            "engine": "bing",
            "from_url": self.driver.current_url,
            "timestamp_scraped": datetime.now().isoformat(),
            "not_available": False,
            "text": "",
            "main_answer": "",
            "claims": [],
            "sources": [],
            "disclaimer": "",
        }

        # References list at the bottom of the answer: gives the full page
        # title and a plain display URL for each numbered citation, keyed
        # by the same number each inline <a data-sups="N"> points at.
        citation_map = {}
        cit_root = container.find_elements(By.CSS_SELECTOR, sel["bg_cit_root"])
        if cit_root:
            for cit in cit_root[0].find_elements(By.CSS_SELECTOR, sel["bg_cit_item"]):
                num = cit.get_attribute("data-citation-num")
                if not num:
                    continue
                try:
                    href = cit.find_element(By.CSS_SELECTOR, "a").get_attribute("href")
                except Exception:
                    href = None
                title_el = cit.find_elements(By.CSS_SELECTOR, sel["bg_cit_title"])
                site_el = cit.find_elements(By.CSS_SELECTOR, sel["bg_cit_site"])
                url_el = cit.find_elements(By.CSS_SELECTOR, sel["bg_cit_url"])
                citation_map[num] = {
                    "title": title_el[0].text if title_el else (site_el[0].text if site_el else ""),
                    "href": href,
                    # Plain display URL from the References list -- used as
                    # a fallback if decoding the obfuscated redirect fails.
                    "display_url": url_el[0].text if url_el else "",
                }

        answer_html = self.html_without_citation_links(mdroot)
        result["text"] = md(answer_html, strip=["a", "img"]).strip()

        headings = mdroot.find_elements(By.CSS_SELECTOR, sel["bg_main_heading"])
        result["main_answer"] = headings[0].text if headings else ""

        result["claims"] = self.get_bing_claims(mdroot, citation_map)
        # The top-level "sources" list mirrors Google's: every unique
        # reference the answer cites, regardless of which claim(s) use it.
        seen_hrefs = set()
        for claim in result["claims"]:
            for src in claim["sources"]:
                href = src.pop("_href", None)
                if href and href not in seen_hrefs:
                    seen_hrefs.add(href)
                    result["sources"].append({k: v for k, v in src.items()})

        # The disclaimer tooltip is only shown on hover/click of the little
        # (i) icon, so it's visually hidden in the DOM by default --
        # Selenium's .text returns "" for a hidden element, so this reads
        # textContent directly via JS instead of relying on is_displayed().
        disclaimer_els = container.find_elements(By.CSS_SELECTOR, sel["bg_disclaimer"])
        if disclaimer_els:
            result["disclaimer"] = (self.driver.execute_script(
                "return arguments[0].textContent;", disclaimer_els[0]
            ) or "").strip()

        # Expand the visually-clipped answer box so a saved screenshot
        # shows the full answer, not just the ~335px preview -- doesn't
        # affect result[] above, since that was already read straight out
        # of the DOM regardless of the CSS clip.
        show_more = self.driver.find_elements(By.CSS_SELECTOR, sel["bg_show_more"])
        if show_more and show_more[0].is_displayed():
            try:
                show_more[0].click()
                time.sleep(0.3)
                self.debug("parse_bing_answer: clicked show-more before screenshot")
            except Exception:
                pass
        else:
            self.debug("parse_bing_answer: no show-more button found/displayed (answer may already be short enough)")

        self.debug(
            f"parse_bing_answer: done -- {len(result['claims'])} claims, "
            f"{len(result['sources'])} sources, disclaimer={'yes' if result['disclaimer'] else 'no'}"
        )
        self.driver.execute_script("window.scrollTo(0, 0);")
        return result

    def get_bing_claims(self, mdroot, citation_map) -> list:
        """One claim per <li>/<p> inside the answer: its text (citation
        links stripped out) plus whichever reference(s) its own inline
        <a class="md_citlink" data-sups="N"> tags point at, looked up in
        `citation_map`. A claim with no citation at all (Bing does write
        plain, uncited sentences sometimes) gets an empty sources list,
        same as Google's claims do.
        """
        sel = self.selectors
        claims = []
        for item in mdroot.find_elements(By.CSS_SELECTOR, sel["bg_claim_items"]):
            if not item.is_displayed():
                continue

            citlinks = item.find_elements(By.CSS_SELECTOR, sel["bg_citlink"])
            claim_sources = []
            seen_nums = set()
            for link in citlinks:
                num = link.get_attribute("data-sups")
                if not num or num in seen_nums:
                    continue
                seen_nums.add(num)
                ref = citation_map.get(num, {})
                href = ref.get("href")
                decoded_url = self.decode_bing_redirect_url(href) if href else ""
                final_url = decoded_url or ref.get("display_url") or href or ""
                claim_sources.append({
                    "title": ref.get("title") or (link.get_attribute("aria-label") or "").removeprefix("Sources: "),
                    "description": "",
                    "url_id": hashlib.md5(final_url.encode()).hexdigest(),
                    "domain": urlparse(final_url).netloc if final_url else "",
                    "url": final_url,
                    "is_translated": False,
                    "_href": final_url,
                })

            claim_text = self.html_without_citation_links(item, return_text=True)
            if not claim_text:
                continue
            claims.append({"claim": claim_text, "sources": claim_sources})

        return claims

    def html_without_citation_links(self, element, return_text=False):
        """Return an element's outerHTML (or, with return_text, its plain
        textContent) after removing every Bing inline citation link
        (<a class="md_citlink">) from a clone -- so a citation's own site
        name (e.g. "NASA") doesn't leak into the claim/answer text,
        mirroring what html_without_source_buttons() does for Google.
        """
        sel = self.selectors
        return self.driver.execute_script(
            """
            const clone = arguments[0].cloneNode(true);
            clone.querySelectorAll(arguments[1]).forEach(a => a.remove());
            return arguments[2] ? clone.textContent.replace(/\\s+/g, ' ').trim() : clone.outerHTML;
            """,
            element,
            sel["bg_citlink"],
            return_text,
        )

    @staticmethod
    def decode_bing_redirect_url(href) -> str:
        """Decode the real URL out of a bing.com/ck/a redirect link.

        Unlike Google's /goto redirect (which needs an actual HTTP request
        to read the Location header), Bing's redirect embeds the real URL
        directly in its own `u=` query parameter, as base64 with a 2-char
        prefix -- so this can be decoded entirely offline. Falls back to
        the raw href unchanged if it isn't that kind of link, or if
        decoding fails for any reason (Bing could change this scheme).
        """
        if not href:
            return ""
        try:
            parsed = urlparse(href)
            payload = parse_qs(parsed.query).get("u", [None])[0]
            if not payload:
                return href
            stripped = payload[2:]  # drop the fixed 2-char prefix (e.g. "a1")
            padded = stripped + "=" * (-len(stripped) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8")
        except Exception:
            return href

    # ------------------------------------------------------------------
    # DuckDuckGo ("DuckAssist") parsing
    # ------------------------------------------------------------------
    def parse_duckduckgo_answer(self, page_id, query) -> Optional[dict]:
        """DuckAssist is structurally flatter than both Google's and
        Bing's: there is no per-sentence/per-claim citation markup at
        all. The answer is just one or more <p> paragraphs, followed by
        ONE shared row of source links for the whole thing -- nothing in
        the markup ties a specific source to a specific sentence the way
        Google's hover badges or Bing's inline <a data-sups> links do.

        So unlike parse_ai_overview()/parse_bing_answer(), "claims" here
        is intentionally just a single entry covering the whole answer
        text, paired with the full shared source list -- splitting it
        into one claim per paragraph would invent a false impression of
        per-paragraph sourcing that the page itself doesn't support.

        DuckDuckGo's source links are also already the real, direct
        URLs (no obfuscated redirect to decode, unlike Google's /goto or
        Bing's /ck/a).
        """
        sel = self.selectors
        time.sleep(1)

        container = None
        for _ in range(6):
            container_els = self.driver.find_elements(By.CSS_SELECTOR, sel["dk_container"])
            if container_els and container_els[0].is_displayed():
                container = container_els[0]
                break
            time.sleep(0.5)
        if container is None:
            return None

        result = {
            "id": page_id,
            "query": query,
            "engine": "duckduckgo",
            "from_url": self.driver.current_url,
            "timestamp_scraped": datetime.now().isoformat(),
            "not_available": False,
            "text": "",
            "main_answer": "",
            "claims": [],
            "sources": [],
            "disclaimer": "",
        }

        paragraphs = [p for p in container.find_elements(By.CSS_SELECTOR, sel["dk_paragraphs"]) if p.is_displayed()]
        answer_text = "\n\n".join(p.text for p in paragraphs if p.text).strip()
        result["text"] = answer_text

        main_strong = container.find_elements(By.CSS_SELECTOR, sel["dk_main_strong"])
        if main_strong:
            result["main_answer"] = main_strong[0].text
        elif paragraphs:
            # Fall back to the first paragraph's text if there's no bold
            # lead-in phrase at all (a plainer answer than usual).
            result["main_answer"] = paragraphs[0].text

        sources = []
        for a_el in container.find_elements(By.CSS_SELECTOR, sel["dk_source_links"]):
            if not a_el.is_displayed():
                continue
            href = a_el.get_attribute("href")
            if not href:
                continue
            title = a_el.text.strip()
            sources.append({
                "title": title,
                "description": "",
                "url_id": hashlib.md5(href.encode()).hexdigest(),
                "domain": urlparse(href).netloc,
                "url": href,
                "is_translated": False,
            })
        result["sources"] = sources

        if answer_text:
            result["claims"] = [{"claim": answer_text, "sources": sources}]

        disclaimers = self.driver.find_elements(By.CSS_SELECTOR, sel["dk_disclaimer"])
        for d in disclaimers:
            if d.is_displayed():
                result["disclaimer"] = d.text
                break

        self.driver.execute_script("window.scrollTo(0, 0);")
        return result

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------
    def save_html(self, query, page_id, engine="google"):
        html_dir = os.path.join(self.output_dir, "html")
        os.makedirs(html_dir, exist_ok=True)
        filename = self.get_page_str(query, page_id, engine) + ".html"
        with open(os.path.join(html_dir, filename), "w", encoding="utf-8") as f:
            f.write(self.driver.page_source)

    def save_screenshot(self, query, page_id, engine="google"):
        """Save a full-page screenshot without resizing the browser window.

        The previous approach resized the actual Firefox window to the
        page's full scroll height (up to 10000px) before capturing, so
        the GPU had to composite a window surface that could be several
        thousand pixels tall. On some systems/drivers that is enough to
        crash the GPU process and trigger a full display-driver reset
        (a momentary black screen across all monitors, not just this
        app's window). Firefox's own WebDriver exposes a native
        full-page screenshot command (`:takeFullPageScreenshot`) that
        captures the whole page by stitching it internally, with no
        window resize at all -- use that instead, and only fall back to
        the resize approach (capped much lower than before) if it's
        unavailable for some reason (e.g. an unusually old geckodriver).
        """
        shots_dir = os.path.join(self.output_dir, "screenshots")
        os.makedirs(shots_dir, exist_ok=True)
        filename = self.get_page_str(query, page_id, engine) + ".png"
        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.2)

        full_page_fn = getattr(self.driver, "get_full_page_screenshot_as_file", None)
        if callable(full_page_fn):
            try:
                full_page_fn(os.path.join(shots_dir, filename))
                return
            except Exception as e:
                self.log(f"  (native full-page screenshot failed, falling back: {e})", classes="text-orange")

        try:
            window_height = self.driver.execute_script("return document.documentElement.scrollHeight")
            self.driver.set_window_size(1600, min(window_height, 3000))
        except Exception:
            pass
        self.driver.save_screenshot(os.path.join(shots_dir, filename))
        self.driver.set_window_size(1600, 1000)

    def save_source_html(self, source_record):
        """Fetch one cited source page and save its raw HTML, plain and
        simple -- a direct `requests.get()`, no Selenium/JS rendering.
        Sources repeat a lot across different queries (the same NASA or
        WWF page gets cited for several answers), so each unique source
        (by `url_id`) is only ever fetched once per run.
        """
        link = source_record.get("url") or ""
        if not link:
            return
        url_id = source_record.get("url_id") or hashlib.md5(link.encode()).hexdigest()
        if url_id in self.sources_fetched:
            return
        self.sources_fetched.add(url_id)

        sources_dir = os.path.join(self.output_dir, "sources")
        os.makedirs(sources_dir, exist_ok=True)
        path = os.path.join(sources_dir, f"{url_id}.html")
        if os.path.exists(path):
            return

        try:
            response = self.http_session.get(link, timeout=15)
            response.raise_for_status()
            with open(path, "wb") as f:
                f.write(response.content)
        except requests.RequestException as e:
            self.log(f"  (couldn't fetch source page {link}: {e})", classes="text-orange")

    def save_all_sources(self, result):
        if not result:
            return
        for source in result.get("sources", []):
            self.save_source_html(source)
        for claim in result.get("claims", []):
            for source in claim.get("sources", []):
                self.save_source_html(source)

    def convert_to_csv(self):
        csv_path = self.results_file[:-6] + ".csv"
        with open(self.results_file, "r", encoding="utf-8") as in_json:
            rows = [json.loads(line) for line in in_json if line.strip()]
        if not rows:
            return

        for row in rows:
            claims = row.get("claims") or []
            if claims and row.get("engine", "google") == "google":
                # The DOM-scraped `main_answer` is often cut off mid-sentence
                # by Google's own markup (see get_claims() for why). The
                # first claim covers the same answer but captured in full,
                # with its source(s) attached, so use that for a main_answer
                # column that actually reads as a complete sentence.
                #
                # Bing doesn't have this truncation problem -- its
                # `main_answer` already comes from a complete heading/
                # sentence (see parse_bing_answer()) -- and its first claim
                # is frequently just a short bullet fragment ("Large,
                # forward-facing eyes"), not a stand-in summary, so
                # overwriting it here would make things worse, not better.
                row["main_answer"] = self.format_claim(claims[0])
            row["claims"] = self.claims_to_string(claims)

            # The top-level "sources" list is the same bottom-of-answer
            # citations that are already attached to their specific claims
            # in the "claims" column -- dropped here rather than duplicated
            # into its own column.
            row.pop("sources", None)

        fieldnames = [k for k in rows[0].keys() if k != "sources"]
        for row in rows[1:]:
            for k in row:
                if k != "sources" and k not in fieldnames:
                    fieldnames.append(k)

        # utf-8-sig (adds a BOM) rather than plain utf-8: Excel on Windows
        # has no reliable way to autodetect a BOM-less UTF-8 CSV, so without
        # it, Excel falls back to the system codepage (cp1252) and renders
        # exactly this kind of mojibake -- en dashes as "â€“", emoji as
        # garbled multi-character sequences, etc. The underlying file is
        # correct UTF-8 either way; this only changes how Excel guesses.
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        self.log(f"Wrote {csv_path}", classes="text-blue")

    @staticmethod
    def format_claim(c) -> str:
        src_str = "; ".join(
            f"{s.get('title', '')} ({s.get('url', '')})" for s in c.get("sources", [])
        )
        return f"{c.get('claim', '')} [{src_str}]"

    @classmethod
    def claims_to_string(cls, claims) -> str:
        if not claims:
            return ""
        return "\n".join(cls.format_claim(c) for c in claims)

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    @staticmethod
    def get_url_string(query: str) -> str:
        # quote_plus, not a bare .replace(" ", "+") -- a query containing
        # "&", "#", "%", or non-ASCII characters needs real percent-
        # encoding, or it gets silently truncated/misread as extra URL
        # parameters by Google/Bing (e.g. "coffee & tea" would cut off
        # after "coffee").
        return quote_plus(query.lower().strip())

    def get_page_str(self, query, page_id, engine="google"):
        safe_query = re.sub(r'[\\/*?:"<>|]', "", query)[:80]
        return f"{page_id}_{engine}_{safe_query}"

    def print_elapsed(self, start_time):
        self.log(f"  ({time.time() - start_time:.1f}s)")

    @staticmethod
    def is_goto_link(url) -> bool:
        parsed = urlparse(url or "")
        return parsed.path == "/goto" and re.fullmatch(r"(www\.)?google\.[a-z.]+", parsed.netloc) is not None

    def resolve_goto_link(self, goto_url) -> Optional[str]:
        if goto_url in self.resolved_goto_links:
            return self.resolved_goto_links[goto_url]
        resolved = None
        try:
            response = self.http_session.get(goto_url, allow_redirects=False, timeout=10)
            location = response.headers.get("Location")
            if response.is_redirect and location:
                resolved = urljoin(goto_url, location)
        except requests.RequestException:
            pass
        self.resolved_goto_links[goto_url] = resolved
        return resolved

    def source_link_fields(self, href) -> dict:
        fields = {}
        link = href or ""
        if self.is_goto_link(link):
            if self.follow_goto_links:
                resolved = self.resolve_goto_link(link)
                if resolved:
                    fields["goto_url"] = link
                    link = resolved
            elif not self.goto_notice_logged:
                self.log("Source links are obfuscated Google redirects. Enable 'Follow redirect links' for real URLs.", classes="text-orange")
                self.goto_notice_logged = True

        is_translated = "translate.google.com" in link
        if is_translated:
            parsed = urlparse(link)
            original_url = parse_qs(parsed.query).get("u", [None])[0] or link
            display_url = original_url
            display_domain = urlparse(original_url).netloc
            fields["translated_url"] = link
        else:
            display_url = link
            display_domain = urlparse(link).netloc

        return {"domain": display_domain, "url": display_url, "is_translated": is_translated, **fields}


# ======================================================================
# GUI
# ======================================================================
class GUI:
    def __init__(self):
        self.scraper = None
        self.label_insert = "Insert queries"
        self.label_queryfile = "Use query file"
        self.setup_ui()

    def setup_ui(self):
        ui.colors(primary="#1c5276")

        if os.path.isdir(ASSETS_DIR):
            # Batik parang pattern, tiled behind the cards with a white
            # wash blended in so the pattern reads as texture rather than
            # competing with the form content sitting on top of it.
            ui.add_head_html(
                """
                <style>
                  body {
                    background-image:
                      linear-gradient(rgba(255,255,255,0.88), rgba(255,255,255,0.88)),
                      url('/assets/batik_parang.jpg');
                    background-size: 100% 100%;
                    background-repeat: no-repeat;
                    background-attachment: fixed;
                  }
                </style>
                """
            )

        with ui.header().classes("items-center"):
            ui.label("Search Engines AI Overviewer").classes("text-xl font-bold")
            ui.label(f"v{APP_VERSION}").classes("text-sm opacity-70 ml-2")

        with ui.row().classes("w-full p-4 gap-4 items-stretch"):
            with ui.column().classes("w-1/2 min-w-[420px]"):
                with ui.card().classes("w-full"):
                    ui.label("Queries").classes("text-lg font-bold mb-2")
                    self.input_method = ui.radio([self.label_insert, self.label_queryfile], value=self.label_insert).props("inline")
                    default_query_file = get_default_output_dir(input_file=True)
                    self.file_input = ui.input("Query file (csv, one 'query' column)", value=default_query_file).classes("w-full") \
                        .bind_visibility_from(self.input_method, "value", value=self.label_queryfile)
                    self.text_input = ui.textarea(
                        "Enter queries (one per line)",
                        placeholder="is climate change real\nis scraping legal\ncan garlic serve as an antibiotic",
                    ).classes("w-full").bind_visibility_from(self.input_method, "value", value=self.label_insert)

                    ui.label("Search engines").classes("text-sm font-bold mt-2")
                    with ui.row().classes("w-full gap-4 items-center"):
                        self.scrape_google_cb = ui.checkbox("Google", value=True)
                        self.scrape_bing_cb = ui.checkbox("Bing", value=False)
                        self.scrape_duckduckgo_cb = ui.checkbox("DuckDuckGo", value=False)
                        with ui.icon("help_outline").classes("text-grey"):
                            ui.tooltip("Pick one or more. With several selected, each query is "
                                       "scraped from Google, then Bing, then DuckDuckGo, in that "
                                       "order, before moving to the next query.")

                    self.shuffle = ui.checkbox("Shuffle queries")
                    self.save_html_cb = ui.checkbox("Save raw HTML per query", value=True)
                    self.save_screenshots_cb = ui.checkbox("Save screenshot per query", value=False)
                    self.save_sources_cb = ui.checkbox("Save cited source pages (raw HTML)", value=False)
                    self.follow_goto_cb = ui.checkbox("Follow obfuscated redirect links", value=True)

                    default_output = get_default_output_dir()
                    self.output_dir = ui.input("Output directory", value=default_output).classes("w-full")

                with ui.card().classes("w-full"):
                    ui.label("Scrape settings").classes("text-lg font-bold mb-2")
                    self.profile = ui.input(
                        "Firefox profile path (optional)",
                        placeholder=r"C:\Users\You\AppData\Roaming\Mozilla\Firefox\Profiles\xxx.default",
                    ).classes("w-full")
                    with ui.row().classes("w-full gap-2"):
                        self.tld = ui.input("Top-level domain", value=".com").classes("flex-grow")
                        self.offset = ui.number("Offset", value=0, format="%.0f").classes("flex-grow")
                    self.throttle_strings = ui.input(
                        "Throttled text (comma-separated)", value="Something went wrong, Try again later"
                    ).classes("w-full")

                    with ui.expansion("CSS selectors (Google)").classes("w-full"):
                        self.selector_inputs = {}
                        for key, (label, tooltip) in SELECTOR_META.items():
                            with ui.row().classes("w-full items-center gap-1"):
                                inp = ui.input(label).classes("flex-grow").props("dense")
                                with ui.icon("help_outline").classes("text-grey"):
                                    ui.tooltip(tooltip)
                                self.selector_inputs[key] = inp

                    with ui.expansion("CSS selectors (Bing)").classes("w-full"):
                        self.bg_selector_inputs = {}
                        for key, (label, tooltip) in BG_SELECTOR_META.items():
                            with ui.row().classes("w-full items-center gap-1"):
                                inp = ui.input(label).classes("flex-grow").props("dense")
                                with ui.icon("help_outline").classes("text-grey"):
                                    ui.tooltip(tooltip)
                                self.bg_selector_inputs[key] = inp

                    with ui.expansion("CSS selectors (DuckDuckGo)").classes("w-full"):
                        self.dk_selector_inputs = {}
                        for key, (label, tooltip) in DK_SELECTOR_META.items():
                            with ui.row().classes("w-full items-center gap-1"):
                                inp = ui.input(label).classes("flex-grow").props("dense")
                                with ui.icon("help_outline").classes("text-grey"):
                                    ui.tooltip(tooltip)
                                self.dk_selector_inputs[key] = inp

            with ui.column().classes("w-1/2 min-w-[420px] flex-grow"):
                with ui.card().classes("w-full"):
                    ui.label("Run").classes("text-lg font-bold mb-2")
                    with ui.row().classes("w-full gap-2"):
                        self.btn_prepare = ui.button("1. Prepare browser", on_click=self.on_prepare).classes("flex-grow")
                        self.btn_start = ui.button("2. Start scraping", on_click=self.on_start).classes("flex-grow")
                        self.btn_start.disable()
                        self.btn_stop = ui.button("Stop", color="red", on_click=self.on_stop).classes("flex-grow")
                        self.btn_stop.disable()

                with ui.card().classes("w-full h-96 flex-grow"):
                    ui.label("Status").classes("text-lg font-bold")
                    self.progress_label = ui.label("Progress: 0/0")
                    self.progress_bar = ui.linear_progress(value=0, show_value=False).classes("w-full mb-4")
                    self.btn_open_folder = ui.button("Open output folder", on_click=self.open_output_folder).classes("w-full")
                    self.btn_open_folder.set_visibility(False)
                    self.log_scroll = ui.scroll_area().classes("w-full h-full rounded")
                    with self.log_scroll:
                        self.log_column = ui.column().classes("w-full p-2 gap-0")

        self.loaded_selectors = load_selectors(log=self.log_push_safe)
        for key, inp in self.selector_inputs.items():
            inp.value = self.loaded_selectors.get(key, "")
        for key, inp in self.bg_selector_inputs.items():
            inp.value = self.loaded_selectors.get(key, "")
        for key, inp in self.dk_selector_inputs.items():
            inp.value = self.loaded_selectors.get(key, "")

    def log_push_safe(self, msg):
        # Used only during early startup, before the log panel exists yet.
        print(msg)

    def log_push(self, msg, classes=None):
        base = "text-sm font-mono w-full break-words"
        extra = f" {classes}" if classes else ""
        with self.log_column:
            ui.label(msg).classes(base + extra).style("white-space: pre-wrap; word-break: break-word;")
        self.log_scroll.scroll_to(percent=1.0)

    def update_progress(self, current, total):
        self.progress_label.set_text(f"Progress: {current}/{total}")
        self.progress_bar.set_value(current / total if total else 0)

    async def on_prepare(self):
        self.log_column.clear()
        self.btn_open_folder.set_visibility(False)
        self.btn_prepare.disable()

        if not (self.output_dir.value or "").strip():
            self.output_dir.value = get_default_output_dir()

        query_file = self.file_input.value
        if self.input_method.value == self.label_insert:
            temp_file = os.path.join(self.output_dir.value, "queries.csv")
            os.makedirs(os.path.dirname(temp_file), exist_ok=True)
            queries = [q.strip() for q in self.text_input.value.split("\n") if q.strip()]
            with open(temp_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["query"])
                for q in queries:
                    writer.writerow([q])
            query_file = temp_file

        tld = self.tld.value.strip().lower()
        if not tld.startswith("."):
            tld = "." + tld

        selectors = {k: inp.value for k, inp in self.selector_inputs.items() if inp.value}
        selectors.update({k: inp.value for k, inp in self.bg_selector_inputs.items() if inp.value})
        selectors.update({k: inp.value for k, inp in self.dk_selector_inputs.items() if inp.value})

        self.scraper = GoogleAIOverviewScraper(
            base_url=f"https://google{tld}/",
            profile=self.profile.value,
            output_dir=self.output_dir.value,
            inserted_queries=self.text_input.value if self.input_method.value == self.label_insert else None,
            query_file=query_file if self.input_method.value == self.label_queryfile else None,
            offset=int(self.offset.value or 0),
            shuffle_queries=self.shuffle.value,
            save_html=self.save_html_cb.value,
            save_screenshots=self.save_screenshots_cb.value,
            save_sources_html=self.save_sources_cb.value,
            follow_goto_links=self.follow_goto_cb.value,
            throttled_strings=self.throttle_strings.value,
            selectors=selectors,
            scrape_google=self.scrape_google_cb.value,
            scrape_bing=self.scrape_bing_cb.value,
            scrape_duckduckgo=self.scrape_duckduckgo_cb.value,
            progress_callback=self.update_progress,
            log_callback=self.log_push,
        )

        success = await run.io_bound(self.scraper.prepare)
        if success:
            self.btn_start.enable()
        else:
            self.btn_prepare.enable()

    async def on_start(self):
        self.btn_start.disable()
        self.btn_stop.enable()
        await run.io_bound(self.scraper.process)
        self.btn_prepare.enable()
        self.btn_stop.disable()
        self.btn_open_folder.set_visibility(True)

    def on_stop(self):
        self.btn_stop.disable()
        if self.scraper:
            self.scraper.is_running = False

    def open_output_folder(self):
        output_dir = self.output_dir.value
        if output_dir and os.path.isdir(output_dir):
            if sys.platform == "win32":
                os.startfile(output_dir)
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", output_dir])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", output_dir])


@ui.page("/")
def index():
    GUI()


if __name__ in {"__main__", "__mp_main__"}:
    # host="127.0.0.1": NiceGUI binds all interfaces (0.0.0.0) by default,
    # which is why the startup banner used to list your LAN IPs -- anyone
    # else on the same network could otherwise open this app in their own
    # browser, with no login, and see/control your running scraper. Native
    # mode only ever needs to reach the app on this machine, so restrict it
    # to localhost.
    # Portrait rather than wide: the two main columns are each
    # min-w-[420px], so a window narrower than ~880px makes the layout's
    # flex row wrap them into a single stacked column instead of forcing
    # them side by side with the right column pushed off awkwardly.
    # port=8091: NiceGUI's native mode defaults to port 8080 if not told
    # otherwise, same as Kenniskrabber (another NiceGUI-based scraper this
    # tool owes credit to, see the module docstring) and plenty of other
    # NiceGUI apps -- if two such apps are ever open on the same machine
    # at once, the second one to start fails to bind that shared port. A
    # specific, uncommon port avoids that clash entirely.
    ui.run(
        title="Search Engines AI Overviewer",
        # favicon: NiceGUI accepts either an emoji or a path to an image
        # file here. The real window icon in the packaged .exe/.app comes
        # from assets/icon.ico / assets/icon.icns (baked in by PyInstaller
        # via build.py's --icon flag) -- this just makes the icon show up
        # the same way when running straight from source with `python
        # main.py` too, where there's no .exe/.app to carry an icon.
        favicon=os.path.join(ASSETS_DIR, "icon.png") if os.path.isdir(ASSETS_DIR) else None,
        native=True,
        reload=False,
        window_size=(560, 980),
        host="127.0.0.1",
        port=8091,
    )
