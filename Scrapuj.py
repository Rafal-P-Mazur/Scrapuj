import json
import requests
from bs4 import BeautifulSoup
import os
import tempfile
import pandas as pd
import TemplateCreator_flet
import time
import random
import logging
from urllib.robotparser import RobotFileParser
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import flet as ft
import threading
from lxml import html, etree
from soupsieve.util import SelectorSyntaxError
import sys
import re

LXML_AVAILABLE = True
# Optional Playwright fallback (only used if installed)
try:
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

# ----------------------------
# PyInstaller-Safe Path Setup
# ----------------------------
def get_base_path():
    """Get the reliable base path, whether running as a script or PyInstaller exe."""
    if getattr(sys, 'frozen', False):
        # Running as a PyInstaller executable
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        # Running as a normal Python script
        return os.path.dirname(os.path.abspath(__file__))

BASE_PATH = get_base_path()
TEMPLATE_DIR = os.path.join(BASE_PATH, "templates")
COOKIE_DIR = os.path.join(BASE_PATH, "cookies")
OUTPUT_DIR = os.path.join(BASE_PATH, "output")

# --- Create directories if they don't exist ---
for path in [TEMPLATE_DIR, COOKIE_DIR, OUTPUT_DIR]:
    os.makedirs(path, exist_ok=True)

# ----------------------------
# Configuration / Defaults
# ----------------------------
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/119.0.0.0 Safari/537.36"
)

# A small sample list of user-agents for rotation (expand if desired)
USER_AGENTS = [
    DEFAULT_USER_AGENT,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
]

CAPTCHA_PATTERNS = re.compile(
    r"captcha|recaptcha|prove you are human|verify you are human|are you human",
    flags=re.IGNORECASE,
)

