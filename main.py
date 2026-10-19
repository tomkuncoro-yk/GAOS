"""
Google AI Overview Scraper
===========================

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
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from nicegui import app, ui, run

from selenium import webdriver
from selenium.webdriver.common.by import By
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
APP_VERSION = "0.1"
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
    base_dir = os.path.join(user_home, "Documents", "GoogleAIOverviewScraper")
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
        progress_callback=None,
        log_callback=None,
    ):
        self.base_url = base_url or "https://www.google.com/"
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

        self.finished_queries = set()
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

    # ------------------------------------------------------------------
    # Browser setup
    # ------------------------------------------------------------------
    def get_driver(self, custom_profile=""):
        options = webdriver.FirefoxOptions()
        options.add_argument(f"--user-agent={USER_AGENT}")

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

        # Basic anti-detection: hide common signals that reveal an automated
        # browser (navigator.webdriver, missing plugins, WebGL vendor
        # strings, etc.). See stealthify.js next to this script.
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
                            self.finished_queries.add(json.loads(line)["id"])
                        except Exception as e:
                            self.log(f"Warning: could not parse a results line: {e}", classes="text-orange")
                self.log(f"Already collected {len(self.finished_queries)} queries previously.", classes="text-orange")

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

                if page_id in self.finished_queries:
                    self.log(f"[{count}/{total}] Already scraped, skipping: {query}", classes="text-orange")
                    continue

                self.log(f"[{count}/{total}] Scraping: {query}")
                start = time.time()
                try:
                    query_url = f"{self.base_url}search?q={self.get_url_string(query)}"
                    self.open_page(query_url)
                    result = self.parse_ai_overview(page_id, query)
                except Exception as e:
                    self.log(f"Error scraping '{query}': {e}", classes="text-red")
                    import traceback

                    self.log(traceback.format_exc())
                    result = None

                if result:
                    for k, v in self.query_extra_data.get(page_id, {}).items():
                        result.setdefault(k, v)
                    with open(self.results_file, "a", encoding="utf-8") as out:
                        out.write(json.dumps(result) + "\n")
                    self.log(f"  -> {len(result.get('claims', []))} claims, {len(result.get('sources', []))} sources", classes="text-blue")
                else:
                    self.log("  -> No AI Overview found for this query.", classes="text-orange")

                if self.save_html_enabled:
                    self.save_html(query, page_id)
                if self.save_screenshots_enabled:
                    self.save_screenshot(query, page_id)
                if self.save_sources_enabled:
                    self.save_all_sources(result)

                self.finished_queries.add(page_id)
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
                self.driver.get(url)
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

    def dismiss_cookie_consent(self):
        """Best-effort click on Google's cookie/consent dialog, if one appears.

        Doesn't rely on an external extension -- just tries a handful of
        commonly-used button ids/text on Google's consent interstitial.
        Silently does nothing if no consent dialog is present.
        """
        candidates = [
            "#L2AGLb",  # "Accept all" button id Google has used for a long time
            "button[aria-label='Accept all']",
            "button[aria-label='Reject all']",
            "div[aria-label='Accept all']",
        ]
        for sel in candidates:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for el in elements:
                    if el.is_displayed():
                        el.click()
                        time.sleep(0.5)
                        return
            except Exception:
                continue

    def check_for_captcha(self):
        captcha = self.driver.find_elements(By.CSS_SELECTOR, "#recaptcha")
        if captcha and captcha[0].is_displayed():
            self.log("CAPTCHA detected -- please solve it in the Firefox window...", classes="text-orange")
            while captcha and captcha[0].is_displayed():
                time.sleep(2)
                captcha = self.driver.find_elements(By.CSS_SELECTOR, "#recaptcha")

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

        The claim "container" to climb to is the nearest ancestor
        carrying a `data-hveid` attribute (Google's own per-segment
        id), starting the search from the badge's *parent* -- the
        badge itself also carries `data-hveid`, so starting from the
        badge would just match itself and return empty/near-empty
        text. This covers both bulleted claims (wrapped in an `<li
        data-hveid=...>`) and the lead-in sentence, which has no `<li>`
        or `<p>` wrapper at all but does have a `data-hveid` div a few
        levels up -- a case the earlier `.closest('li') || closest('p')`
        approach missed, producing an empty `"claim": ""` entry.
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
                        // Walk up to the nearest ancestor carrying Google's
                        // own per-segment data-hveid id. Start from the
                        // badge's PARENT, since the badge itself also has
                        // a data-hveid and would otherwise match first.
                        let root = badge.parentElement;
                        while (root && !root.hasAttribute('data-hveid')) {
                            root = root.parentElement;
                        }
                        if (!root) return '';
                        const parts = [];
                        root.querySelectorAll('*').forEach(el => {
                            if (el.children.length === 0 && !el.closest('button')) {
                                const text = el.textContent.trim();
                                if (text) parts.push(text);
                            }
                        });
                        return parts.join(' ');
                        """,
                        link_badge,
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
    # Saving
    # ------------------------------------------------------------------
    def save_html(self, query, page_id):
        html_dir = os.path.join(self.output_dir, "html")
        os.makedirs(html_dir, exist_ok=True)
        filename = self.get_page_str(query, page_id) + ".html"
        with open(os.path.join(html_dir, filename), "w", encoding="utf-8") as f:
            f.write(self.driver.page_source)

    def save_screenshot(self, query, page_id):
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
        filename = self.get_page_str(query, page_id) + ".png"
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
            row["sources"] = self.list_to_string(row.get("sources"))
            row["claims"] = self.claims_to_string(row.get("claims"))
        fieldnames = list(rows[0].keys())
        for row in rows[1:]:
            for k in row:
                if k not in fieldnames:
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
    def list_to_string(sources) -> str:
        if not sources:
            return ""
        return "\n".join(f"{s.get('title', '')}: {s.get('description', '')} - {s.get('url', '')}" for s in sources)

    @staticmethod
    def claims_to_string(claims) -> str:
        if not claims:
            return ""
        lines = []
        for c in claims:
            src_str = "; ".join(
                f"{s.get('title', '')} ({s.get('url', '')})" for s in c.get("sources", [])
            )
            lines.append(f"{c.get('claim', '')} [{src_str}]")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    @staticmethod
    def get_url_string(query: str) -> str:
        return query.lower().strip().replace(" ", "+")

    def get_page_str(self, query, page_id):
        safe_query = re.sub(r'[\\/*?:"<>|]', "", query)[:80]
        return f"{page_id}_{safe_query}"

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
            ui.label("Google AI Overview Scraper").classes("text-xl font-bold")
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

                    with ui.expansion("CSS selectors").classes("w-full"):
                        self.selector_inputs = {}
                        for key, (label, tooltip) in SELECTOR_META.items():
                            with ui.row().classes("w-full items-center gap-1"):
                                inp = ui.input(label).classes("flex-grow").props("dense")
                                with ui.icon("help_outline").classes("text-grey"):
                                    ui.tooltip(tooltip)
                                self.selector_inputs[key] = inp

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
    ui.run(title="Google AI Overview Scraper", native=True, reload=False, window_size=(560, 980), host="127.0.0.1")
