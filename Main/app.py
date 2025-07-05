#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
app.py

Versi “super-kompleks” dari SiteMirror, dengan perbaikan pada _parse_and_rewrite_css
agar tidak memicu TypeError. Sekarang skrip dapat men‐crawl dan mirror seluruh halaman
di domain tanpa terhenti hanya pada satu halaman.

Cara pakai (contoh):
    python app.py https://t2b.my.id/ \
        --output my_full_mirror \
        --max_workers 8 \
        --delay 0.5 \
        --force_render

Catatan:
- Jangan tambahkan “False” atau “True” setelah flag boolean; cukup tulis flag‐nya saja.
- Di PowerShell, gunakan backtick (`) untuk line‐continuation jika ingin memecah argumen ke beberapa baris.
"""

import argparse
import os
import re
import sys
import time
import logging
import shutil
import sqlite3
import threading
import random
import string
from urllib.parse import urlparse, urljoin, urldefrag, parse_qs
from urllib import robotparser
from xml.etree import ElementTree as ET
from logging.handlers import RotatingFileHandler

import requests
from bs4 import BeautifulSoup
import cssutils
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# ==============================================================================
#                           GLOBAL CONFIGURATIONS
# ==============================================================================

LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR, exist_ok=True)

log_formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] [%(name)s/%(funcName)s] %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("MegaMirror")
logger.setLevel(logging.DEBUG)

# Console handler (INFO+)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# File handler (DEBUG+), rotasi 5 × 5MB
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "site_mirror.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding='utf-8'
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# Supress cssutils verbose log
cssutils.log.setLevel(logging.ERROR)

# ==============================================================================
#                            UTILITY FUNCTIONS
# ==============================================================================

def random_string(length=8):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def sanitize_filename(name: str) -> str:
    if not name or name.strip() == "":
        return random_string(6) + ".html"
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', name).strip()
    sanitized = sanitized.replace(" ", "_")
    return sanitized

def normalize_url(base_url: str, link: str) -> str:
    joined = urljoin(base_url, link)
    normalized, _ = urldefrag(joined)
    parsed = urlparse(normalized)
    path = parsed.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
        normalized = f"{parsed.scheme}://{parsed.netloc}{path}"
    return normalized

def is_same_domain(root_netloc: str, target_url: str) -> bool:
    try:
        parsed = urlparse(target_url)
        return parsed.netloc.endswith(root_netloc)
    except Exception:
        return False

def parse_query_to_filename(query: str) -> str:
    try:
        params = parse_qs(query, keep_blank_values=True)
        parts = []
        for k, vs in params.items():
            for v in vs:
                parts.append(f"{sanitize_filename(k)}_{sanitize_filename(v)}")
        if parts:
            return "_".join(parts)
        else:
            return random_string(6)
    except Exception:
        return random_string(6)

# ==============================================================================
#                         DATABASE MANAGER (SQLite)
# ==============================================================================

class DBManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db_lock = threading.Lock()
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_db(self):
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS urls (
                    url TEXT PRIMARY KEY,
                    status TEXT,
                    last_fetched TIMESTAMP
                );
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS resources (
                    url TEXT PRIMARY KEY,
                    local_path TEXT
                );
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS sitemap_urls (
                    url TEXT PRIMARY KEY
                );
            """)
            conn.commit()
            conn.close()

    def add_url(self, url: str):
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            try:
                c.execute("INSERT OR IGNORE INTO urls(url, status) VALUES(?, 'pending')", (url,))
                conn.commit()
            except Exception as e:
                logger.error(f"[DB:add_url] Gagal insert URL {url}: {e}")
            finally:
                conn.close()

    def get_pending_urls(self, limit: int = 1):
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            c.execute("SELECT url FROM urls WHERE status='pending' ORDER BY last_fetched IS NOT NULL LIMIT ?", (limit,))
            rows = c.fetchall()
            conn.close()
        return [r[0] for r in rows]

    def mark_visited(self, url: str):
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            try:
                c.execute("UPDATE urls SET status='visited', last_fetched=CURRENT_TIMESTAMP WHERE url=?", (url,))
                conn.commit()
            except Exception as e:
                logger.error(f"[DB:mark_visited] Gagal update visited {url}: {e}")
            finally:
                conn.close()

    def resource_exists(self, url: str):
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            c.execute("SELECT local_path FROM resources WHERE url=?", (url,))
            row = c.fetchone()
            conn.close()
        return row[0] if row else None

    def add_resource(self, url: str, local_path: str):
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            try:
                c.execute("INSERT OR IGNORE INTO resources(url, local_path) VALUES(?, ?)", (url, local_path))
                conn.commit()
            except Exception as e:
                logger.error(f"[DB:add_resource] Gagal insert resource {url}: {e}")
            finally:
                conn.close()

    def sitemap_exists(self, url: str) -> bool:
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            c.execute("SELECT 1 FROM sitemap_urls WHERE url=?", (url,))
            row = c.fetchone()
            conn.close()
        return row is not None

    def add_sitemap(self, url: str):
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            try:
                c.execute("INSERT OR IGNORE INTO sitemap_urls(url) VALUES(?)", (url,))
                conn.commit()
            except Exception as e:
                logger.error(f"[DB:add_sitemap] Gagal insert sitemap {url}: {e}")
            finally:
                conn.close()

# ==============================================================================
#                         HTML FETCHER (Requests & Selenium)
# ==============================================================================

class HTMLFetcher:
    def __init__(self, user_agent: str, timeout: float, max_retries: int,
                 delay: float, verify_ssl: bool, force_render: bool, selenium_wait: float):
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.delay = delay
        self.verify_ssl = verify_ssl
        self.force_render = force_render and self._init_selenium()
        self.selenium_wait = selenium_wait

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

    def _init_selenium(self) -> bool:
        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--ignore-certificate-errors")
            driver_path = ChromeDriverManager().install()
            self.driver = webdriver.Chrome(options=chrome_options)
            logger.info("[HTMLFetcher] Selenium Headless Chrome diinisialisasi.")
            return True
        except Exception as e:
            logger.warning(f"[HTMLFetcher] Gagal inisialisasi Selenium: {e} — akan pakai Requests saja.")
            self.force_render = False
            return False

    def _retry_request(self, method: str, url: str) -> requests.Response:
        attempt = 0
        backoff = self.delay
        while attempt < self.max_retries:
            try:
                resp = self.session.request(
                    method, url,
                    timeout=self.timeout,
                    headers={"User-Agent": self.user_agent},
                    verify=self.verify_ssl
                )
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                attempt += 1
                sleep_time = backoff + random.uniform(0, 0.5)
                logger.warning(f"[HTMLFetcher] Retry {attempt}/{self.max_retries} untuk {url}: {e} — delay {sleep_time:.2f}s")
                time.sleep(sleep_time)
                backoff *= 2
        logger.error(f"[HTMLFetcher] Gagal fetch {url} setelah {self.max_retries} percobaan.")
        return None

    def fetch_static(self, url: str) -> (str, dict):
        resp = self._retry_request("GET", url)
        if not resp:
            return None, {}
        content_type = resp.headers.get("Content-Type", "")
        data = {
            "status_code": resp.status_code,
            "content_type": content_type,
            "headers": resp.headers
        }
        return resp.text, data

    def fetch_rendered(self, url: str) -> (str, dict):
        try:
            self.driver.get(url)
            time.sleep(self.selenium_wait + random.uniform(0, 0.5))
            rendered_html = self.driver.page_source
            data = {
                "status_code": 200,
                "content_type": "text/html; rendered",
                "headers": {}
            }
            return rendered_html, data
        except Exception as e:
            logger.warning(f"[HTMLFetcher] Selenium gagal render {url}: {e}")
            return None, {}

    def fetch(self, url: str) -> (str, dict):
        if self.force_render:
            html, meta = self.fetch_rendered(url)
            if html is not None:
                return html, meta
            logger.info(f"[HTMLFetcher] Fallback ke static untuk {url}")
        return self.fetch_static(url)

    def shutdown(self):
        if hasattr(self, "driver") and self.force_render:
            try:
                self.driver.quit()
                logger.info("[HTMLFetcher] Selenium WebDriver ditutup.")
            except Exception:
                pass

# ==============================================================================
#                         LINK EXTRACTOR (dari HTML)
# ==============================================================================

class LinkExtractor:
    def __init__(self, root_url: str, root_netloc: str):
        self.root_url = root_url
        self.root_netloc = root_netloc

    def _extract_meta_refresh(self, soup: BeautifulSoup, base_url: str) -> set:
        links = set()
        for meta in soup.find_all("meta", attrs={"http-equiv": re.compile(r'(?i)refresh')}):
            content = meta.get("content", "")
            match = re.search(r"url=(.+)", content, flags=re.IGNORECASE)
            if match:
                raw = match.group(1).strip().strip("'\"")
                abs_link = normalize_url(base_url, raw)
                links.add(abs_link)
        return links

    def _extract_inline_js_links(self, soup: BeautifulSoup, base_url: str) -> set:
        links = set()
        for script in soup.find_all("script"):
            script_text = script.string or ""
            for m in re.findall(r"window\.location\.href\s*=\s*['\"](.*?)['\"]", script_text):
                abs_link = normalize_url(base_url, m)
                links.add(abs_link)
            for m in re.findall(r"window\.open\s*\(\s*['\"](.*?)['\"]", script_text):
                abs_link = normalize_url(base_url, m)
                links.add(abs_link)
            for m in re.findall(r"fetch\s*\(\s*['\"](.*?)['\"]", script_text):
                abs_link = normalize_url(base_url, m)
                links.add(abs_link)
        return links

    def _extract_data_attrs(self, soup: BeautifulSoup, base_url: str) -> set:
        """
        Iterasi manual semua tag → periksa tag.attrs (dict) → cari key yang diawali 'data-'.
        Jika val tampak URL, normalisasi dan tambahkan.
        """
        links = set()
        for tag in soup.find_all():
            for key, val in tag.attrs.items():
                if key.startswith("data-") and isinstance(val, str):
                    if val.startswith("/") or val.startswith("http://") or val.startswith("https://"):
                        abs_link = normalize_url(base_url, val)
                        links.add(abs_link)
        return links

    def extract(self, html_text: str, base_url: str) -> set:
        soup = BeautifulSoup(html_text, "html.parser")
        links = set()

        # <a href>
        for tag in soup.find_all("a", href=True):
            raw = tag["href"]
            abs_link = normalize_url(base_url, raw)
            links.add(abs_link)

        # <form action>
        for tag in soup.find_all("form", action=True):
            raw = tag["action"]
            abs_link = normalize_url(base_url, raw)
            links.add(abs_link)

        # <button onclick>
        for tag in soup.find_all("button", onclick=True):
            onclick = tag["onclick"]
            match = re.search(r"(?:location\.href|window\.location\.href)\s*=\s*['\"](.*?)['\"]", onclick)
            if match:
                abs_link = normalize_url(base_url, match.group(1))
                links.add(abs_link)

        # <video src>, <audio src>, <source src>
        for tag in soup.find_all(["video", "audio", "source"], src=True):
            raw = tag["src"]
            abs_link = normalize_url(base_url, raw)
            links.add(abs_link)

        # Inline-CSS: style="background:url(...)"
        for tag in soup.find_all(style=True):
            css_text = tag["style"]
            for ref in re.findall(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", css_text):
                abs_link = normalize_url(base_url, ref)
                links.add(abs_link)
        for tag in soup.find_all("style"):
            css_text = tag.string or ""
            for ref in re.findall(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", css_text):
                abs_link = normalize_url(base_url, ref)
                links.add(abs_link)

        # Meta-refresh
        links |= self._extract_meta_refresh(soup, base_url)

        # Inline JS
        links |= self._extract_inline_js_links(soup, base_url)

        # Atribut data-*
        links |= self._extract_data_attrs(soup, base_url)

        return links

# ==============================================================================
#                       RESOURCE DOWNLOADER & REWRITER
# ==============================================================================

class ResourceDownloader:
    def __init__(self, output_dir: str, root_netloc: str, dbmgr: DBManager,
                 user_agent: str, timeout: float, max_retries: int, delay: float,
                 verify_ssl: bool, max_workers: int):
        self.output_dir = output_dir
        self.root_netloc = root_netloc
        self.dbmgr = dbmgr
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.delay = delay
        self.verify_ssl = verify_ssl

        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "*/*"
        })

    def _retry_request(self, method: str, url: str) -> requests.Response:
        attempt = 0
        backoff = self.delay
        while attempt < self.max_retries:
            try:
                resp = self.session.request(
                    method, url,
                    timeout=self.timeout,
                    headers={"User-Agent": self.user_agent},
                    verify=self.verify_ssl,
                    stream=True
                )
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                attempt += 1
                sleep_time = backoff + random.uniform(0, 0.5)
                logger.warning(f"[ResDL] Retry {attempt}/{self.max_retries} untuk {url}: {e} — delay {sleep_time:.2f}s")
                time.sleep(sleep_time)
                backoff *= 2
        logger.error(f"[ResDL] Gagal download {url} setelah {self.max_retries} percobaan.")
        return None

    def sanitize_path(self, url: str) -> (str, str):
        parsed = urlparse(url)
        netloc = parsed.netloc
        path = parsed.path.lstrip("/")
        query = parsed.query

        domain_folder = os.path.join(self.output_dir, netloc)

        if not path or path.endswith("/"):
            folder = os.path.join(domain_folder, path)
            filename = "index.html"
        else:
            folder_candidate, file_candidate = os.path.split(os.path.join(domain_folder, path))
            ext = os.path.splitext(file_candidate)[1]
            if ext:
                folder = folder_candidate
                filename = file_candidate
            else:
                folder = os.path.join(domain_folder, path)
                filename = "index.html"

        if query:
            qfilename = parse_query_to_filename(query)
            base, ext = os.path.splitext(filename)
            filename = f"{base}__{qfilename}{ext or '.html'}"

        os.makedirs(folder, exist_ok=True)
        return folder, filename

    def _save_file(self, folder: str, filename: str, content: bytes) -> str:
        full_path = os.path.join(folder, filename)
        try:
            with open(full_path, "wb") as f:
                f.write(content)
            return full_path
        except Exception as e:
            logger.error(f"[ResDL] Gagal simpan file {full_path}: {e}")
            return None

    def _parse_and_rewrite_css(self, css_bytes: bytes, css_url: str) -> bytes:
        try:
            parser = cssutils.CSSParser(fetcher=None)
            sheet = parser.parseString(css_bytes.decode('utf-8', errors='ignore'), href=css_url)
        except Exception as e:
            logger.warning(f"[ResDL:CSS] Gagal parse CSS {css_url}: {e}. Return original.")
            return css_bytes

        for rule in sheet:
            if hasattr(rule, 'style'):
                for prop in rule.style:
                    # Perbaikan: pastikan getPropertyValue menerima nama properti (string), bukan objek
                    prop_name = prop.name
                    val = rule.style.getPropertyValue(prop_name)
                    urls_in_val = re.findall(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", val)
                    for orig_ref in urls_in_val:
                        abs_ref = normalize_url(css_url, orig_ref)
                        if abs_ref.startswith("data:"):
                            continue
                        existing = self.dbmgr.resource_exists(abs_ref)
                        if existing:
                            rel_path = os.path.relpath(
                                existing,
                                os.path.dirname(os.path.join(*self.sanitize_path(css_url)))
                            )
                            val = val.replace(orig_ref, rel_path.replace(os.sep, "/"))
                        else:
                            resp = self._retry_request("GET", abs_ref)
                            if resp and resp.content:
                                folder_ref, file_ref = self.sanitize_path(abs_ref)
                                ref_path = self._save_file(folder_ref, file_ref, resp.content)
                                if ref_path:
                                    self.dbmgr.add_resource(abs_ref, ref_path)
                                    rel_path = os.path.relpath(
                                        ref_path,
                                        os.path.dirname(os.path.join(*self.sanitize_path(css_url)))
                                    )
                                    val = val.replace(orig_ref, rel_path.replace(os.sep, "/"))
                                else:
                                    logger.warning(f"[ResDL:CSS] Gagal simpan resource {abs_ref}")
                            else:
                                logger.warning(f"[ResDL:CSS] Gagal unduh resource {abs_ref}")
                        rule.style.setProperty(prop_name, val)

        modified_css = sheet.cssText.decode('utf-8')
        return modified_css.encode('utf-8')

    def download_and_rewrite(self, tag, attr: str, base_url: str) -> str:
        orig_url = tag.get(attr, "")
        abs_url = normalize_url(base_url, orig_url)
        parsed = urlparse(abs_url)
        if parsed.scheme not in ["http", "https"]:
            return None
        if not is_same_domain(self.root_netloc, abs_url):
            return None

        existing = self.dbmgr.resource_exists(abs_url)
        if existing:
            rel_existing = os.path.relpath(
                existing,
                os.path.dirname(os.path.join(*self.sanitize_path(base_url)))
            )
            return rel_existing.replace(os.sep, "/")

        resp = self._retry_request("GET", abs_url)
        if not resp or not resp.content:
            logger.warning(f"[ResDL] Gagal unduh resource {abs_url}")
            return None

        ext = os.path.splitext(parsed.path)[1].lower()
        folder, filename = self.sanitize_path(abs_url)
        if ext == ".css":
            modified = self._parse_and_rewrite_css(resp.content, abs_url)
            saved = self._save_file(folder, filename, modified)
        else:
            saved = self._save_file(folder, filename, resp.content)

        if saved:
            self.dbmgr.add_resource(abs_url, saved)
            rel_path = os.path.relpath(
                saved,
                os.path.dirname(os.path.join(*self.sanitize_path(base_url)))
            )
            return rel_path.replace(os.sep, "/")
        else:
            return None

    def submit(self, tag, attr: str, base_url: str):
        return self.executor.submit(self.download_and_rewrite, tag, attr, base_url)

    def shutdown(self):
        try:
            self.executor.shutdown(wait=False)
        except Exception:
            pass

# ==============================================================================
#                         CRAWL SCHEDULER (BFS LOOP)
# ==============================================================================

class CrawlScheduler:
    def __init__(self, root_url: str, output_dir: str, dbmgr: DBManager,
                 fetcher: HTMLFetcher, linker: LinkExtractor, resdl: ResourceDownloader, excluded_urls: list|None):
        self.root_url = root_url
        self.root_netloc = urlparse(root_url).netloc
        self.output_dir = output_dir
        self.dbmgr = dbmgr
        self.fetcher = fetcher
        self.linker = linker
        self.resdl = resdl
        self.excluded_urls = excluded_urls or []
        base_folder = os.path.join(output_dir, self.root_netloc)
        if os.path.exists(base_folder):
            logger.info(f"[Scheduler] Menghapus folder lama: {base_folder}")
            shutil.rmtree(base_folder)
        os.makedirs(base_folder, exist_ok=True)

    def seed(self):
        self.dbmgr.add_url(self.root_url)

        robots_url = urljoin(self.root_url, "/robots.txt")
        try:
            rp = robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
            logger.info(f"[Scheduler] robots.txt diambil dari {robots_url}")
            robots_txt = requests.get(
                robots_url,
                timeout=self.fetcher.timeout,
                headers={"User-Agent": self.fetcher.user_agent},
                verify=self.fetcher.verify_ssl
            ).text
            for line in robots_txt.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_link = line.split(":", 1)[1].strip()
                    if not self.dbmgr.sitemap_exists(sitemap_link):
                        urls = self._parse_sitemap(sitemap_link)
                        for u in urls:
                            if any((excluded_url in u) for excluded_url in self.excluded_urls):
                                logger.info(f"[Scheduler] Excluded URL: {u}")
                                continue
                            if is_same_domain(self.root_netloc, u):
                                self.dbmgr.add_url(u)
                                logger.info(f"[Scheduler] Added URL: {u}")
                        self.dbmgr.add_sitemap(sitemap_link)
        except Exception as e:
            logger.warning(f"[Scheduler] Gagal parse robots.txt {robots_url}: {e}")

        sitemap_fallback = urljoin(self.root_url, "/sitemap.xml")
        if not self.dbmgr.sitemap_exists(sitemap_fallback):
            urls = self._parse_sitemap(sitemap_fallback)
            for u in urls:
                if any((excluded_url in u) for excluded_url in self.excluded_urls):
                    logger.info(f"[Scheduler] Excluded URL: {u}")
                    continue
                if is_same_domain(self.root_netloc, u):
                    self.dbmgr.add_url(u)
                    logger.info(f"[Scheduler] Added URL: {u}")
            self.dbmgr.add_sitemap(sitemap_fallback)

        pending = self.dbmgr.get_pending_urls(limit=1000)
        logger.info(f"[Scheduler] Total initial pending URLs: {len(pending)}")

    def _parse_sitemap(self, sitemap_url: str) -> list:
        try:
            resp = requests.get(
                sitemap_url,
                timeout=self.fetcher.timeout,
                headers={"User-Agent": self.fetcher.user_agent},
                verify=self.fetcher.verify_ssl
            )
            resp.raise_for_status()
            content = resp.content
            tree = ET.fromstring(content)
            namespace = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            urls = []
            for elem in tree.findall("ns:url/ns:loc", namespace):
                loc = elem.text.strip()
                urls.append(loc)
            logger.info(f"[Scheduler] Parsed {len(urls)} URLs from sitemap {sitemap_url}")
            return urls
        except Exception as e:
            logger.warning(f"[Scheduler] Gagal parse sitemap {sitemap_url}: {e}")
            return []
        
    

    def run(self):
        logger.info("[Scheduler] Mulai proses crawling...")

        while True:
            pending_list = self.dbmgr.get_pending_urls(limit=1)
            if not pending_list:
                logger.info("[Scheduler] Semua URL sudah di-visit. Selesai.")
                break

            url = pending_list[0]
            logger.info(f"[Scheduler] Memproses URL: {url}")

            parsed_robots = robotparser.RobotFileParser()
            parsed_robots.set_url(urljoin(self.root_url, "/robots.txt"))
            try:
                parsed_robots.read()
            except Exception:
                pass
            if not parsed_robots.can_fetch(self.fetcher.user_agent, url) and not args.ignore_robots:
                logger.warning(f"[Scheduler] Akses dilarang robots.txt: {url}")
                self.dbmgr.mark_visited(url)
                continue

            html_text, meta = self.fetcher.fetch(url)
            if not html_text:
                self.dbmgr.mark_visited(url)
                continue

            content_type = meta.get("content_type", "")
            if "text/html" not in content_type.lower():
                fake_tag = type("X", (), {})()
                setattr(fake_tag, "get", lambda k, default=None: url if k in ("src", "href") else default)
                fake_attr = "src"
                rel_path = self.resdl.download_and_rewrite(fake_tag, fake_attr, url)
                if rel_path:
                    logger.info(f"[Scheduler] Saved non-HTML {url} → {rel_path}")
                self.dbmgr.mark_visited(url)
                continue

            all_links = self.linker.extract(html_text, url)
            internal_links = set(filter(lambda u: is_same_domain(self.root_netloc, u), all_links))
            logger.info(f"[Scheduler] Ditemukan {len(internal_links)} link internal di {url}")

            for link in internal_links:
                logger.info(f"[Scheduler] Checking URL: {link}")
                logger.info(f"[Scheduler] Excluded URLs: {self.excluded_urls}")
                if any((excluded_url in link) for excluded_url in self.excluded_urls):
                    logger.info(f"[Scheduler] Excluded URL: {link}")
                    continue
                logger.info(f"[Scheduler] Adding URL: {link}")
                self.dbmgr.add_url(link)

            soup = BeautifulSoup(html_text, "html.parser")

            # Rewrite <a href>
            for tag in soup.find_all("a", href=True):
                raw = tag["href"]
                abs_link = normalize_url(url, raw)
                if is_same_domain(self.root_netloc, abs_link):
                    folder, filename = self.resdl.sanitize_path(abs_link)
                    rel = os.path.relpath(
                        os.path.join(folder, filename),
                        os.path.dirname(os.path.join(*self.resdl.sanitize_path(url)))
                    )
                    tag["href"] = rel.replace(os.sep, "/")
                else:
                    tag["href"] = abs_link

            # Rewrite <form action>
            for tag in soup.find_all("form", action=True):
                raw = tag["action"]
                abs_link = normalize_url(url, raw)
                if is_same_domain(self.root_netloc, abs_link):
                    folder, filename = self.resdl.sanitize_path(abs_link)
                    rel = os.path.relpath(
                        os.path.join(folder, filename),
                        os.path.dirname(os.path.join(*self.resdl.sanitize_path(url)))
                    )
                    tag["action"] = rel.replace(os.sep, "/")
                else:
                    tag["action"] = abs_link

            # Rewrite <button onclick>
            for tag in soup.find_all("button", onclick=True):
                onclick = tag["onclick"]
                match = re.search(r"(?:location\.href|window\.location\.href)\s*=\s*['\"](.*?)['\"]", onclick)
                if match:
                    abs_link = normalize_url(url, match.group(1))
                    if is_same_domain(self.root_netloc, abs_link):
                        folder, filename = self.resdl.sanitize_path(abs_link)
                        rel = os.path.relpath(
                            os.path.join(folder, filename),
                            os.path.dirname(os.path.join(*self.resdl.sanitize_path(url)))
                        )
                        tag["onclick"] = f"location.href='{rel.replace(os.sep, '/')}'"
                    else:
                        tag["onclick"] = f"location.href='{abs_link}'"

            # Rewrite media (video, audio, source)
            for tag in soup.find_all(["video", "audio", "source"], src=True):
                raw = tag["src"]
                abs_link = normalize_url(url, raw)
                if is_same_domain(self.root_netloc, abs_link):
                    folder, filename = self.resdl.sanitize_path(abs_link)
                    rel = os.path.relpath(
                        os.path.join(folder, filename),
                        os.path.dirname(os.path.join(*self.resdl.sanitize_path(url)))
                    )
                    tag["src"] = rel.replace(os.sep, "/")
                else:
                    tag["src"] = abs_link

            # Submit semua resource statis
            futures = []
            for tag_name, attr in [("img", "src"), ("script", "src"), ("link", "href")]:
                for tag in soup.find_all(tag_name):
                    if attr not in tag.attrs:
                        continue
                    futures.append(self.resdl.submit(tag, attr, url))

            for fut in as_completed(futures):
                _ = fut.result()  # tag sudah di-update di dalam download_and_rewrite

            # Inline <style> CSS
            for style_tag in soup.find_all("style"):
                css_text = style_tag.string or ""
                if not css_text.strip():
                    continue
                new_css = css_text
                for ref in re.findall(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", css_text):
                    abs_ref = normalize_url(url, ref)
                    if is_same_domain(self.root_netloc, abs_ref) and not abs_ref.startswith("data:"):
                        existing = self.dbmgr.resource_exists(abs_ref)
                        if existing:
                            rel = os.path.relpath(
                                existing,
                                os.path.dirname(os.path.join(*self.resdl.sanitize_path(url)))
                            )
                            new_css = new_css.replace(ref, rel.replace(os.sep, "/"))
                        else:
                            resp = self.resdl._retry_request("GET", abs_ref)
                            if resp and resp.content:
                                folder_ref, file_ref = self.resdl.sanitize_path(abs_ref)
                                ref_path = self.resdl._save_file(folder_ref, file_ref, resp.content)
                                if ref_path:
                                    self.dbmgr.add_resource(abs_ref, ref_path)
                                    rel = os.path.relpath(
                                        ref_path,
                                        os.path.dirname(os.path.join(*self.resdl.sanitize_path(url)))
                                    )
                                    new_css = new_css.replace(ref, rel.replace(os.sep, "/"))
                                else:
                                    logger.warning(f"[Scheduler] Gagal simpan inline resource {abs_ref}")
                            else:
                                logger.warning(f"[Scheduler] Gagal unduh inline resource {abs_ref}")
                style_tag.string.replace_with(new_css)

            # Inline style="background:url(...)"
            for tag in soup.find_all(style=True):
                css_text = tag["style"]
                new_css = css_text
                for ref in re.findall(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", css_text):
                    abs_ref = normalize_url(url, ref)
                    if is_same_domain(self.root_netloc, abs_ref) and not abs_ref.startswith("data:"):
                        existing = self.dbmgr.resource_exists(abs_ref)
                        if existing:
                            rel = os.path.relpath(
                                existing,
                                os.path.dirname(os.path.join(*self.resdl.sanitize_path(url)))
                            )
                            new_css = new_css.replace(ref, rel.replace(os.sep, "/"))
                        else:
                            resp = self.resdl._retry_request("GET", abs_ref)
                            if resp and resp.content:
                                folder_ref, file_ref = self.resdl.sanitize_path(abs_ref)
                                ref_path = self.resdl._save_file(folder_ref, file_ref, resp.content)
                                if ref_path:
                                    self.dbmgr.add_resource(abs_ref, ref_path)
                                    rel = os.path.relpath(
                                        ref_path,
                                        os.path.dirname(os.path.join(*self.resdl.sanitize_path(url)))
                                    )
                                    new_css = new_css.replace(ref, rel.replace(os.sep, "/"))
                                else:
                                    logger.warning(f"[Scheduler] Gagal simpan inline style resource {abs_ref}")
                            else:
                                logger.warning(f"[Scheduler] Gagal unduh inline style resource {abs_ref}")
                tag["style"] = new_css

            # Simpan HTML final
            folder_html, filename_html = self.resdl.sanitize_path(url)
            full_html_path = os.path.join(folder_html, filename_html)
            try:
                with open(full_html_path, "w", encoding="utf-8") as f:
                    f.write(soup.prettify())
                logger.info(f"[Scheduler] HTML disimpan: {full_html_path}")
            except Exception as e:
                logger.error(f"[Scheduler] Gagal simpan HTML {url}: {e}")

            self.dbmgr.mark_visited(url)

        self.fetcher.shutdown()
        self.resdl.shutdown()
        logger.info("[Scheduler] Semua proses selesai. Mirror berhasil!")

# ==============================================================================
#                                MAIN SCRIPT
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MegaMirror: Mirror sebuah situs (Static + JS rendered) secara komprehensif."
    )
    parser.add_argument("url", help="URL root situs, misalnya: https://example.com")
    parser.add_argument("--output", "-o", default="mirrored_full_site",
                        help="Folder output (default: ./mirrored_full_site)")
    parser.add_argument("--ignore-robots", action="store_true", default=False,
                        help="Jika diset, abaikan rules di robots.txt.")
    parser.add_argument("--no-ssl-verify", action="store_true", default=False,
                        help="Jika diset, nonaktifkan verifikasi SSL.")
    parser.add_argument("--max-workers", "-w", type=int, default=10,
                        help="Jumlah worker untuk download resource statis (default: 10).")
    parser.add_argument("--exclude", "-x", action="append", help="exclude URLS matching this substring", nargs=1)
    parser.add_argument("--timeout", "-t", type=float, default=15.0,
                        help="Timeout (detik) untuk setiap request HTTP (default: 15).")
    parser.add_argument("--max-retries", "-r", type=int, default=5,
                        help="Jumlah retry untuk request yang gagal (exponential backoff) (default: 5).")
    parser.add_argument("--delay", "-d", type=float, default=1.0,
                        help="Delay awal (detik) + jitter untuk retries & rate-limiting (default: 1).")
    parser.add_argument("--user-agent", "-u", type=str, default="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                        help="User-Agent header yang dipakai (default: MegaMirrorBot/3.5).")
    parser.add_argument("--force-render", action="store_true", default=False,
                        help="Jika diset, paksa gunakan Selenium Headless Chrome untuk merender halaman.")
    parser.add_argument("--selenium-wait", type=float, default=2.0,
                        help="Waktu tunggu (detik) agar JavaScript benar-benar selesai dieksekusi (default: 2).")

    args = parser.parse_args()

    root_url = args.url.rstrip("/")
    output_dir = args.output
    ignore_robots = args.ignore_robots
    verify_ssl = not args.no_ssl_verify
    max_workers = args.max_workers
    timeout = args.timeout
    max_retries = args.max_retries
    exclude = [item for sublist in args.exclude for item in sublist]
    delay = args.delay
    user_agent = args.user_agent
    force_render = args.force_render
    selenium_wait = args.selenium_wait

    # Validasi URL
    if not root_url.startswith(("http://", "https://")):
        logger.error("URL harus diawali dengan 'http://' atau 'https://'.")
        sys.exit(1)

    logger.info("================================================================================")
    logger.info(f"MegaMirror d i m u l a i : {root_url}")
    logger.info(f"Output folder      : {output_dir}")
    logger.info(f"Ignore robots.txt  : {ignore_robots}")
    logger.info(f"Verify SSL         : {verify_ssl}")
    logger.info(f"Max workers        : {max_workers}")
    print(exclude)
    logger.info(f"Exclude            : {", ".join(exclude) if exclude else "None"}")
    logger.info(f"Timeout (s)        : {timeout}")
    logger.info(f"Max retries        : {max_retries}")
    logger.info(f"Delay (s)          : {delay}")
    logger.info(f"User-Agent         : {user_agent}")
    logger.info(f"Force render JS    : {force_render}")
    logger.info(f"Selenium wait (s)  : {selenium_wait}")
    logger.info("================================================================================")

    os.makedirs(output_dir, exist_ok=True)
    db_path = os.path.join(output_dir, "mirror.db")

    # Inisialisasi DBManager
    dbmgr = DBManager(db_path)

    # Inisialisasi komponen
    fetcher = HTMLFetcher(
        user_agent=user_agent,
        timeout=timeout,
        max_retries=max_retries,
        delay=delay,
        verify_ssl=verify_ssl,
        force_render=force_render,
        selenium_wait=selenium_wait
    )

    linker = LinkExtractor(
        root_url=root_url,
        root_netloc=urlparse(root_url).netloc
    )

    resdl = ResourceDownloader(
        output_dir=output_dir,
        root_netloc=urlparse(root_url).netloc,
        dbmgr=dbmgr,
        user_agent=user_agent,
        timeout=timeout,
        max_retries=max_retries,
        delay=delay,
        verify_ssl=verify_ssl,
        max_workers=max_workers
    )

    scheduler = CrawlScheduler(
        root_url=root_url,
        output_dir=output_dir,
        dbmgr=dbmgr,
        fetcher=fetcher,
        linker=linker,
        excluded_urls=exclude,
        resdl=resdl
    )

    # Seed (tambahkan root_url dan sitemap jika ada)
    scheduler.seed()

    # Jalankan crawling hingga selesai
    scheduler.run()

    logger.info("MegaMirror: Proses mirror selesai. Periksa hasil di folder output.")
    sys.exit(0)