# Configure logging if you want to also write to a file (optional)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ----------------------------
# Scraper Logic (Backend)
# ----------------------------
class Scraper:
    def __init__(self, *, rotate_user_agent=True, min_delay=1.0, max_delay=3.0, proxies=None, retry_total=3):
        """
        rotate_user_agent: pick random UA for each session
        min_delay/max_delay: random sleep between requests (seconds)
        proxies: dict to pass to requests (e.g. {"http": "...", "https": "..."})
        retry_total: number of retries for transient errors
        """
        self.template = {"selectors": {}}
        self.rotate_user_agent = rotate_user_agent
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.proxies = proxies
        self.retry_total = retry_total

    def _make_session(self):
        session = requests.Session()

        # Choose user-agent
        ua = random.choice(USER_AGENTS) if self.rotate_user_agent else DEFAULT_USER_AGENT
        session.headers.update({
            "User-Agent": ua,
            "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Referer": "https://www.google.com/",
        })

        # Retry strategy for transient network errors
        retry_strategy = Retry(
            total=self.retry_total,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        if self.proxies:
            session.proxies.update(self.proxies)

        return session

    def read_template(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def clean_text(self, el):
        """Clean a BeautifulSoup element, preserving paragraphs and line breaks."""
        for br in el.find_all("br"):
            br.replace_with("\n")
        for p in el.find_all("p"):
            p.insert_before("\n\n")
        text = el.get_text()
        lines = text.splitlines()
        cleaned_lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in lines if line.strip()]
        return "\n".join(cleaned_lines)

    def _is_allowed_by_robots(self, url, user_agent):
        """
        Return True if either robots.txt allows the URL for our user-agent, or if checking fails (fail-open).
        """
        try:
            parsed = urlparse(url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            rp = RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
            allowed = rp.can_fetch(user_agent, url)
            return allowed
        except Exception:
            # If robots.txt cannot be read, we choose to proceed (fail-open) — adjust if you prefer fail-closed.
            return True

    def _detect_captcha(self, text):
        if not text:
            return False
        return bool(CAPTCHA_PATTERNS.search(text))

    def _save_batch_urls(self, batch_links, output_file, log_callback=None):
        """Appends a batch of links to a TXT file."""
        if not batch_links:
            return

        try:
            # Deduplicate links within this batch
            unique_links = list(dict.fromkeys(batch_links))

            # Use "a" mode to append. Add a newline to separate from previous batches.
            with open(output_file, "a", encoding="utf-8") as f:
                # Add a newline only if the file is not empty
                if f.tell() > 0:
                    f.write("\n")
                f.write("\n".join(unique_links))

            if log_callback:
                log_callback(f"💾 Batch of {len(unique_links)} links saved to {os.path.basename(output_file)}")
        except Exception as e:
            log_msg = f"❌ Error saving URL batch: {e!r}"
            logging.error(log_msg)
            if log_callback:
                log_callback(log_msg)

    def _save_batch_json(self, batch_results, json_file, log_callback=None):
        """Appends a batch of results to a JSON file."""
        if not batch_results:
            return

        existing_data = []
        try:
            # Try to read existing data
            if os.path.exists(json_file) and os.path.getsize(json_file) > 0:
                with open(json_file, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                if not isinstance(existing_data, list):
                    existing_data = []  # Overwrite corrupted data
        except json.JSONDecodeError:
            if log_callback:
                log_callback(f"⚠️ Corrupted JSON file found. Overwriting with new batch.")
            existing_data = []  # Overwrite corrupted file
        except Exception as e:
            log_msg = f"❌ Error reading existing JSON file: {e!r}"
            logging.error(log_msg)
            if log_callback:
                log_callback(log_msg)
            # Don't stop, just overwrite
            existing_data = []

        try:
            # Add new batch results to the list
            existing_data.extend(batch_results)

            # Write the entire updated list back to the file
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2, ensure_ascii=False)

            if log_callback:
                log_callback(f"💾 Batch of {len(batch_results)} items saved to {os.path.basename(json_file)}")
        except Exception as e:
            log_msg = f"❌ Error saving JSON batch: {e!r}"
            logging.error(log_msg)
            if log_callback:
                log_callback(log_msg)

    def _log_error_to_file(self, error_file, url, error_message):
        """Appends a failed URL and error message to a log file."""
        if not error_file:
            return
        try:
            # Ensure error_message is a single line
            clean_message = str(error_message).replace('\n', ' ').replace('\r', '')
            with open(error_file, "a", encoding="utf-8") as f:
                f.write(f"{url}\t{clean_message}\n")
        except Exception as e:
            # Log to console if writing to file fails
            logging.error(f"Failed to write to error log file: {e!r}")

    def _save_batch_metadata(self, batch_results, template, export_folder, main_tag, xlsx_file, json_file,
                             log_callback=None):
        """Saves a batch for 'text_metadata' mode: appends to JSON, appends to XLSX, and writes new TXT files."""
        if not batch_results:
            return

        # 1. Save to the master JSON file
        self._save_batch_json(batch_results, json_file, log_callback)

        # 2. Process and save XLSX and TXT files
        df_existing = None
        current_idx = 0
        try:
            # Check if xlsx file exists to get the starting index for new files
            if os.path.exists(xlsx_file) and os.path.getsize(xlsx_file) > 0:
                df_existing = pd.read_excel(xlsx_file)
                current_idx = len(df_existing)
        except Exception as e:
            if log_callback:
                log_callback(f"⚠️ Could not read existing metadata.xlsx: {e!r}. Creating a new one.")
            df_existing = None
            current_idx = 0

        metadata_rows = []
        new_txt_files_count = 0

        try:
            # 3. Create new TXT files and prepare metadata rows
            for i, row in enumerate(batch_results, start=1):
                # Don't create TXT files for rows that were errors
                if "error" in row:
                    metadata_row = dict(row)
                    metadata_row["Nazwa pliku"] = "ERROR"
                    metadata_rows.append(metadata_row)
                    continue

                txt_idx = current_idx + i  # This gives us file names 1, 2, ... 100, 101, ...
                merged_content = []

                # Merge tags in the user-defined order
                if isinstance(main_tag, list):
                    for tag in main_tag:
                        content = row.get(tag, "")
                        if isinstance(content, list):
                            merged_content.extend(content)
                        elif content:
                            merged_content.append(str(content))
                elif main_tag:  # Fallback for a single tag string (should be list from UI)
                    content = row.get(main_tag, "")
                    if isinstance(content, list):
                        merged_content = content
                    elif content:
                        merged_content = [str(content)]

                txt_filename = f"{txt_idx}.txt"
                with open(os.path.join(export_folder, txt_filename), "w", encoding="utf-8") as f:
                    f.write("\n\n".join(merged_content))  # Join with double newline

                new_txt_files_count += 1

                metadata_row = dict(row)  # keep everything
                metadata_row["Nazwa pliku"] = txt_filename
                metadata_rows.append(metadata_row)

            # 4. Create new DataFrame and append to existing (if any)
            df_new = pd.DataFrame(metadata_rows)

            if df_existing is not None:
                df_to_save = pd.concat([df_existing, df_new], ignore_index=True)
            else:
                df_to_save = df_new

            # 5. Reorder columns
            cols = df_to_save.columns.tolist()
            if 'url' in cols:
                cols.insert(0, cols.pop(cols.index('url')))
            if 'Nazwa pliku' in cols:
                cols.insert(0, cols.pop(cols.index('Nazwa pliku')))
            df_to_save = df_to_save[cols]

            # 6. Save the updated XLSX file
            df_to_save.to_excel(xlsx_file, index=False)

            if log_callback:
                log_callback(f"💾 Batch of {new_txt_files_count} TXT files saved.")
                log_callback(f"💾 Metadata for {len(metadata_rows)} items appended to {os.path.basename(xlsx_file)}")

        except Exception as e:
            log_msg = f"❌ Error saving metadata batch: {e!r}"
            logging.error(log_msg)
            if log_callback:
                log_callback(log_msg)

    def _extract_from_content(self, page_content, url, template, mode, resp_for_lxml=None):
        """
        Internal helper to extract data from raw HTML content based on the template and mode.
        This is the core extraction logic, now reusable by both 'requests' and 'playwright' engines.
        """
        row = {"url": url}
        lxml_tree = None
        # soup = BeautifulSoup(page_content, "html.parser", from_encoding="utf-8")
        soup = BeautifulSoup(page_content, "html.parser")
        all_links_for_url_mode = []  # For 'urls_only' mode

        # --- START MODIFICATION ---
        selectors = template.get("selectors", {})
        main_selectors = {k: v for k, v in selectors.items() if not k.endswith("_excluded")}
        excluded_selectors = {k: v for k, v in selectors.items() if k.endswith("_excluded")}

        # 1. PRZETWARZANIE WYKLUCZANIA
        # Usuń z soup elementy pasujące do selektorów wykluczonych.
        # Jest to konieczne, jeśli główny selektor (np. 'treść') zawiera te, które mają być pominięte (np. 'photo_excluded').
        # Wykluczanie działa najlepiej z domyślnym parsowaniem.
        for exclude_category, exclude_selector in excluded_selectors.items():
            try:
                # Z uwagi na to, że selektor wykluczający jest częścią drzewa,
                # musi być parsowany za pomocą tego samego narzędzia co główny selektor.
                # Używamy soup.select do znalezienia elementów.
                excluded_elements = soup.select(exclude_selector)
                for el in excluded_elements:
                    el.decompose()  # Usuń element z drzewa BeautifulSoup
                logging.debug(f"Successfully decomposed {len(excluded_elements)} elements for {exclude_category}")
            except SelectorSyntaxError:
                # Ignorujemy błędy składni CSS dla wykluczonych (nie obsługujemy XPath do usuwania)
                logging.warning(f"Exclusion selector '{exclude_selector}' is not valid CSS and was skipped.")
            except Exception as e:
                logging.error(f"Error during exclusion for '{exclude_selector}': {e!r}")

        # 2. PRZETWARZANIE GŁÓWNYCH SELEKTORÓW
        # Iterujemy tylko po selektorach, które nie są 'excluded'
        for category, selector in main_selectors.items():
            # --- END MODIFICATION ---

            els = []
            try:
                # First, attempt to use the selector as a CSS selector
                els = soup.select(selector)
            except SelectorSyntaxError:
                # If it's not valid CSS syntax, fall back to XPath
                if not LXML_AVAILABLE:
                    logging.warning(
                        f"Invalid CSS selector '{selector}'. Install 'lxml' for XPath fallback.")
                    continue

                try:
                    # Create the lxml tree from raw content if it doesn't exist
                    if lxml_tree is None:
                        # --- START FIX ---
                        # Define the UTF-8 parser ONCE
                        lxml_parser = html.HTMLParser(encoding="utf-8")

                        if resp_for_lxml:
                            # We used 'requests' which provides bytes
                            lxml_tree = html.fromstring(resp_for_lxml, parser=lxml_parser)
                        else:
                            # We used Playwright which provides a string, so we encode it
                            # AND pass the parser to tell lxml it's utf-8
                            lxml_tree = html.fromstring(page_content.encode('utf-8'), parser=lxml_parser)
                        # --- END FIX ---

                    lxml_els = lxml_tree.xpath(selector)
                    # Convert lxml elements to BeautifulSoup elements for consistent processing.
                    els = [BeautifulSoup(etree.tostring(l_el, encoding='unicode'), 'html.parser')
                           for l_el in lxml_els]
                except Exception as e:
                    logging.info(f"Playwright processing failed for {url}: {e!r}")
                    els = []
            except Exception as e:
                logging.error(f"An unexpected error occurred with selector '{selector}': {e!r}")
                els = []


            if els:
                texts = [self.clean_text(el) for el in els if el.get_text(strip=True)]
                links = []
                for el in els:
                    if el.name == "a" and el.get("href"):
                        links.append(urljoin(url, el["href"]))
                    for a in el.find_all("a", href=True):
                        links.append(urljoin(url, a["href"]))

                if mode in ["text_only", "text_metadata"]:
                    row[category] = texts[0] if len(texts) == 1 else texts
                elif mode == "urls_only":
                    all_links_for_url_mode.extend(links)  # Collect all links
            else:
                if mode in ["text_only", "text_metadata"]:
                    row[category] = None

        if mode == "urls_only":
            row["urls"] = all_links_for_url_mode  # Add collected links to the row

        return row

    def _process_playwright_page(self, page, url, template, mode, scrape_script=None, log_callback=None,
                                 cancel_flag=None):
        """
        Process a SINGLE page/URL using an EXISTING Playwright page object.
        This contains the script execution and data extraction logic.
        """
        collected_rows = []
        scrape_called_by_user = False  # Flag to track user call

        try:
            # --- Browser/Context setup is REMOVED from here ---

            # 1. Navigate to the new URL
            def handle_route(route):
                # Block images, stylesheets, and fonts
                if (route.request.resource_type in ["image", "stylesheet", "font"]):
                    route.abort()
                else:
                    route.continue_()

            # Apply the handler to all requests
            page.route(re.compile(r".*"), handle_route)
            page.goto(url)
            page.wait_for_load_state("load")

            # 2. Define the scrape() function for the user
            def user_scrape_function():
                """Internal function exposed to user script as 'scrape()'."""
                nonlocal scrape_called_by_user
                scrape_called_by_user = True
                try:
                    if log_callback:
                        log_callback(f"ℹ️ scrape() called by script on {page.url}")
                    current_html = page.content()

                    # Use the core extraction helper
                    scraped_row = self._extract_from_content(
                        page_content=current_html,
                        url=page.url,  # Use current page URL, might have changed
                        template=template,
                        mode=mode
                    )
                    collected_rows.append(scraped_row)
                    if log_callback:
                        log_callback(f"✅ scrape() successful, {len(scraped_row) - 1} categories found.")
                except Exception as e:
                    logging.error(f"Error during user-called scrape(): {e!r}")
                    if log_callback:
                        log_callback(f"❌ Error during user-called scrape(): {e!r}")


            # 3. EXECUTE USER SCRIPT
            if scrape_script:
                logging.info(f"Executing scrape action script for {url}")
                try:
                    exec_globals = {
                        'page': page,
                        'time': time,
                        'scrape': user_scrape_function,
                    }
                    if log_callback:
                        exec_globals['log'] = log_callback
                    if cancel_flag:
                        exec_globals['is_cancelled'] = cancel_flag

                    exec(scrape_script, exec_globals)
                    logging.info(f"Successfully executed script for {url}")

                except Exception as e:
                    logging.error(f"Error executing scrape script for {url}: {e!r}")
                    if log_callback:
                        log_callback(f"❌ Script error on {url}: {e!r}")

            # 4. AUTO-SCRAPE (Fallback)
            if not scrape_called_by_user:
                # --- START MODIFICATION ---
                # Only log if a script *ran* but didn't call scrape().
                # If no script was provided, this is just noise and slows down the app.
                if log_callback and scrape_script:
                    log_callback(f"ℹ️ Script finished. Auto-scraping final page state for {url}...")
                # --- END MODIFICATION ---
                try:
                    current_html = page.content()
                    scraped_row = self._extract_from_content(
                        page_content=current_html,
                        url=page.url,
                        template=template,
                        mode=mode
                    )
                    collected_rows.append(scraped_row)


                except Exception as e:
                    logging.error(f"Error during auto-scrape: {e!r}")
                    if log_callback:
                        log_callback(f"❌ Error during auto-scrape: {e!r}")



            return {"status": "ok", "scraped_rows": collected_rows}  # Return collected data
        except Exception as e:
            logging.info(f"Playwright processing failed for {url}: {e!r}")
            return {"status": "error", "message": str(e)}

    def _run_playwright_session(self, urls, template, mode, scrape_script, log_callback, headless, cancel_flag,
                                cookie_file_path, timeout=60000,
                                # --- START: Add batch save params ---
                                output_file=None, json_file=None, xlsx_file=None,
                                export_folder=None, main_tag_keys=None,
                                # --- END: Add batch save params ---
                                error_log_file=None
                                ):
        """
        Launches ONE Playwright browser instance, loads cookies once,
        and iterates through all URLs, saving in batches.
        """
        all_results = []  # To store results from ALL URLs
        num_urls = len(urls)

        # --- START: Add batch lists ---
        batch_results = []
        batch_links = []
        # --- END: Add batch lists ---

        if not PLAYWRIGHT_AVAILABLE:
            if log_callback:
                log_callback("❌ Playwright is not installed.")
            return []  # Return empty list

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)

                # --- Load cookies ONCE ---
                context_args = {}
                if cookie_file_path:
                    try:
                        if os.path.exists(cookie_file_path) and os.path.getsize(cookie_file_path) > 0:
                            with open(cookie_file_path, 'r', encoding='utf-8') as f:
                                json.load(f)  # Test load
                            context_args["storage_state"] = cookie_file_path
                            if log_callback:
                                log_callback(f"ℹ️ Loading session from {os.path.basename(cookie_file_path)}")
                        else:
                            if log_callback:
                                log_callback(f"⚠️ Cookie file not found or is empty. Proceeding without session.")
                    except Exception as e:
                        if log_callback:
                            log_callback(f"❌ Error loading cookie file: {e!r}. Proceeding without session.")

                context = browser.new_context(**context_args)
                page = context.new_page()  # Create the FIRST page
                page.set_default_timeout(timeout)

                # --- NOW we loop through the URLs ---
                for idx, url in enumerate(urls, start=1):
                    if cancel_flag and cancel_flag():
                        if log_callback:
                            log_callback("⚠️ Scraping canceled by user.")
                        break

                    try:
                        if url.startswith("view-source:"):
                            url = url[len("view-source:"):]

                        # Check robots.txt (using default UA for check)
                        if not self._is_allowed_by_robots(url, DEFAULT_USER_AGENT):
                            msg = f"⚠️ [{idx}/{num_urls}] Skipped {url} — disallowed by robots.txt"
                            error_row = {"url": url, "error": "Disallowed by robots.txt"}
                            self._log_error_to_file(error_log_file, url, "Disallowed by robots.txt")
                            # all_results.append(error_row)
                            # --- START: Add to batch ---
                            if mode != "urls_only":
                                batch_results.append(error_row)
                            # --- END: Add to batch ---
                            if log_callback:
                                log_callback(msg)
                            continue

                        if log_callback:
                            log_callback(f"🚀 [{idx}/{num_urls}] Navigating to {url}...")

                        # Call the refactored processing function
                        page_result = self._process_playwright_page(
                            page=page,
                            url=url,
                            template=template,
                            mode=mode,
                            scrape_script=scrape_script,
                            log_callback=log_callback,
                            cancel_flag=cancel_flag
                        )

                        # Handle the result directly instead of raising an error
                        if page_result["status"] == "ok":
                            #all_results.extend(page_result["scraped_rows"])
                            # --- START: Add to batch ---
                            if mode == "urls_only":
                                for r in page_result["scraped_rows"]:
                                    batch_links.extend(r.get("urls", []))
                            else:
                                batch_results.extend(page_result["scraped_rows"])
                            # --- END: Add to batch ---

                            if log_callback:
                                log_callback(
                                    f"✅ [{idx}/{num_urls}] {url} scraped ({len(page_result['scraped_rows'])} items found)")
                        else:
                            # Page processing failed, log it and append the error
                            error_message = page_result.get('message', 'Unknown Playwright processing error')
                            self._log_error_to_file(error_log_file, url, error_message)
                            logging.warning(f"Playwright processing failed for {url}: {error_message}")
                            if log_callback:
                                log_callback(f"❌ [{idx}/{num_urls}] {url} error: {error_message}")

                            error_row = {"url": url, "error": error_message}
                            #all_results.append(error_row)
                            # --- START: Add to batch ---
                            if mode != "urls_only":
                                batch_results.append(error_row)
                            # --- END: Add to batch ---

                            # --- START THE COMBINED FIX ---
                            # The page is broken. We must close it and create a new one.
                            if log_callback:
                                log_callback(f"ℹ️ Attempting to recover Playwright session...")
                            try:
                                page.close()  # Close the broken page
                                page = context.new_page()  # Create a fresh page from the same context
                                page.set_default_timeout(timeout)  # Re-apply the timeout
                                if log_callback:
                                    log_callback(f"✅ Playwright session recovered. Continuing to next URL.")
                            except Exception as recovery_e:
                                # If this recovery fails, the whole context or browser is dead.
                                logging.error(f"Critical error: Failed to recover Playwright context: {recovery_e!r}")
                                if log_callback:
                                    log_callback(f"💥 CRITICAL: Playwright context is unresponsive. Aborting run.")
                                break  # Break out of the for-loop entirely.
                            # --- END THE COMBINED FIX ---


                    except Exception as e:
                        # This 'except' catches other errors (e.g., robots.txt failure)
                        logging.error(f"Error processing {url}: {e!r}")
                        if log_callback:
                            log_callback(f"❌ [{idx}/{num_urls}] {url} error: {e!r}")
                        error_row = {"url": url, "error": repr(e)}
                        self._log_error_to_file(error_log_file, url, repr(e))
                        # all_results.append(error_row)  # Log error for this URL
                        # --- START: Add to batch ---
                        if mode != "urls_only":
                            batch_results.append(error_row)
                        # --- END: Add to batch ---

                    # --- START: Batch Save Logic ---
                    if (idx % 100 == 0 or idx == num_urls) and not (cancel_flag and cancel_flag()):
                        if log_callback:
                            log_callback(f"💾 Saving batch... (up to URL {idx}/{num_urls})")
                        try:
                            if mode == "urls_only":
                                self._save_batch_urls(batch_links, output_file, log_callback)
                                batch_links.clear()
                            elif mode == "text_only":
                                self._save_batch_json(batch_results, json_file, log_callback)
                                batch_results.clear()
                            elif mode == "text_metadata":
                                self._save_batch_metadata(batch_results, template, export_folder, main_tag_keys,
                                                          xlsx_file, json_file, log_callback)
                                batch_results.clear()
                        except Exception as e:
                            log_msg = f"❌ CRITICAL: Failed to save batch! {e!r}"
                            logging.error(log_msg)
                            if log_callback:
                                log_callback(log_msg)
                    # --- END: Batch Save Logic ---

                    # Apply delay *between* requests
                    time.sleep(random.uniform(self.min_delay, self.max_delay))

                # --- Loop finished ---
                context.close()
                browser.close()
        except Exception as e:
            logging.error(f"A critical Playwrisght session error occurred: {e!r}")
            if log_callback:
                log_callback(f"💥 A critical Playwright error occurred: {e!r}")
        return []
        #return all_results  # Return the full list of results

    def run_scraper_from_content(self, template_content, urls_text, output_name, mode="text_only",
                                 progress_callback=None, cancel_flag=None, engine="requests", scrape_script=None,
                                 headless=True, cookie_file_path=None,
                                 # --- START: Add main_tag_keys ---
                                 main_tag_keys=None
                                 # --- END: Add main_tag_keys ---
                                 ):
        try:
            template = json.loads(template_content)
        except json.JSONDecodeError:
            return {"status": "error", "message": "Invalid JSON in template file."}

        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8") as tmp:
            json.dump(template, tmp, indent=2, ensure_ascii=False)
            tmp_filename = tmp.name

        result = self.run_scraper(tmp_filename, urls_text, output_name, mode, progress_callback, cancel_flag, engine,
                                  scrape_script, headless, cookie_file_path=cookie_file_path,
                                  # --- START: Pass main_tag_keys ---
                                  main_tag_keys=main_tag_keys
                                  # --- END: Pass main_tag_keys ---
                                  )
        os.remove(tmp_filename)
        return result

    # --- MODIFIED ---
        # --- MODIFIED ---
    def run_scraper(self, template_file, urls_text, output_name, mode="text_only",
                    progress_callback=None, cancel_flag=None, engine="requests", scrape_script=None, headless=True,
                    cookie_file_path=None,
                    # --- START: Add main_tag_keys ---
                    main_tag_keys=None
                    # --- END: Add main_tag_keys ---
                    ):
        try:
            with open(template_file, encoding="utf-8") as f:
                template = json.load(f)
        except FileNotFoundError:
            return {"status": "error", "message": f"Template {template_file} not found."}

        # --- START: Define all output paths ---
        ext = ".json" if mode in ["text_only", "text_metadata"] else ".txt"
        output_file = os.path.join(OUTPUT_DIR, output_name + ext)  # TXT file or "text_only" JSON

        export_folder = None
        json_file = None  # Master JSON file (for text_only or metadata)
        xlsx_file = None  # Metadata XLSX file
        error_log_file = os.path.join(OUTPUT_DIR, output_name + "_errors.txt")  # <-- ADD THIS

        # --- Clear old files for a fresh run ---
        if os.path.exists(error_log_file): os.remove(error_log_file)  # <-- ADD THIS

        if mode == "text_metadata":
            export_folder = os.path.join(OUTPUT_DIR, output_name)
            os.makedirs(export_folder, exist_ok=True)
            # This JSON file stores all raw scraped data for metadata mode
            json_file = os.path.join(export_folder, "scraped_data.json")
            xlsx_file = os.path.join(export_folder, "metadane.xlsx")
            # Clear old files
            if os.path.exists(json_file): os.remove(json_file)
            if os.path.exists(xlsx_file): os.remove(xlsx_file)
        elif mode == "text_only":
            json_file = output_file
            # Clear old file for a fresh run
            if os.path.exists(json_file): os.remove(json_file)
        elif mode == "urls_only":
            # Clear old file for a fresh run
            if os.path.exists(output_file): os.remove(output_file)
        # --- END: Define all output paths ---

        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        num_urls = len(urls)
        results = []  # This will hold ALL results for the final return

        # --- START: Add batch lists ---
        batch_results = []
        batch_links = []
        # --- END: Add batch lists ---

        # --- REFACTORED LOGIC ---
        if engine == "playwright":
            # Playwright engine handles its own session and loop
            results = self._run_playwright_session(
                urls=urls,
                template=template,
                mode=mode,
                scrape_script=scrape_script,
                log_callback=progress_callback,
                headless=headless,
                cancel_flag=cancel_flag,
                cookie_file_path=cookie_file_path,
                # --- START: Pass batch params ---
                output_file=output_file,
                json_file=json_file,
                xlsx_file=xlsx_file,
                export_folder=export_folder,
                main_tag_keys=main_tag_keys,
                # --- END: Pass batch params ---
                error_log_file = error_log_file
            )
        else:
            # 'requests' engine uses the original loop-per-URL logic
            session = self._make_session()
            ua_for_robots = session.headers.get("User-Agent", DEFAULT_USER_AGENT)

            for idx, url in enumerate(urls, start=1):
                if cancel_flag and cancel_flag():
                    if progress_callback:
                        progress_callback("⚠️ Scraping canceled by user.")
                    break

                scraped_row = None
                error_row = None

                try:
                    if url.startswith("view-source:"):
                        url = url[len("view-source:"):]

                    if not self._is_allowed_by_robots(url, ua_for_robots):
                        msg = f"⚠️ [{idx}/{len(urls)}] Skipped {url} — disallowed by robots.txt"
                        error_row = {"url": url, "error": "Disallowed by robots.txt"}
                        self._log_error_to_file(error_log_file, url, "Disallowed by robots.txt")  # <-- ADD THIS
                        if progress_callback:
                            progress_callback(msg)
                    else:
                        page_content = None
                        resp_for_lxml = None

                        time.sleep(random.uniform(self.min_delay, self.max_delay))

                        # This is now ONLY the 'requests' logic
                        max_retries = 3
                        for attempt in range(1, max_retries + 1):
                            try:
                                resp = session.get(url, timeout=15)
                                resp.raise_for_status()
                                soup_check = BeautifulSoup(resp.content, "html.parser")
                                if soup_check.get_text(strip=True):
                                    page_content = resp.content
                                    resp_for_lxml = resp.content
                                    break
                                else:
                                    if progress_callback:
                                        progress_callback(
                                            f"⚠️ [{idx}/{len(urls)}] Empty content on attempt {attempt}, retrying...")
                                    time.sleep(random.uniform(2, 5))
                            except requests.exceptions.RequestException as e:
                                if progress_callback:
                                    progress_callback(
                                        f"❌ [{idx}/{len(urls)}] Request error on attempt {attempt}: {e!r}")
                                time.sleep(random.uniform(2, 5))
                        else:
                            raise ConnectionError(f"Failed to get content from {url} after {max_retries} retries.")

                        if not page_content:
                            raise ConnectionError(f"Failed to fetch content from {url} using {engine} engine.")

                        scraped_row = self._extract_from_content(
                            page_content=page_content,
                            url=url,
                            template=template,
                            mode=mode,
                            resp_for_lxml=resp_for_lxml
                        )
                        if progress_callback:
                            progress_callback(f"✅ [{idx}/{len(urls)}] {url} scraped")

                except Exception as e:
                    error_row = {"url": url, "error": repr(e)}
                    self._log_error_to_file(error_log_file, url, repr(e))
                    if progress_callback:
                        progress_callback(f"❌ [{idx}/{len(urls)}] {url} error: {repr(e)}")

                # --- START: Add to results and batch ---
                if scraped_row:
                    #results.append(scraped_row)
                    if mode == "urls_only":
                        batch_links.extend(scraped_row.get("urls", []))
                    else:
                        batch_results.append(scraped_row)
                elif error_row:
                    #results.append(error_row)
                    if mode != "urls_only":
                        batch_results.append(error_row)
                # --- END: Add to results and batch ---

                # --- START: Batch Save Logic ---
                if (idx % 100 == 0 or idx == num_urls) and not (cancel_flag and cancel_flag()):
                    if progress_callback:
                        progress_callback(f"💾 Saving batch... (up to URL {idx}/{num_urls})")
                    try:
                        if mode == "urls_only":
                            self._save_batch_urls(batch_links, output_file, progress_callback)
                            batch_links.clear()
                        elif mode == "text_only":
                            self._save_batch_json(batch_results, json_file, progress_callback)
                            batch_results.clear()
                        elif mode == "text_metadata":
                            self._save_batch_metadata(batch_results, template, export_folder, main_tag_keys,
                                                      xlsx_file, json_file, progress_callback)
                            batch_results.clear()
                    except Exception as e:
                        log_msg = f"❌ CRITICAL: Failed to save batch! {e!r}"
                        logging.error(log_msg)
                        if progress_callback:
                            # --- This is the fix ---
                            progress_callback(log_msg)
                # --- END: Batch Save Logic ---

        # --- END REFACTORED LOGIC ---

        # --- START: Remove old save logic ---
        # The entire "Save output" block is GONE.
        # --- END: Remove old save logic ---

        # --- START: Determine final output path for message ---
        final_output_path = output_file
        if mode == "text_metadata":
            final_output_path = export_folder
        elif mode == "text_only":
            final_output_path = json_file
        # --- END: Determine final output path for message ---

        return {"status": "ok", "filename": final_output_path, "count": len(results), "results": results,
                "template": template}




# ----------------------------
# Flet GUI (Frontend)
# ----------------------------
class ScraperApp(ft.Column):
    def __init__(self, page: ft.Page):
        super().__init__()
        self.page = page
        self.current_step = 1
        self.expand = True
        self.is_running = False
        self._cancel_scraping = False

        # --- Backend and State ---
        self.scraper = Scraper(rotate_user_agent=True, min_delay=1.0, max_delay=3.0)
        self.template_path_str = None
        self.template_content = None
        self.template_tags = []
        self.scrape_script = ""  # RENAMED from pre_scrape_script
        self.visual_script_data = []
        self.selected_main_tag_keys = []
        self.cookie_file_path = None
        self.temp_cookie_data = None

        # Initialize all controls
        self.initialize_controls()

        # --- File Picker Setup ---
        self.template_file_picker = ft.FilePicker(on_result=self.on_template_select_result)
        self.urls_file_picker = ft.FilePicker(on_result=self.on_urls_file_load_result)

        self.cookie_load_picker = ft.FilePicker(on_result=self.on_cookie_load_result)
        self.cookie_save_picker = ft.FilePicker(on_result=self.on_cookie_save_result)

        self.page.overlay.extend([
            self.template_file_picker,
            self.urls_file_picker,
            self.cookie_load_picker,  # --- NEW ---
            self.cookie_save_picker  # --- NEW ---
        ])

        self.controls.extend([
            self.create_stepper(),
            ft.Divider(height=10),
            self.get_content_for_step(),
            ft.Divider(height=10),
            self.create_navigation_buttons(),
        ])

    def initialize_controls(self):
        # --- Step 1 ---
        self.template_path_text = ft.Text("No template selected.", italic=True, color="grey")
        self.template_button = ft.PopupMenuButton(
            items=[
                ft.PopupMenuItem(text="Select Existing Template...", icon=ft.Icons.FOLDER_OPEN,
                                 on_click=self.select_template_click),
                ft.PopupMenuItem(text="Create New Template...", icon=ft.Icons.ADD, on_click=self.create_template_click),
            ],
            content=ft.Row([ft.Icon(ft.Icons.DESCRIPTION), ft.Text("Scraping Template")])
        )

        # --- Step 2 ---
        self.urls_field = ft.TextField(label="Paste URLs Here (one per line)", multiline=True, min_lines=20,
                                       max_lines=20, border=ft.InputBorder.OUTLINE, expand=True)
        self.load_urls_button = ft.IconButton(
            icon=ft.Icons.UPLOAD_FILE, tooltip="Load URLs from .txt file",
            on_click=lambda _: self.urls_file_picker.pick_files(
                dialog_title="Select a TXT file with URLs", allowed_extensions=["txt"], allow_multiple=False, initial_directory=OUTPUT_DIR
            )
        )

        # --- Step 3 ---
        self.output_name_field = ft.TextField(label="Output Name (for file or folder)", value="output", expand=True)

        self.engine_menu = ft.Dropdown(
            options=[ft.DropdownOption(e) for e in ["Requests (faster)", "Playwright (customizable)"]],
            value="Requests (faster)",
            label="Choose Scraping Engine", on_change=self.engine_changed
        )
        self.headless_cb = ft.Checkbox(label="Run Headless (invisible browser)", value=True)
        self.playwright_actions_btn = ft.ElevatedButton("Playwright Script...",
                                                         on_click=self.open_playwright_script_editor) # RENAMED

        # --- NEW: Cookie UI Controls ---
        self.save_cookies_btn = ft.ElevatedButton(
            "Save Login Session...",
            icon=ft.Icons.SAVE,
            on_click=self.save_cookies_click,
            tooltip="Launch browser to log in and save session cookies"
        )
        self.load_cookies_btn = ft.ElevatedButton(
            "Load Login Session...",
            icon=ft.Icons.FOLDER_OPEN,
            tooltip="Load a previously saved session file (.json)",
            on_click=lambda _: self.cookie_load_picker.pick_files(
                dialog_title="Select Cookie JSON File", allowed_extensions=["json"], allow_multiple=False,
                initial_directory=COOKIE_DIR  # <-- ADDED
            )
        )
        self.cookie_file_text = ft.Text("No session loaded.", italic=True, color="grey")
        self.cookie_file_text = ft.Text("No session loaded.", italic=True, color="grey", expand=True)
        # --- END NEW ---

        self.playwright_options_card = ft.Card(
            visible=False,
            content=ft.Container(
                ft.Column([
                    ft.Text("Playwright Options", style=ft.TextThemeStyle.TITLE_MEDIUM),
                    self.headless_cb, self.playwright_actions_btn,

                    ft.Divider(height=10),
                    ft.Text("Login Session", style=ft.TextThemeStyle.TITLE_MEDIUM),
                    ft.Row(
                        [self.load_cookies_btn, self.cookie_file_text],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER
                    ),
                    ft.Text(
                        "Click 'Load Login Session' to load cookies with saved login session.",
                        size=11,
                        italic=True,
                        color=ft.Colors.GREY_700
                    ),
                    self.save_cookies_btn,
                    ft.Text(
                        "Click 'Save Login Session' to log in. Close the browser to save.",
                        size=11,
                        italic=True,
                        color=ft.Colors.GREY_700
                    )
                ]), padding=15
            )
        )
        self.mode_menu = ft.Dropdown(
            options=[
                ft.DropdownOption("Scrap text (JSON)"),
                ft.DropdownOption("Scrap URLs (TXT)"),
                ft.DropdownOption("Scrap text & metadata (Export)")
            ],
            value="Scrap text (JSON)",
            label="Output Mode",
        )
        self.run_button = ft.ElevatedButton(
            "Run Scraper", icon=ft.Icons.PLAY_ARROW_ROUNDED, height=50,
            style=ft.ButtonStyle(bgcolor=ft.Colors.GREEN_700, color=ft.Colors.WHITE),
            on_click=self.run_scraper_click
        )

        # --- Step 4 (Logs) ---
        self.cancel_button = ft.ElevatedButton(
            "Cancel Scraping", height=50, icon=ft.Icons.CANCEL,
            on_click=self.cancel_scraping_click,
            style=ft.ButtonStyle(bgcolor=ft.Colors.RED_700, color=ft.Colors.WHITE),
            visible=False
        )
        self.log_field = ft.TextField(
            label="Logs", multiline=True, read_only=True, min_lines=20, max_lines=20,
            border=ft.InputBorder.OUTLINE, expand=True,
            value="Welcome! Select a template to begin.\n"
        )

    def on_cookie_load_result(self, e: ft.FilePickerResultEvent):
        """Called when user selects a cookie file to load."""
        if not e.files:
            self.log("ℹ️ Cookie file selection cancelled.")
            return

        self.cookie_file_path = e.files[0].path
        self.cookie_file_text.value = os.path.basename(self.cookie_file_path)
        self.cookie_file_text.italic = False
        self.cookie_file_text.color = "black"
        self.log(f"✅ Session cookie file loaded: {self.cookie_file_path}")
        self.update()

    def save_cookies_click(self, e):
        """Launches the browser for the user to log in."""
        if not PLAYWRIGHT_AVAILABLE:
            self.log("❌ Playwright must be installed to save cookies.")
            return

        # Get start URL (first URL from list, or google)
        start_url = "https://google.com"
        if self.urls_field.value and self.urls_field.value.strip():
            start_url = self.urls_field.value.strip().splitlines()[0]

        # Run the browser part in a thread to not freeze the UI
        thread = threading.Thread(
            target=self._save_cookies_task,
            args=(start_url,),  # No longer passes a save path
            daemon=True
        )
        thread.start()

    def on_cookie_save_result(self, e: ft.FilePickerResultEvent):
        """
        Called after user selects *where* to save the file.
        This now saves the data from self.temp_cookie_data.
        """
        if not e.path:
            self.log("ℹ️ Save session cancelled.")
            self.temp_cookie_data = None  # Clear temp data
            return

        save_path = e.path
        if not save_path.endswith(".json"):
            save_path += ".json"

        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(self.temp_cookie_data, f, indent=2)

            self.log(f"✅ Session saved to {os.path.basename(save_path)}")

            # --- Bonus: Auto-load the file we just saved ---
            self.cookie_file_path = save_path
            self.cookie_file_text.value = os.path.basename(self.cookie_file_path)
            self.cookie_file_text.italic = False
            self.cookie_file_text.color = "black"
            self.log(f"ℹ️ Session auto-loaded for this run.")
            self.update()

        except Exception as ex:
            self.log(f"❌ Error writing session file: {ex}")
        finally:
            self.temp_cookie_data = None  # Clear temp data

    def _trigger_cookie_save_dialog(self):
        """
        Runs on the main thread. Called by _save_cookies_task.
        Opens the 'Save File' dialog.
        """
        if self.temp_cookie_data is None:
            self.log("❌ No session data was captured. Cannot save.")
            return

        self.cookie_save_picker.save_file(
            dialog_title="Save Session As",
            file_name="session.json",
            allowed_extensions=["json"]
        )

    def _save_cookies_task(self, start_url):
        """
        Runs in a thread. Launches Playwright, waits for user to close
        browser, then triggers the save dialog.
        """
        if not PLAYWRIGHT_AVAILABLE:
            return

        try:
            self.log("🚀 Launching browser. Please log in to the website...")
            self.log("🔴 IMPORTANT: When you are finished, CLOSE THE BROWSER WINDOW to save your session.")

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context()
                page = context.new_page()

                page.goto(start_url, timeout=0)
                page.wait_for_event("close", timeout=0)

                self.log("Browser closed. Preparing to save session...")
                session_data = context.storage_state()
                self.temp_cookie_data = session_data

                browser.close()  # Make sure browser is fully closed first

            # --- THE FIX ---
            # Now that the browser is closed, call save_file directly.
            # Flet will handle this cross-thread call and open the dialog.
            if self.temp_cookie_data:
                self.cookie_save_picker.save_file(
                    dialog_title="Save Session As",
                    file_name="session.json",
                    allowed_extensions=["json"],
                    initial_directory=COOKIE_DIR  # <-- ADDED
                )
            else:
                self.log("❌ No session data was captured. Cannot save.")
            # --- END FIX ---

        except Exception as ex:
            self.log(f"❌ Error during cookie saving: {ex}")

    def log(self, message: str):
        """Thread-safe method to append messages to the log field."""
        try:
            # --- START FIX: Rolling log to prevent MemoryError ---
            max_lines = 500  # Keep the last 500 lines

            # Get current lines, split by newline
            current_lines = self.log_field.value.splitlines()

            # Get the last (max_lines - 1) lines
            if len(current_lines) > max_lines:
                trimmed_lines = current_lines[-(max_lines - 1):]
            else:
                trimmed_lines = current_lines

            # Add the new message
            trimmed_lines.append(message)

            # Re-join and set the new value
            self.log_field.value = "\n".join(trimmed_lines)
            # --- END FIX ---

            self.page.update()
        except Exception as e:
            # If this fails, the Flet UI is dead or unresponsive.
            # We MUST catch this exception, or the whole app will crash.
            # We print to the console as a fallback.
            print(f"--- FLET UI LOGGING FAILED ---")
            print(f"Original Message: {message}")
            print(f"Error: {e!r}")
            print(f"-------------------------------")

    # --- UI Building Methods ---
    def show_view(self):
        """Clears and rebuilds the entire UI based on the current step."""
        self.controls.clear()
        self.controls.extend([
            self.create_stepper(),
            ft.Divider(height=10),
            self.get_content_for_step(),
            ft.Divider(height=10),
            self.create_navigation_buttons(),
        ])
        self.update()

    def create_stepper(self):
        steps = ["Template", "URLs", "Configuration", "Scraping"]
        icons = [ft.Icons.DESCRIPTION, ft.Icons.LINK, ft.Icons.TUNE, ft.Icons.TERMINAL]

        step_controls = []
        for i, (label, icon) in enumerate(zip(steps, icons), 1):
            is_active = self.current_step == i
            is_clickable = not self.is_running and i != 4  # Can't click to logs

            step_controls.append(
                ft.Container(
                    content=ft.Row([
                        ft.Icon(icon),
                        ft.Text(label, weight=ft.FontWeight.BOLD if is_active else ft.FontWeight.NORMAL)
                    ]),
                    padding=ft.padding.symmetric(horizontal=20, vertical=10),
                    border_radius=ft.border_radius.all(20),

                    data=i,
                    on_click=self.handle_step_click if is_clickable else None,
                    on_hover=self.handle_step_hover if is_clickable else None,
                    opacity=1 if is_clickable else 0.5,
                )
            )
            if i < len(steps):
                step_controls.append(ft.Divider(height=20, thickness=2))

        return ft.Row(step_controls, alignment=ft.MainAxisAlignment.CENTER)

    def get_content_for_step(self):
        """Returns the main content Column for the current step."""
        # Disable controls if scraper is running
        is_disabled = self.is_running
        is_playwright = self.engine_menu.value == "Playwright (customizable)"
        self.template_button.disabled = is_disabled
        self.urls_field.disabled = is_disabled
        self.load_urls_button.disabled = is_disabled
        self.output_name_field.disabled = is_disabled
        self.engine_menu.disabled = is_disabled
        self.headless_cb.disabled = not is_playwright or self.is_running
        self.playwright_actions_btn.disabled = not is_playwright or self.is_running
        self.mode_menu.disabled = is_disabled
        self.run_button.visible = not is_disabled
        self.cancel_button.visible = is_disabled

        if self.current_step == 1:
            return ft.Container(
                # The Column is the content of our styled box
                content=ft.Column([
                    ft.Text("Step 1: Select a scraper template file or create a new one.", size=18),
                    ft.Row([self.template_button, self.template_path_text],
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ], spacing=30, horizontal_alignment=ft.CrossAxisAlignment.CENTER),

                # --- Styling Properties for the Container ---
                padding=10,  # Add 30 pixels of space inside the container
                border_radius=10,  # Round the corners

                expand=True,  # Make the container fill the available width
            )
        elif self.current_step == 2:
            return ft.Container(
                # The Column is the content of our styled box
                content=ft.Column([
                    ft.Text("Step 2: Paste URLs to scrape or load from a file.", size=18),
                    ft.Stack([self.urls_field, ft.Row([self.load_urls_button], top=5, right=5)])
                ], spacing=30, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                # --- Styling Properties for the Container ---
                padding=10,  # Add 30 pixels of space inside the container
                border_radius=10,  # Round the corners

                expand=True,  # Make the container fill the available width
            )

        elif self.current_step == 3:
            return ft.Container(
                content=ft.Column([
                    # --- MODIFICATION START ---
                    # Wrap the Text in a Row and center it
                    ft.Row(
                        [ft.Text("Step 3: Configure engine and output settings.", size=18)],
                        alignment=ft.MainAxisAlignment.CENTER
                    ),
                    ft.Row([self.output_name_field]),
                    self.engine_menu,
                    self.playwright_options_card,
                    self.mode_menu,
                    self.run_button,
                    # Keep this as STRETCH to affect all other controls
                ], spacing=15 if is_playwright else 30, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                # --- Styling Properties for the Container ---
                padding=10,  # Add 30 pixels of space inside the container
                border_radius=10,  # Round the corners
                expand=True,  # Make the container fill the available width
            )


        elif self.current_step == 4:
            return ft.Container(
                content=ft.Column([self.log_field, self.cancel_button], spacing=20,
                                  horizontal_alignment=ft.CrossAxisAlignment.STRETCH), padding=10, border_radius=10,
                expand=True)

        return ft.Container()  # Fallback

    def create_navigation_buttons(self):
        nav_row = ft.Row(
            [
                ft.ElevatedButton("Back", icon=ft.Icons.ARROW_BACK, on_click=self.prev_step,
                                  disabled=(self.current_step == 1 or self.is_running)),

                ft.ElevatedButton("Next", icon=ft.Icons.ARROW_FORWARD, on_click=self.next_step,
                                  disabled=(self.current_step >= 3 or self.is_running)),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN
        )
        return nav_row

    # --- Step Navigation and Event Handlers ---
    def handle_step_click(self, e):
        self.current_step = e.control.data
        self.show_view()

    def handle_step_hover(self, e):
        e.control.page.mouse_cursor = ft.MouseCursor.CLICK if e.data == "true" else ft.MouseCursor.BASIC
        e.control.page.update()

    def next_step(self, e):
        if self.current_step < 3: self.current_step += 1
        self.show_view()

    def prev_step(self, e):
        if self.current_step > 1: self.current_step -= 1
        self.show_view()

    def engine_changed(self, e):
        is_playwright = e.control.value == "Playwright (customizable)"
        self.playwright_options_card.visible = is_playwright
        self.headless_cb.disabled = not is_playwright or self.is_running
        self.playwright_actions_btn.disabled = not is_playwright or self.is_running
        self.update()
        self.show_view()

    # --- File/Dialog Handlers ---
    def create_template_click(self, e):
        def launch():
            TemplateCreator_flet.run_template_creator()

        threading.Thread(target=launch, daemon=True, name="MainThread").start()
        self.log("✅ Template Creator launched in background.")

    def select_template_click(self, e):
        self.template_file_picker.pick_files(
            dialog_title="Select Scraper Template", allowed_extensions=["json"], allow_multiple=False,
            initial_directory=TEMPLATE_DIR
        )

    def on_template_select_result(self, e: ft.FilePickerResultEvent):
        if not e.files:
            self.log("ℹ️ Template selection cancelled.")
            return

        self.template_path_str = e.files[0].path
        self.template_path_text.value = os.path.basename(self.template_path_str)
        self.template_path_text.italic = False
        self.template_path_text.color = "black"
        self.log(f"✅ Template selected: {self.template_path_str}")

        try:
            self.template_content = self.scraper.read_template(self.template_path_str)
            template_data = json.loads(self.template_content)
            self.template_tags = list(template_data.get("selectors", {}).keys())
        except Exception as ex:
            self.log(f"❌ Error loading template: {ex}")
            self.template_path_str = None
            self.template_content = None
            self.template_tags = []

        self.update()

    def on_urls_file_load_result(self, e: ft.FilePickerResultEvent):
        if not e.files:
            self.log("ℹ️ URL file selection cancelled.")
            return

        try:
            file_path = e.files[0].path
            with open(file_path, 'r', encoding='utf-8') as f:
                self.urls_field.value = f.read()
            self.log(f"✅ URLs loaded from {os.path.basename(file_path)}")
        except Exception as ex:
            self.log(f"❌ Error reading URL file: {ex}")
        self.update()

    def open_playwright_script_editor(self, e):
        """Opens a modal dialog to edit the Playwright scrape script."""

        # --- Controls ---

        # 1. Script Tab's text field
        script_field = ft.TextField(
            value=self.scrape_script,
            multiline=True,
            min_lines=20,
            max_lines=20,
            border=ft.InputBorder.OUTLINE,
        )

        # 2. Visual Tab's UI
        visual_script_canvas = ft.Column(expand=True, spacing=5, scroll=ft.ScrollMode.AUTO)

        # --- Helper: Code Generator (Now Recursive) ---
        def generate_code_recursive(data_list, indent_level, imports_set):
            """Generates Python code from the nested block data."""
            code_lines = []
            indent = "    " * indent_level

            for block in data_list:
                block_type = block.get("type")

                # --- Helper function to build the locator string ---
                def build_locator_string(by, selector_value, text_value):
                    if by == "text":
                        locator_str = f"page.locator(f\"*:has-text({repr(text_value)})\").first"
                        log_msg = f"\"text \" + {repr(text_value)}"

                    elif by == "both":
                        stripped_selector = selector_value.strip()
                        if stripped_selector.startswith(("/", "(", "..")):
                            selector_part = f"\"xpath=\" + {repr(selector_value)}"
                        else:
                            selector_part = f"{repr(selector_value)}"
                        locator_str = f"page.locator({selector_part} + f\":has-text({repr(text_value)})\").first"
                        log_msg = f"\"selector \" + {repr(selector_value)} + \" with text \" + {repr(text_value)}"

                    else:  # default to 'selector'
                        stripped_value = selector_value.strip()
                        if stripped_value.startswith(("/", "(", "..")):
                            locator_str = f"page.locator(\"xpath=\" + {repr(selector_value)})"
                            log_msg = f"\"xpath \" + {repr(selector_value)}"
                        else:
                            locator_str = f"page.locator({repr(selector_value)})"
                            log_msg = f"\"selector \" + {repr(selector_value)}"

                    return locator_str, log_msg

                # --- End helper function ---

                if block_type == "click":
                    by = block.get("by", "selector")
                    selector_val = block.get("selector_value", "")
                    text_val = block.get("text_value", "")
                    locator_str, log_msg = build_locator_string(by, selector_val, text_val)

                    code_lines.append(f"{indent}log(\"🖱️ Clicking \" + {log_msg})")
                    code_lines.append(f"{indent}{locator_str}.click(force=True)")

                elif block_type == "select_form":
                    by = block.get("by", "selector")
                    selector_val = block.get("selector_value", "")
                    text_val = block.get("text_value", "")
                    locator_str, log_msg = build_locator_string(by, selector_val, text_val)
                    option_text = block.get("option_text", "")

                    code_lines.append(f"{indent}log(f\"⤵️ Selecting option '{option_text}' from \" + {log_msg})")
                    code_lines.append(f"{indent}{locator_str}.select_option(label={repr(option_text)})")

                elif block_type == "input_text":
                    by = block.get("by", "selector")
                    selector_val = block.get("selector_value", "")
                    text_val = block.get("text_value", "")
                    locator_str, log_msg = build_locator_string(by, selector_val, text_val)
                    input_text = block.get("input_text", "")

                    code_lines.append(f"{indent}log(f\"⌨️ Typing '{input_text}' into \" + {log_msg})")
                    code_lines.append(f"{indent}{locator_str}.fill({repr(input_text)})")

                # --- NEW BLOCK: Wait For Element ---
                elif block_type == "wait_for_element":
                    # 1. Find the element
                    by = block.get("by", "selector")
                    selector_val = block.get("selector_value", "")
                    text_val = block.get("text_value", "")
                    locator_str, log_msg = build_locator_string(by, selector_val, text_val)

                    # 2. Get the condition to wait for
                    condition = block.get("condition", "visible")  # "visible", "hidden", "enabled", "disabled"

                    # 3. Generate code
                    code_lines.append(f"{indent}log(f\"⏳ Waiting for \" + {log_msg} + f\" to be {condition}...\")")
                    # Use Playwright's built-in wait. Using 30s default timeout.
                    code_lines.append(f"{indent}{locator_str}.wait_for(state={repr(condition)}, timeout=30000)")
                # --- END NEW BLOCK ---

                elif block_type == "wait":
                    duration_str = block.get("duration", "1").strip()
                    if "-" in duration_str:
                        imports_set.add("import random")
                        parts = duration_str.split("-")
                        if len(parts) == 2:
                            try:
                                min_val = float(parts[0].strip())
                                max_val = float(parts[1].strip())
                                code_lines.append(f"{indent}wait_time = random.uniform({min_val}, {max_val})")
                                code_lines.append(
                                    f"{indent}log(f'⏳ Waiting for {{wait_time:.2f}}s (randomly from {min_val}-{max_val})')")
                                code_lines.append(f"{indent}time.sleep(wait_time)")
                            except ValueError:
                                code_lines.append(
                                    f"{indent}log(f'⚠️ Invalid wait range: {repr(duration_str)}. Defaulting to 1s.')")
                                code_lines.append(f"{indent}time.sleep(1)")
                        else:
                            code_lines.append(
                                f"{indent}log(f'⚠️ Invalid wait range format: {repr(duration_str)}. Defaulting to 1s.')")
                            code_lines.append(f"{indent}time.sleep(1)")
                    else:
                        try:
                            wait_time = float(duration_str)
                            code_lines.append(f"{indent}log(f'⏳ Waiting for {wait_time}s')")
                            code_lines.append(f"{indent}time.sleep({wait_time})")
                        except ValueError:
                            code_lines.append(
                                f"{indent}log(f'⚠️ Invalid wait duration: {repr(duration_str)}. Defaulting to 1s.')")
                            code_lines.append(f"{indent}time.sleep(1)")

                elif block_type == "scrape":
                    code_lines.append(f"{indent}log('📊 Scraping data...')")
                    code_lines.append(f"{indent}scrape()")

                elif block_type == "scroll":
                    pixels = block.get("pixels", 500)
                    code_lines.append(f"{indent}log(f'↕️ Scrolling down {pixels}px')")
                    code_lines.append(f"{indent}page.mouse.wheel(0, {pixels})")

                elif block_type == "if_condition":
                    by = block.get("by", "selector")
                    selector_val = block.get("selector_value", "")
                    text_val = block.get("text_value", "")
                    locator_str, log_msg = build_locator_string(by, selector_val, text_val)

                    condition = block.get("condition", "is_visible")
                    condition_map = {
                        "is_visible": {"py_check": f"{locator_str}.is_visible()", "log_msg": "is visible"},
                        "is_not_visible": {"py_check": f"not {locator_str}.is_visible()", "log_msg": "is NOT visible"},
                        "is_enabled": {"py_check": f"{locator_str}.is_enabled()", "log_msg": "is enabled (active)"},
                        "is_disabled": {"py_check": f"{locator_str}.is_disabled()",
                                        "log_msg": "is disabled (inactive)"},
                    }
                    check = condition_map.get(condition, condition_map["is_visible"])

                    code_lines.append(f"{indent}log(\"❔ Checking if \" + {log_msg} + f\" {check['log_msg']}...\")")
                    code_lines.append(f"{indent}if {check['py_check']}:")

                    if block.get("children"):
                        code_lines.extend(generate_code_recursive(block["children"], indent_level + 1, imports_set))
                    else:
                        code_lines.append(f"{indent}    pass # No actions added to 'if' block")

                elif block_type == "break_loop":
                    code_lines.append(f"{indent}log('➡️ Breaking loop...')")
                    code_lines.append(f"{indent}break")

                elif block_type == "repeat":
                    code_lines.append(f"{indent}log('🔁 Starting loop...')")
                    code_lines.append(f"{indent}while True:")
                    code_lines.append(f"{indent}    if is_cancelled():")
                    code_lines.append(f"{indent}        log('⚠️ Loop cancelled by user.')")
                    code_lines.append(f"{indent}        break")
                    if block.get("children"):
                        code_lines.extend(generate_code_recursive(block["children"], indent_level + 1, imports_set))
                    else:
                        code_lines.append(f"{indent}    pass # Loop is empty")
                    code_lines.append(f"{indent}    time.sleep(0.1)")

            return code_lines

        BLOCK_COLORS = {
            "click": ft.Colors.INDIGO_50,
            "select_form": ft.Colors.ORANGE_50,
            "input_text": ft.Colors.LIME_100,
            "wait": ft.Colors.CYAN_50,
            "wait_for_element": ft.Colors.AMBER_50,  # --- NEW ---
            "scrape": ft.Colors.GREEN_50,
            "scroll": ft.Colors.BLUE_100,
            "if_condition": ft.Colors.PURPLE_50,
            "repeat": ft.Colors.DEEP_PURPLE_100,
            "break_loop": ft.Colors.RED_100,
            "default": ft.Colors.WHITE
        }

        # --- Helper: UI Builder (Now Recursive) ---
        def build_block_ui(block_data, parent_list, is_in_loop=False):
            """Recursively creates the Flet UI for a single block and its children."""

            block_type = block_data.get("type", "Unknown")
            block_color = BLOCK_COLORS.get(block_type, BLOCK_COLORS["default"])

            # --- Define controls for this block ---
            title_text = f"{block_type.replace('_', ' ').title()}"
            if block_type == "if_condition":
                title_text = "If (Condition)"
            elif block_type == "break_loop":
                title_text = "Break Loop"
            elif block_type == "select_form":
                title_text = "Select from Form"
            elif block_type == "input_text":
                title_text = "Input Text"
            elif block_type == "wait":
                title_text = "Wait"  # --- MODIFIED: Clarified title ---
            elif block_type == "wait_for_element":  # --- NEW ---
                title_text = "Wait For Element"

            title = ft.Text(title_text, style=ft.TextThemeStyle.TITLE_MEDIUM)

            def delete_block_handler(e):
                parent_list.remove(block_data)
                build_visual_canvas()  # Refresh entire UI

            delete_btn = ft.IconButton(
                icon=ft.Icons.DELETE_FOREVER,
                icon_color=ft.Colors.RED_400,
                on_click=delete_block_handler,
                tooltip="Delete block"
            )

            # --- Assemble the block's card ---
            block_content = [
                ft.Row([title, delete_btn], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ]

            if block_type == "if_condition":
                def on_condition_change(e):
                    block_data["condition"] = e.control.value

                condition_dropdown = ft.Dropdown(
                    label="Condition Type",
                    value=block_data.get("condition", "is_visible"),
                    on_change=on_condition_change,
                    options=[
                        ft.DropdownOption("is_visible", "Is Visible"),
                        ft.DropdownOption("is_not_visible", "Is Not Visible"),
                        ft.DropdownOption("is_enabled", "Is Enabled (Active)"),
                        ft.DropdownOption("is_disabled", "Is Disabled (Inactive)"),
                    ],
                    expand=True
                )
                block_content.append(condition_dropdown)
                block_content.append(ft.Divider(height=5, color="transparent"))

            if block_type == "wait":
                duration_field = ft.TextField(
                    label="Duration (e.g., '2', '0.5', or '1-3')",
                    value=block_data.get("duration", "1"),
                    on_change=lambda e: block_data.update({"duration": e.control.value}),
                    prefix_icon=ft.Icons.TIMER
                )
                block_content.append(duration_field)

            # --- Element selectors ---
            # --- MODIFIED: Added 'wait_for_element' ---
            if block_type in ["click", "if_condition", "select_form", "input_text", "wait_for_element"]:
                selector_field = ft.TextField(
                    label="Selector (CSS or XPath)",
                    value=block_data.get("selector_value", ""),
                    on_change=lambda e: block_data.update({"selector_value": e.control.value})
                )
                text_field = ft.TextField(
                    label="Text Content",
                    value=block_data.get("text_value", ""),
                    on_change=lambda e: block_data.update({"text_value": e.control.value})
                )

                def on_radio_change(e):
                    block_data["by"] = e.control.value
                    by_val = e.control.value
                    selector_field.visible = (by_val == "selector" or by_val == "both")
                    text_field.visible = (by_val == "text" or by_val == "both")
                    selector_field.update()
                    text_field.update()

                radio_group = ft.RadioGroup(
                    value=block_data.get("by", "selector"),
                    on_change=on_radio_change,
                    content=ft.Row([
                        ft.Radio(value="selector", label="By Selector"),
                        ft.Radio(value="text", label="By Text"),
                        ft.Radio(value="both", label="By Selector & Text")
                    ])
                )

                current_by = block_data.get("by", "selector")
                selector_field.visible = (current_by == "selector" or current_by == "both")
                text_field.visible = (current_by == "text" or current_by == "both")

                block_content.append(radio_group)
                block_content.append(selector_field)
                block_content.append(text_field)

                # --- Specific field for 'select_form' ---
                if block_type == "select_form":
                    block_content.append(ft.Divider(height=5, color="transparent"))
                    option_text_field = ft.TextField(
                        label="Option to Select (Exact Text)",
                        value=block_data.get("option_text", ""),
                        on_change=lambda e: block_data.update({"option_text": e.control.value}),
                        prefix_icon=ft.Icons.LIST_ALT
                    )
                    block_content.append(option_text_field)

                # --- Specific field for 'input_text' ---
                if block_type == "input_text":
                    block_content.append(ft.Divider(height=5, color="transparent"))
                    input_text_field = ft.TextField(
                        label="Text to Input",
                        value=block_data.get("input_text", ""),
                        on_change=lambda e: block_data.update({"input_text": e.control.value}),
                        prefix_icon=ft.Icons.EDIT
                    )
                    block_content.append(input_text_field)

                # --- NEW: Specific fields for 'wait_for_element' ---
                if block_type == "wait_for_element":
                    block_content.append(ft.Divider(height=5, color="transparent"))

                    def on_condition_change(e):
                        block_data["condition"] = e.control.value

                    condition_dropdown = ft.Dropdown(
                        label="Wait Condition",
                        value=block_data.get("condition", "visible"),
                        on_change=on_condition_change,
                        options=[
                            ft.DropdownOption("visible", "Is Visible (appears)"),
                            ft.DropdownOption("hidden", "Is Hidden (disappears)"),
                            ft.DropdownOption("enabled", "Is Enabled (is clickable)"),
                            ft.DropdownOption("disabled", "Is Disabled (is grayed out)"),
                        ],
                        prefix_icon=ft.Icons.HOURGLASS_TOP
                    )
                    block_content.append(condition_dropdown)
                # --- END NEW ---

            # --- UI for nested children ---
            children_container = ft.Container(
                padding=ft.padding.only(left=15, top=10),
                border=ft.border.only(left=ft.BorderSide(2, ft.Colors.GREY_300))
            )

            if "children" in block_data:
                children_column = ft.Column(spacing=5, controls=[])
                current_is_in_loop = is_in_loop or block_data.get("type") == "repeat"

                for child_block in block_data["children"]:
                    children_column.controls.append(
                        build_block_ui(child_block, block_data["children"], is_in_loop=current_is_in_loop)
                    )
                children_column.controls.append(
                    create_add_block_dropdown(
                        block_data["children"],
                        parent_block_type=block_data.get("type"),
                        is_in_loop=current_is_in_loop
                    )
                )

                children_container.content = children_column
                block_content.append(children_container)

            return ft.Card(ft.Container(ft.Column(block_content), padding=10), color=block_color)

        def create_add_block_dropdown(parent_list, parent_block_type=None, is_in_loop=False):
            """
            Factory to create a new 'Add Block' control.
            """

            # --- 1. Define the block creation logic ---
            def _create_and_add_block(block_name):
                if not block_name:
                    return

                new_block = {}
                if block_name == "Click Element":
                    new_block = {"type": "click", "by": "selector", "selector_value": "", "text_value": ""}

                elif block_name == "Select from Form":
                    new_block = {
                        "type": "select_form",
                        "by": "selector",
                        "selector_value": "",
                        "text_value": "",
                        "option_text": ""
                    }

                elif block_name == "Input Text":
                    new_block = {
                        "type": "input_text",
                        "by": "selector",
                        "selector_value": "",
                        "text_value": "",
                        "input_text": ""
                    }

                # --- NEW ---
                elif block_name == "Wait For Element":
                    new_block = {
                        "type": "wait_for_element",
                        "by": "selector",
                        "selector_value": "",
                        "text_value": "",
                        "condition": "visible"  # default
                    }
                # --- END NEW ---

                elif block_name == "Wait":  # --- MODIFIED: Name change ---
                    new_block = {"type": "wait", "duration": "1"}

                elif block_name == "Scroll Page":
                    new_block = {"type": "scroll", "pixels": 500}
                elif block_name == "Scrape Data":
                    new_block = {"type": "scrape"}
                elif block_name == "If (Condition)":
                    new_block = {
                        "type": "if_condition",
                        "condition": "is_visible",
                        "by": "selector",
                        "selector_value": "",
                        "text_value": "",
                        "children": []
                    }
                elif block_name == "Repeat (Loop)":
                    new_block = {"type": "repeat", "children": []}
                elif block_name == "Break Loop":
                    new_block = {"type": "break_loop"}

                parent_list.append(new_block)
                build_visual_canvas()  # Refresh entire UI

            # --- 2. Define event handlers ---
            def handle_dropdown_change(e):
                _create_and_add_block(e.control.value)

            def handle_menu_item_click(e):
                _create_and_add_block(e.control.text)

            # --- 3. Dynamically build the list of available options ---
            # --- MODIFIED: Added new options ---
            option_strings = [
                "Click Element",
                "Input Text",
                "Select from Form",
                "Wait For Element",  # --- NEW ---
                "Wait",  # --- MODIFIED: Name change ---
                "Scroll Page",
                "Scrape Data",
                "If (Condition)",
                "Repeat (Loop)",
            ]

            if parent_block_type == "if_condition" and is_in_loop:
                option_strings.append("Break Loop")

            # --- 4. Return the correct control based on context ---
            if parent_block_type is None:
                # TOP LEVEL Dropdown
                return ft.Dropdown(
                    label="Add Action...",
                    on_change=handle_dropdown_change,
                    options=[ft.DropdownOption(s) for s in option_strings],
                )
            else:
                # NESTED PopupMenuButton
                menu_items = []
                for s in option_strings:
                    icon = ft.Icons.ADD  # Default icon
                    if s == "Click Element": icon = ft.Icons.MOUSE
                    if s == "Select from Form": icon = ft.Icons.ARROW_DROP_DOWN_CIRCLE
                    if s == "Input Text": icon = ft.Icons.KEYBOARD
                    if s == "Wait": icon = ft.Icons.TIMER  # --- MODIFIED: Name change ---
                    if s == "Wait For Element": icon = ft.Icons.VISIBILITY  # --- NEW ---
                    if s == "Scroll Page": icon = ft.Icons.ARROW_DOWNWARD
                    if s == "Scrape Data": icon = ft.Icons.DOWNLOAD_FOR_OFFLINE
                    if s == "If (Condition)": icon = ft.Icons.QUESTION_MARK
                    if s == "Repeat (Loop)": icon = ft.Icons.LOOP
                    if s == "Break Loop": icon = ft.Icons.STOP

                    menu_items.append(
                        ft.PopupMenuItem(text=s, icon=icon, on_click=handle_menu_item_click)
                    )

                return ft.PopupMenuButton(
                    icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                    tooltip="Add Action...",
                    items=menu_items
                )

        def build_visual_canvas():
            """Clears and re-builds the entire visual canvas from self.visual_script_data."""
            visual_script_canvas.controls.clear()

            # Build all top-level blocks
            for block in self.visual_script_data:
                visual_script_canvas.controls.append(
                    build_block_ui(block, self.visual_script_data)
                )

            # --- MOVED TO BOTTOM ---
            # Add a divider before the button for spacing
            visual_script_canvas.controls.append(ft.Divider(height=10))
            # Top-level "Add" button
            visual_script_canvas.controls.append(
                create_add_block_dropdown(self.visual_script_data)
            )
            # --- END MOVE ---

            # --- MODIFIED ---
            # We must check if the canvas is on the page *before* trying to scroll it.
            # scroll_to() automatically calls update(), so this is all we need.
            if visual_script_canvas.page:
                visual_script_canvas.scroll_to(offset=-1)

        # --- Visual Builder Root UI ---
        visual_builder_ui = ft.Column(
            [
                ft.Text(
                    "Note: Manually editing the 'Script' tab is one-way. "
                    "Changes made there will NOT update this visual builder.",
                    italic=True,
                    color=ft.Colors.GREY
                ),
                visual_script_canvas
            ],
            tight=True,
            width=600,
            height=450,
        )

        # --- The Tabs control ---
        tabs_control = ft.Tabs(
            selected_index=0,
            width=1000,
            tabs=[
                ft.Tab(
                    text="Script",
                    icon=ft.Icons.CODE,
                    content=ft.Container(script_field, padding=10)
                ),
                ft.Tab(
                    text="Visual Builder",
                    icon=ft.Icons.BUILD_CIRCLE,
                    content=ft.Container(visual_builder_ui, padding=10)
                ),
            ],
        )

        # --- Dialog Setup ---
        dialog = ft.AlertDialog(
            modal=True,
            inset_padding=10,
            shape=ft.RoundedRectangleBorder(radius=3),
            content=tabs_control,
            actions_alignment=ft.MainAxisAlignment.END,
        )

        # --- Tab Sync Logic ---
        def on_tab_change(e):
            if e.control.selected_index == 0:  # User clicked "Script"
                imports_to_add = set()
                code_lines = generate_code_recursive(self.visual_script_data, 0, imports_to_add)

                generated_code = "\n".join(list(imports_to_add))
                if imports_to_add:
                    generated_code += "\n\n"
                generated_code += "\n".join(code_lines)

                script_field.value = generated_code
                script_field.update()
            elif e.control.selected_index == 1:  # User clicked "Visual"
                build_visual_canvas()

        tabs_control.on_change = on_tab_change

        # --- Dialog Actions ---
        def save_script(e):
            if tabs_control.selected_index == 0:
                self.scrape_script = script_field.value
                self.visual_script_data = []
                self.log("ℹ️ Manual script saved. (Visual builder data cleared)")
            else:
                imports_to_add = set()
                code_lines = generate_code_recursive(self.visual_script_data, 0, imports_to_add)

                generated_code = "\n".join(list(imports_to_add))
                if imports_to_add:
                    generated_code += "\n\n"
                generated_code += "\n".join(code_lines)

                self.scrape_script = generated_code
                self.log("ℹ️ Visual script generated and saved.")

            self.page.close(dialog)

        def close_dialog(e):
            self.page.close(dialog)

        dialog.actions = [
            ft.ElevatedButton("Save and Close", on_click=save_script),
            ft.TextButton("Cancel", on_click=close_dialog),
        ]

        # --- Initial Load ---
        build_visual_canvas()
        self.page.open(dialog)

    def open_main_tag_dialog(self):
        """Opens a dialog with a ReorderableListView to select tags for export."""

        # This is the correct event handler for ft.ReorderableListView
        def handle_reorder(e: ft.OnReorderEvent):
            # The logic to manually move the control in the list is required by Flet
            item_to_move = reorderable_list.controls.pop(e.old_index)
            reorderable_list.controls.insert(e.new_index, item_to_move)
            reorderable_list.update()

        # Create the ReorderableListView
        reorderable_list = ft.ReorderableListView(
            on_reorder=handle_reorder,
            height=300,  # Give it a fixed height to make it scrollable inside the dialog
        )

        # Populate the list
        for tag in self.template_tags:
            reorderable_list.controls.append(
                ft.ListTile(
                    title=ft.Checkbox(label=tag),
                    leading=ft.Icon(ft.Icons.DRAG_INDICATOR),
                )
            )

        # --- Dialog and Actions ---
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Select and Order Tags for Export"),
            content=ft.Column(
                [
                    ft.Text("Check the tags you want to export. Drag to reorder."),
                    reorderable_list,
                ]
            ),
        )

        def confirm_and_run(e):
            self.selected_main_tag_keys = []
            # Extract the selected tags from the (potentially reordered) list
            for list_tile in reorderable_list.controls:
                checkbox = list_tile.title  # The Checkbox is the title of our ListTile
                if checkbox.value:
                    self.selected_main_tag_keys.append(checkbox.label)

            if not self.selected_main_tag_keys:
                self.page.snack_bar = ft.SnackBar(ft.Text("Please select at least one tag."), bgcolor=ft.Colors.RED)
                self.page.snack_bar.open = True
                self.page.update()
                return

            self.page.close(dialog)
            self.start_scraping_thread()

        def close_dialog(e):
            self.page.close(dialog)

        dialog.actions = [
            ft.ElevatedButton("Confirm and Run", on_click=confirm_and_run),
            ft.TextButton("Cancel", on_click=close_dialog),
        ]

        self.page.open(dialog)

    # --- Scraper Control ---
    def run_scraper_click(self, e):
        """Initiates validation and starts the scraping process."""
        # --- Validation ---
        if not self.template_path_str or not self.template_content:
            self.log("❌ Please select a valid template file first.")
            self.current_step = 1;
            self.show_view()
            return
        if not self.urls_field.value.strip():
            self.log("❌ Please enter or load some URLs to scrape.")
            self.current_step = 2;
            self.show_view()
            return

        mode_map = {
            "Scrap text (JSON)": "text_only",
            "Scrap URLs (TXT)": "urls_only",
            "Scrap text & metadata (Export)": "text_metadata",
        }
        mode = mode_map.get(self.mode_menu.value)

        # If metadata mode, show tag selector first. The scraper will be started from that dialog.
        if mode == "text_metadata":
            if not self.template_tags:
                self.log("❌ Template has no tags defined for metadata export.")
                return
            self.open_main_tag_dialog()
        else:
            self.start_scraping_thread()

    def start_scraping_thread(self):
        """Sets the UI to a 'running' state and starts the background thread."""
        self.is_running = True
        self._cancel_scraping = False
        self.current_step = 4
        self.show_view()
        self.log_field.value = ""  # Clear previous logs
        self.log("🚀 Scraping process starting...")
        self.show_view()

        # Run the actual scraper logic in a separate thread
        thread = threading.Thread(target=self._scrape_task, daemon=True)
        thread.start()

    def cancel_scraping_click(self, e):
        self._cancel_scraping = True
        self.log("⚠️ Cancel request sent. Waiting for current URL to finish...")
        # The thread's 'finally' block will reset the UI state.

    def _scrape_task(self):
        """The actual workhorse method that runs in a background thread."""
        try:
            # Gather all parameters from UI controls
            urls_text = self.urls_field.value.strip()
            output_name = self.output_name_field.value.strip() or "output"
            cookie_path = self.cookie_file_path

            engine_map = {"Requests (faster)": "requests", "Playwright (customizable)": "playwright"}
            engine_key = engine_map.get(self.engine_menu.value, "requests")

            mode_map = {
                "Scrap text (JSON)": "text_only", "Scrap URLs (TXT)": "urls_only",
                "Scrap text & metadata (Export)": "text_metadata",
            }
            mode = mode_map.get(self.mode_menu.value)

            run_headless = self.headless_cb.value

            # Run the scraper from the backend class
            result = self.scraper.run_scraper_from_content(
                template_content=self.template_content,
                urls_text=urls_text,
                output_name=output_name,
                mode=mode,
                progress_callback=self.log,
                cancel_flag=lambda: self._cancel_scraping,
                engine=engine_key,
                scrape_script=self.scrape_script,  # RENAMED
                headless=run_headless,
                cookie_file_path=cookie_path,
                # --- START: Pass main tag keys ---
                main_tag_keys=self.selected_main_tag_keys
                # --- END: Pass main tag keys ---
            )

            # Process results
            if result["status"] == "ok":
                output_desc = result['filename']
                if mode == "text_metadata":
                    output_desc = f"folder '{os.path.basename(result['filename'])}'"

                # --- START: Check for error log ---
                error_log_file = os.path.join(OUTPUT_DIR, output_name + "_errors.txt")
                error_log_exists = os.path.exists(error_log_file) and os.path.getsize(error_log_file) > 0
                # --- END: Check for error log ---

                if not self._cancel_scraping:
                    self.log(f"✅ Scraping finished! Output saved to {output_desc}")
                    if error_log_exists:  # <-- ADD THIS
                        self.log(f"ℹ️ Some URLs failed. See {os.path.basename(error_log_file)} for details.")
                else:
                    self.log(f"⏹️ Scraping stopped. Partial output saved to {output_desc}")
                    if error_log_exists:  # <-- ADD THIS
                        self.log(
                            f"ℹ️ Some URLs failed before stopping. See {os.path.basename(error_log_file)} for details.")
            else:
                self.log(f"❌ Error: {result.get('message', 'Unknown error')}")

        except Exception as e:
            self.log(f"💥 A critical error occurred: {e!r}")
        finally:
            # Reset UI state regardless of success or failure
            self.is_running = False
            try:
                self.show_view()
            except Exception as e:
                # If this fails, the UI is dead. Just print to console.
                # The thread will now exit gracefully.
                print(f"--- FLET UI RESET FAILED ---")
                print(f"Error in 'finally' block: {e!r}")
                print(f"-----------------------------")


def main(page: ft.Page):
    page.title = "Scrapuj"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.DEEP_PURPLE)
    page.window.width = 700
    page.window.height = 900
    page.window.min_width = 700
    page.window.min_height = 900
    page.window.resizable = True
    app = ScraperApp(page)

    page.add(
        ft.Container(
            content=app,
            padding=20,
            expand=True,
        )
    )
    page.update()


if __name__ == "__main__":
    ft.app(target=main)