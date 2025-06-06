#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
app.py

Versi “super-kompleks” dari SiteMirror: 
– Mendukung crawling semua halaman (tanpa batas kedalaman, BFS hingga habis)  
– Mem-parse HTML statis (Requests + BeautifulSoup)  
– Mem-parse HTML dinamis (Selenium Headless Chrome)  
– Menangani robots.txt (termasuk Crawl-delay), Sitemap, fallback sitemap.xml  
– Menggunakan SQLite untuk stateful resume (tabel urls, resources, sitemap_urls)  
– Mem-parse Meta-refresh, Link di <a>, <form>, <button onclick>, <video src>, inline-JS, bahkan  
  deep link yang tertanam di atribut data-...  
– ResourceDownloader terpisah, men-download CSS (cssutils, parsing url(...)), JS, gambar, font, PDF, Office, JSON, XML, dsb.  
– Rewrite semua link di HTML/CSS/JS agar menjadi relative path lokal  
– Logging multi-level + RotatingFileHandler (max 5 file @ 5MB)  
– ThreadPoolExecutor terpisah untuk page-crawl (HTML) dan resource-download  
– Exponential backoff + retry + jitter  
– Banyak helper functions untuk normalisasi URL, sanitasi path, transform query→filename, dsb.  
– Modularisasi: DBManager, CrawlScheduler, HTMLFetcher, LinkExtractor, ResourceDownloader, dll.  

**Dependencies (Python 3.8+):**
    pip install requests beautifulsoup4 cssutils selenium webdriver-manager urllib3

**Persyaratan tambahan:**
    – Chrome / Chromium harus terpasang jika menggunakan --force_render  
    – Pastikan `chromedriver` kompatibel jika tidak menggunakan webdriver-manager  

**Contoh pemanggilan:**
    python app.py https://t2b.my.id/ \
        --output my_full_mirror \
        --ignore_robots False \
        --max_workers 8 \
        --timeout 15 \
        --max_retries 7 \
        --delay 1.0 \
        --user_agent "MegaMirrorBot/3.5" \
        --force_render True \
        --selenium_wait 2.5

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
import queue
import random
import string
from urllib.parse import urlparse, urljoin, urldefrag, parse_qs, urlencode, quote_plus
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

# RotatingFileHandler: max 5 files, masing-masing 5MB
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s/%(funcName)s] %(message)s',
                                  datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("MegaMirror")
logger.setLevel(logging.DEBUG)

# Console handler (INFO+)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# File handler (DEBUG+)
file_handler = RotatingFileHandler(os.path.join(LOG_DIR, "site_mirror.log"),
                                   maxBytes=5 * 1024 * 1024,
                                   backupCount=5,
                                   encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# Disable cssutils DEBUG yang terlalu verbose
cssutils.log.setLevel(logging.ERROR)

# ==============================================================================
#                            UTILITY FUNCTIONS
# ==============================================================================

def random_string(length=8):
    """Generate random string untuk fallback filename."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def sanitize_filename(name: str) -> str:
    """
    Bersihkan nama file dengan:
    – Mengganti karakter illegal Windows/Linux
    – Mengubah spasi menjadi underscore
    – Jika string kosong, generate random
    """
    if not name or name.strip() == "":
        return random_string(6) + ".html"
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', name).strip()
    sanitized = sanitized.replace(" ", "_")
    return sanitized

def normalize_url(base_url: str, link: str) -> str:
    """
    – Convert link (bisa relatif atau absolute) menjadi absolute URL  
    – Hilangkan fragment (#...)  
    – Hilangkan trailing slash kecuali root  
    """
    joined = urljoin(base_url, link)
    normalized, _ = urldefrag(joined)
    # Remove trailing slash if not root
    parsed = urlparse(normalized)
    path = parsed.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
        normalized = urlunparse((parsed.scheme, parsed.netloc, path,
                                 parsed.params, parsed.query, parsed.fragment))
    return normalized

def is_same_domain(root_netloc: str, target_url: str) -> bool:
    """
    Cek apakah `target_url` berada di root_netloc (boleh subdomain).
    Contoh: root_netloc='t2b.my.id', target 'sub.t2b.my.id/page' => True
    """
    try:
        parsed = urlparse(target_url)
        return parsed.netloc.endswith(root_netloc)
    except Exception:
        return False

def parse_query_to_filename(query: str) -> str:
    """
    Ubah query string menjadi semacam filename untuk menyimpan:
    Contoh: '?page=2&sort=asc' → 'page_2_sort_asc'
    Jika terlalu rumit, generate random.
    """
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
    """
    Meng-handle koneksi SQLite (mirror.db) dengan tiga tabel:
      – urls(url TEXT PRIMARY KEY, status TEXT, last_fetched TIMESTAMP)  
      – resources(url TEXT PRIMARY KEY, local_path TEXT)  
      – sitemap_urls(url TEXT PRIMARY KEY)  # untuk mencatat sitemap yang sudah diambil
    Menjamin thread-safety via db_lock.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db_lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return conn

    def _init_db(self):
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            # Tabel URLs
            c.execute("""
                CREATE TABLE IF NOT EXISTS urls (
                    url TEXT PRIMARY KEY,
                    status TEXT,
                    last_fetched TIMESTAMP
                );
            """)
            # Tabel Resources
            c.execute("""
                CREATE TABLE IF NOT EXISTS resources (
                    url TEXT PRIMARY KEY,
                    local_path TEXT
                );
            """)
            # Tabel Sitemap URLs (agar tidak diparse ulang)
            c.execute("""
                CREATE TABLE IF NOT EXISTS sitemap_urls (
                    url TEXT PRIMARY KEY
                );
            """)
            conn.commit()
            conn.close()

    def add_url(self, url: str):
        """
        Insert URL ke tabel urls jika belum ada, dengan status = 'pending'.
        """
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
        """
        Ambil daftar URL yang status='pending', urut berdasarkan last_fetched NULL dahulu.
        Batasi jumlah dengan parameter limit (FIFO).
        """
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            c.execute("SELECT url FROM urls WHERE status='pending' ORDER BY last_fetched IS NOT NULL LIMIT ?", (limit,))
            rows = c.fetchall()
            conn.close()
        return [r[0] for r in rows]

    def mark_visited(self, url: str):
        """
        Update status URL menjadi 'visited' dan catat timestamp sekarang.
        """
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
        """
        Cek apakah `url` sudah ada di tabel resources. 
        Jika ada, return local_path; jika tidak, return None.
        """
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            c.execute("SELECT local_path FROM resources WHERE url=?", (url,))
            row = c.fetchone()
            conn.close()
        return row[0] if row else None

    def add_resource(self, url: str, local_path: str):
        """
        Tambahkan record di tabel resources (url→local_path).
        """
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
        """
        Cek apakah URL sitemap.xml sudah pernah diparse/memasuki tabel sitemap_urls.
        """
        with self.db_lock:
            conn = self._connect()
            c = conn.cursor()
            c.execute("SELECT 1 FROM sitemap_urls WHERE url=?", (url,))
            row = c.fetchone()
            conn.close()
        return row is not None

    def add_sitemap(self, url: str):
        """
        Tandai URL sitemap sebagai sudah diambil
        """
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
#                         FETCHER (Requests & Selenium)
# ==============================================================================

class HTMLFetcher:
    """
    – Bertugas mengambil HTML suatu URL, dengan dua mode:
    – Requests murni (fetch_static), atau
    – Selenium Headless Chrome (fetch_rendered)
    Memilih mode berdasarkan self.force_render. 
    """

    def __init__(self, user_agent: str, timeout: float, max_retries: int, delay: float, 
                 verify_ssl: bool, force_render: bool, selenium_wait: float):
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.delay = delay
        self.verify_ssl = verify_ssl
        self.force_render = force_render and self._init_selenium()
        self.selenium_wait = selenium_wait

        # Session requests
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

    def _init_selenium(self) -> bool:
        """
        Inisialisasi Selenium WebDriver (Headless Chrome). 
        Jika gagal install via webdriver-manager, fallback False.
        """
        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--ignore-certificate-errors")
            # Gunakan webdriver-manager untuk otomatis men-download driver
            driver_path = ChromeDriverManager().install()
            self.driver = webdriver.Chrome(executable_path=driver_path, options=chrome_options)
            logger.info("[HTMLFetcher] Selenium Headless Chrome diinisialisasi.")
            return True
        except Exception as e:
            logger.warning(f"[HTMLFetcher] Gagal inisialisasi Selenium: {e} — akan pakai Requests saja.")
            self.force_render = False
            return False

    def _retry_request(self, method: str, url: str) -> requests.Response:
        """
        Lakukan HTTP request dengan exponential backoff + jitter + retries.
        """
        attempt = 0
        backoff = self.delay
        while attempt < self.max_retries:
            try:
                resp = self.session.request(method, url, timeout=self.timeout,
                                            headers={"User-Agent": self.user_agent},
                                            verify=self.verify_ssl)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                attempt += 1
                sleep_time = backoff + random.uniform(0, 0.5)  # jitter
                logger.warning(f"[HTMLFetcher] Retry {attempt}/{self.max_retries} untuk {url}: {e} — delay {sleep_time:.2f}s")
                time.sleep(sleep_time)
                backoff *= 2
        logger.error(f"[HTMLFetcher] Gagal fetch {url} setelah {self.max_retries} percobaan.")
        return None

    def fetch_static(self, url: str) -> (str, dict):
        """
        Fetch halaman via requests + BeautifulSoup. 
        Kembalikan tuple (html_text, extracted_data) 
        where extracted_data={"status_code": int, "content_type": str, ...}
        """
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
        """
        Fetch halaman via Selenium Headless Chrome → tunggu JS selama self.selenium_wait
        Kembalikan (html_text, extracted_data={"status_code":200,"content_type":"text/html"}).
        Jika error, kembalikan (None, {}).
        """
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
        """
        Pilih mode fetch berdasarkan self.force_render. 
        Cek robots.txt & fallback jika error.
        """
        if self.force_render:
            html, meta = self.fetch_rendered(url)
            if html is not None:
                return html, meta
            # Jika Selenium gagal, coba static
            logger.info(f"[HTMLFetcher] Fallback ke static untuk {url}")
        return self.fetch_static(url)

    def shutdown(self):
        """
        Tutup Selenium driver jika ada.
        """
        if hasattr(self, "driver") and self.force_render:
            try:
                self.driver.quit()
                logger.info("[HTMLFetcher] Selenium WebDriver ditutup.")
            except Exception:
                pass

# ==============================================================================
#                         LINK EXTRACTOR (dari HTML rendered atau static)
# ==============================================================================

class LinkExtractor:
    """
    Meng-extract semua link dari HTML (dua skenario):
      – dari HTML yang di-RS (Requests + BeautifulSoup)
      – dari HTML yang di-render (Selenium)
    Mencakup: <a href>, <form action>, <button onclick="window.location.href='...'">,
    <video src>, <audio src>, <source src>, inline-CSS (url(...)), JS inline (window.location),
    meta-refresh, deep-link JSON di atribut data-...
    """

    def __init__(self, root_url: str, root_netloc: str):
        self.root_url = root_url
        self.root_netloc = root_netloc

    def _extract_meta_refresh(self, soup: BeautifulSoup, base_url: str) -> set:
        """
        Cari <meta http-equiv="refresh" content="3; url=http://target.com"> 
        dan kembalikan target URL.
        """
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
        """
        Cari pola window.location.href='...', window.open('...'), atau AJAX fetch('...') di dalam tag <script> inline.
        Ini hanya regex sederhana, bukan full JS parsing.
        """
        links = set()
        for script in soup.find_all("script"):
            script_text = script.string or ""
            # window.location.href
            for match in re.findall(r"window\.location\.href\s*=\s*['\"](.*?)['\"]", script_text):
                abs_link = normalize_url(base_url, match)
                links.add(abs_link)
            # window.open(...)
            for match in re.findall(r"window\.open\s*\(\s*['\"](.*?)['\"]", script_text):
                abs_link = normalize_url(base_url, match)
                links.add(abs_link)
            # fetch('...')
            for match in re.findall(r"fetch\s*\(\s*['\"](.*?)['\"]", script_text):
                abs_link = normalize_url(base_url, match)
                links.add(abs_link)
        return links

    def _extract_data_attrs(self, soup: BeautifulSoup, base_url: str) -> set:
        """
        Cari atribut data-url="..." atau data-href="..." dsb., 
        karena beberapa situs menanam link di data-* untuk JS.
        """
        links = set()
        for tag in soup.find_all(attrs=lambda attr: attr and any(k.startswith("data-") for k in attr.keys())):
            for (k, v) in tag.attrs.items():
                if k.startswith("data-") and isinstance(v, str):
                    # jika string terlihat seperti URL
                    if re.match(r'^https?://', v) or v.startswith("/"):
                        abs_link = normalize_url(base_url, v)
                        links.add(abs_link)
        return links

    def extract(self, html_text: str, base_url: str) -> set:
        """
        Ekstrak semua link internal+eksternal dari html_text:
        – <a href>, <form action>, <button onclick>, <video src>, <audio src>, <source src>  
        – inline-CSS url(...)  
        – <meta refresh>  
        – inline JS (window.location, window.open, fetch)  
        – atribut data-* yang mengandung URL  
        Kembalikan set of URLs (belum disaring domain).
        """
        soup = BeautifulSoup(html_text, "html.parser")
        links = set()

        # 1) <a href>
        for tag in soup.find_all("a", href=True):
            raw = tag["href"]
            abs_link = normalize_url(base_url, raw)
            links.add(abs_link)

        # 2) <form action>
        for tag in soup.find_all("form", action=True):
            raw = tag["action"]
            abs_link = normalize_url(base_url, raw)
            links.add(abs_link)

        # 3) <button onclick="...">
        for tag in soup.find_all("button", onclick=True):
            onclick = tag["onclick"]
            match = re.search(r"(?:location\.href|window\.location\.href)\s*=\s*['\"](.*?)['\"]", onclick)
            if match:
                abs_link = normalize_url(base_url, match.group(1))
                links.add(abs_link)

        # 4) <video src>, <audio src>, <source src>
        for tag in soup.find_all(["video", "audio", "source"], src=True):
            raw = tag["src"]
            abs_link = normalize_url(base_url, raw)
            links.add(abs_link)

        # 5) Inline-CSS: style="background:url(...)" atau <style>url(...)</style>
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

        # 6) Meta-refresh
        links |= self._extract_meta_refresh(soup, base_url)

        # 7) Inline JS
        links |= self._extract_inline_js_links(soup, base_url)

        # 8) Atribut data-*
        links |= self._extract_data_attrs(soup, base_url)

        return links

# ==============================================================================
#                        RESOURCE DOWNLOADER & REWRITER
# ==============================================================================

class ResourceDownloader:
    """
    Mengunduh resource statis:
      – CSS: parse via cssutils lalu unduh url(...) di dalamnya, rewrite  
      – Gambar (png,jpg,svg,webp,gif), Font (woff,ttf,otf,eot), JS (.js), PDF, Office (.docx,.xlsx...),  
        JSON, XML, dsb.  
    Gunakan ThreadPoolExecutor terpisah (self.executor).  
    Rewrite link di tag/tag-attribute (misal <img src>= → local relative path).
    """

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

        # ThreadPoolExecutor untuk download resource
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

        # Session keperluan resource
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "*/*"
        })

    def _retry_request(self, method: str, url: str) -> requests.Response:
        """
        Retries + exponential backoff + jitter.
        """
        attempt = 0
        backoff = self.delay
        while attempt < self.max_retries:
            try:
                resp = self.session.request(method, url, timeout=self.timeout,
                                            headers={"User-Agent": self.user_agent},
                                            verify=self.verify_ssl, stream=True)
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
        """
        Dari URL → generate folder lokal dan nama file:
         – Path: <output_dir>/<root_netloc>/<path dari URL tanpa query>
         – Jika nama file tidak ada ekstensi (atau hanya folder), pakai index.html
         – Jika query ada, tambahkan parse_query_to_filename
        """
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

        # Jika masih ada query string, tambahkan ke filename
        if query:
            qfilename = parse_query_to_filename(query)
            base, ext = os.path.splitext(filename)
            filename = f"{base}__{qfilename}{ext or '.html'}"

        os.makedirs(folder, exist_ok=True)
        return folder, filename

    def _save_file(self, folder: str, filename: str, content: bytes):
        """
        Simpan content → <folder>/<filename>
        """
        full_path = os.path.join(folder, filename)
        try:
            with open(full_path, "wb") as f:
                f.write(content)
            return full_path
        except Exception as e:
            logger.error(f"[ResDL] Gagal simpan file {full_path}: {e}")
            return None

    def _parse_and_rewrite_css(self, css_bytes: bytes, css_url: str) -> bytes:
        """
        Parsing CSS: cari semua url(...) → unduh resource dan rewrite.
        Return CSS yang sudah di-modify (bytes).
        """
        try:
            parser = cssutils.CSSParser(fetcher=None)
            sheet = parser.parseString(css_bytes.decode('utf-8', errors='ignore'), href=css_url)
        except Exception as e:
            logger.warning(f"[ResDL:CSS] Gagal parse CSS {css_url}: {e}. Return original.")
            return css_bytes

        for rule in sheet:
            if hasattr(rule, 'style'):
                for prop in rule.style:
                    val = rule.style.getPropertyValue(prop)
                    urls_in_val = re.findall(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", val)
                    for orig_ref in urls_in_val:
                        abs_ref = normalize_url(css_url, orig_ref)
                        if abs_ref.startswith("data:"):
                            continue
                        # Cek apakah resource sudah ada
                        existing = self.dbmgr.resource_exists(abs_ref)
                        if existing:
                            rel_path = os.path.relpath(existing,
                                                        os.path.dirname(
                                                            os.path.join(*self.sanitize_path(css_url))
                                                        ))
                            val = val.replace(orig_ref, rel_path.replace(os.sep, "/"))
                        else:
                            # Download resource
                            resp = self._retry_request("GET", abs_ref)
                            if resp and resp.content:
                                folder_ref, file_ref = self.sanitize_path(abs_ref)
                                ref_path = self._save_file(folder_ref, file_ref, resp.content)
                                if ref_path:
                                    self.dbmgr.add_resource(abs_ref, ref_path)
                                    rel_path = os.path.relpath(ref_path,
                                                                os.path.dirname(
                                                                    os.path.join(*self.sanitize_path(css_url))
                                                                ))
                                    val = val.replace(orig_ref, rel_path.replace(os.sep, "/"))
                                else:
                                    logger.warning(f"[ResDL:CSS] Gagal simpan resource {abs_ref}")
                            else:
                                logger.warning(f"[ResDL:CSS] Gagal unduh resource {abs_ref}")
                        rule.style[prop] = val

        modified_css = sheet.cssText.decode('utf-8')
        return modified_css.encode('utf-8')

    def download_and_rewrite(self, tag, attr: str, base_url: str) -> str:
        """
        Proses satu tag HTML (misal <img src=...>, <script src=...>, <link href=...>):  
         – Jika eksternal (domain beda), return None  
         – Jika sudah pernah di-download (cek DB), return relative_path  
         – Jika CSS (.css), unduh → parse+rewrite → simpan → return relative_path  
         – Jika bukan CSS (gambar, JS, font, PDF, dsb.), simpan → return relative_path  
        Return nilai relative path (string) atau None jika gagal/skip.
        """
        orig_url = tag.get(attr, "")
        abs_url = normalize_url(base_url, orig_url)
        parsed = urlparse(abs_url)
        if parsed.scheme not in ["http", "https"]:
            return None
        if not is_same_domain(self.root_netloc, abs_url):
            # Kecuali kita pilih untuk menyimpan juga (tidak di-crawl, tapi disimpan)
            return None

        # Cek DB resources
        existing = self.dbmgr.resource_exists(abs_url)
        if existing:
            rel_existing = os.path.relpath(existing,
                                           os.path.dirname(
                                               os.path.join(*self.sanitize_path(base_url))
                                           ))
            return rel_existing.replace(os.sep, "/")

        # Download resource dengan retries
        resp = self._retry_request("GET", abs_url)
        if not resp or not resp.content:
            logger.warning(f"[ResDL] Gagal unduh resource {abs_url}")
            return None

        ext = os.path.splitext(parsed.path)[1].lower()
        folder, filename = self.sanitize_path(abs_url)
        if ext == ".css":
            # Parsing CSS
            modified = self._parse_and_rewrite_css(resp.content, abs_url)
            saved = self._save_file(folder, filename, modified)
        else:
            # Simpan apa adanya
            saved = self._save_file(folder, filename, resp.content)

        if saved:
            self.dbmgr.add_resource(abs_url, saved)
            rel_path = os.path.relpath(saved,
                                       os.path.dirname(
                                           os.path.join(*self.sanitize_path(base_url))
                                       ))
            return rel_path.replace(os.sep, "/")
        else:
            return None

    def submit(self, tag, attr: str, base_url: str):
        """
        Submit tugas download+rewrite ke executor. 
        Kembalikan future yang nantinya result-nya adalah relative_path (atau None).
        """
        return self.executor.submit(self.download_and_rewrite, tag, attr, base_url)

    def shutdown(self):
        """
        Shutdown ThreadPoolExecutor (jangan tunggu lama, cukup shutdown).
        """
        try:
            self.executor.shutdown(wait=False)
        except Exception:
            pass

# ==============================================================================
#                         CRAWL SCHEDULER (BFS LOOP)
# ==============================================================================

class CrawlScheduler:
    """
    Mengelola loop “ambil pendings URL dari DB → crawl satu per satu → tambahkan URL baru”  
    – Memanfaatkan HTMLFetcher (fetcher), LinkExtractor (linker), ResourceDownloader (resdl), dan DBManager (dbmgr)  
    – Untuk setiap halaman:  
      1. Fetch HTML (rendered atau static)  
      2. If content-type bukan HTML (misal PDF/Office/Image), simpan langsung (ResourceDownloader) → tidak parsing HTML  
      3. Jika HTML:  
         a. Extract semua link via LinkExtractor (saring domain sama/beda)  
         b. Rewrite link di tag HTML (semua <a>, <form>, <button>, <video>, <audio>, inline-css)  
         c. Submit semua resource (img, script, link-css) ke ResourceDownloader  
         d. Tunggu resource futures selesai → rewriting tag yang sudah diubah di HTML  
         e. Simpan HTML final ke local path (folder/file)  
         f. Tandai URL visited, berikan timestamp, log jumlah link baru  
    – Loop hingga tidak ada pending URL  
    """

    def __init__(self, root_url: str, output_dir: str, dbmgr: DBManager, fetcher: HTMLFetcher,
                 linker: LinkExtractor, resdl: ResourceDownloader):
        self.root_url = root_url
        self.root_netloc = urlparse(root_url).netloc
        self.output_dir = output_dir
        self.dbmgr = dbmgr
        self.fetcher = fetcher
        self.linker = linker
        self.resdl = resdl

        # Buat folder dasar
        base_folder = os.path.join(output_dir, self.root_netloc)
        if os.path.exists(base_folder):
            logger.info(f"[Scheduler] Menghapus folder lama: {base_folder}")
            shutil.rmtree(base_folder)
        os.makedirs(base_folder, exist_ok=True)

    def seed(self):
        """
        Tambahkan root_url ke DB. 
        Jika robots.txt punya <Sitemap> atau /sitemap.xml ada, parse dan tambahkan semua URL di sana ke DB (pending).
        """
        # 1) Tambahkan root_url
        self.dbmgr.add_url(self.root_url)

        # 2) Parse robots.txt untuk tag Sitemap
        robots_url = urljoin(self.root_url, "/robots.txt")
        try:
            rp = robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
            logger.info(f"[Scheduler] robots.txt diambil dari {robots_url}")
            # Cek apakah ada baris “Sitemap:” di robots.txt
            robots_txt = requests.get(robots_url, timeout=self.fetcher.timeout,
                                      headers={"User-Agent": self.fetcher.user_agent},
                                      verify=self.fetcher.verify_ssl).text
            for line in robots_txt.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_link = line.split(":", 1)[1].strip()
                    if not self.dbmgr.sitemap_exists(sitemap_link):
                        urls = self._parse_sitemap(sitemap_link)
                        for u in urls:
                            if is_same_domain(self.root_netloc, u):
                                self.dbmgr.add_url(u)
                        self.dbmgr.add_sitemap(sitemap_link)
        except Exception as e:
            logger.warning(f"[Scheduler] Gagal parse robots.txt {robots_url}: {e}")

        # 3) Jika belum ada sitemap di DB, coba /sitemap.xml
        sitemap_fallback = urljoin(self.root_url, "/sitemap.xml")
        if not self.dbmgr.sitemap_exists(sitemap_fallback):
            urls = self._parse_sitemap(sitemap_fallback)
            for u in urls:
                if is_same_domain(self.root_netloc, u):
                    self.dbmgr.add_url(u)
            self.dbmgr.add_sitemap(sitemap_fallback)

        pending = self.dbmgr.get_pending_urls(limit=1000)
        logger.info(f"[Scheduler] Total initial pending URLs: {len(pending)}")

    def _parse_sitemap(self, sitemap_url: str) -> list:
        """
        Download & parse sitemap.xml → kembalikan daftar URL yang ditemukan.
        """
        try:
            resp = requests.get(sitemap_url, timeout=self.fetcher.timeout,
                                headers={"User-Agent": self.fetcher.user_agent},
                                verify=self.fetcher.verify_ssl)
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
        """
        Loop utama:
          – Ambil 1 URL dari pending  
          – Cek robots.txt (jika ignore_robots=False)  
          – Fetch HTML (atau non-HTML resource)  
          – Jika non-HTML: langsung unduh via ResourceDownloader (fd) → mark visited → continue  
          – Jika HTML:  
             * Extract semua link via LinkExtractor  
             * Tambahkan link baru ke DB  
             * Rewrite tag resource (img, script, link) → submit ke ResourceDownloader  
             * Tunggu semua futures selesai → rewrite <tag> sesuai relative_path  
             * Rewrite <a>, <form>, <button onclick>, <video>, inline CSS, dll.  
             * Simpan HTML final ke local path  
             * mark visited  
             * log ringkasan  
          – Ulangi hingga tidak ada pending URL  
        """
        logger.info("[Scheduler] Mulai proses crawling...")

        while True:
            pending_list = self.dbmgr.get_pending_urls(limit=1)
            if not pending_list:
                logger.info("[Scheduler] Semua URL sudah di-visit. Selesai.")
                break

            url = pending_list[0]
            logger.info(f"[Scheduler] Memproses URL: {url}")

            # Cek robots.txt
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

            # Fetch content
            html_text, meta = self.fetcher.fetch(url)
            if not html_text:
                # Jika gagal fetch sama sekali → mark visited
                self.dbmgr.mark_visited(url)
                continue

            content_type = meta.get("content_type", "")
            # Jika bukan HTML (misal PDF/Image/Office), simpan langsung
            if "text/html" not in content_type.lower():
                # Buat dummy tag untuk download (gunakan ResourceDownloader)
                fake_tag = type("X", (), {})()
                setattr(fake_tag, "get", lambda k, default=None: url if k == "src" or k == "href" else default)
                fake_attr = "src"
                rel_path = self.resdl.download_and_rewrite(fake_tag, fake_attr, url)
                if rel_path:
                    logger.info(f"[Scheduler] Saved non-HTML {url} → {rel_path}")
                self.dbmgr.mark_visited(url)
                continue

            # Jika HTML, maka:
            # 1) Extract semua link
            all_links = self.linker.extract(html_text, url)
            internal_links = set(filter(lambda u: is_same_domain(self.root_netloc, u), all_links))
            logger.info(f"[Scheduler] Ditemukan {len(internal_links)} link internal di {url}")

            # 2) Masukkan semua link baru ke DB
            new_count = 0
            for link in internal_links:
                # Abaikan jika sudah ada di DB
                self.dbmgr.add_url(link)
                new_count += 1
            logger.debug(f"[Scheduler] {new_count} link baru ditambahkan ke DB")

            # 3) Parse HTML dengan BeautifulSoup untuk rewrite link dan resource
            soup = BeautifulSoup(html_text, "html.parser")

            # A) Rewrite <a href>
            for tag in soup.find_all("a", href=True):
                raw = tag["href"]
                abs_link = normalize_url(url, raw)
                if is_same_domain(self.root_netloc, abs_link):
                    # Pastikan buat folder & filename
                    folder, filename = self.resdl.sanitize_path(abs_link)
                    rel = os.path.relpath(os.path.join(folder, filename),
                                          os.path.dirname(os.path.join(*self.resdl.sanitize_path(url))))
                    tag["href"] = rel.replace(os.sep, "/")
                else:
                    tag["href"] = abs_link  # external

            # B) Rewrite <form action>
            for tag in soup.find_all("form", action=True):
                raw = tag["action"]
                abs_link = normalize_url(url, raw)
                if is_same_domain(self.root_netloc, abs_link):
                    folder, filename = self.resdl.sanitize_path(abs_link)
                    rel = os.path.relpath(os.path.join(folder, filename),
                                          os.path.dirname(os.path.join(*self.resdl.sanitize_path(url))))
                    tag["action"] = rel.replace(os.sep, "/")
                else:
                    tag["action"] = abs_link

            # C) Rewrite <button onclick="window.location.href='...'">
            for tag in soup.find_all("button", onclick=True):
                onclick = tag["onclick"]
                match = re.search(r"(?:location\.href|window\.location\.href)\s*=\s*['\"](.*?)['\"]", onclick)
                if match:
                    abs_link = normalize_url(url, match.group(1))
                    if is_same_domain(self.root_netloc, abs_link):
                        folder, filename = self.resdl.sanitize_path(abs_link)
                        rel = os.path.relpath(os.path.join(folder, filename),
                                              os.path.dirname(os.path.join(*self.resdl.sanitize_path(url))))
                        tag["onclick"] = f"location.href='{rel.replace(os.sep, '/')}'"
                    else:
                        tag["onclick"] = f"location.href='{abs_link}'"

            # D) Rewrite <video src>, <audio src>, <source src>
            for tag in soup.find_all(["video", "audio", "source"], src=True):
                raw = tag["src"]
                abs_link = normalize_url(url, raw)
                if is_same_domain(self.root_netloc, abs_link):
                    folder, filename = self.resdl.sanitize_path(abs_link)
                    rel = os.path.relpath(os.path.join(folder, filename),
                                          os.path.dirname(os.path.join(*self.resdl.sanitize_path(url))))
                    tag["src"] = rel.replace(os.sep, "/")
                else:
                    tag["src"] = abs_link

            # E) Submit semua resource statis ke ResourceDownloader
            futures = []
            for tag_name, attr in [("img", "src"), ("script", "src"), ("link", "href")]:
                for tag in soup.find_all(tag_name):
                    if attr not in tag.attrs:
                        continue
                    futures.append(self.resdl.submit(tag, attr, url))

            # F) Tunggu semua resource selesai di-download → tag[attr] sudah di-rewrite di fungsi
            for fut in as_completed(futures):
                rel_path = fut.result()
                # Tag sudah diupdate di dalam fungsi download_and_rewrite
                pass

            # G) Proses inline <style> CSS
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
                            rel = os.path.relpath(existing,
                                                  os.path.dirname(os.path.join(*self.resdl.sanitize_path(url))))
                            new_css = new_css.replace(ref, rel.replace(os.sep, "/"))
                        else:
                            resp = self.resdl._retry_request("GET", abs_ref)
                            if resp and resp.content:
                                folder_ref, file_ref = self.resdl.sanitize_path(abs_ref)
                                ref_path = self.resdl._save_file(folder_ref, file_ref, resp.content)
                                if ref_path:
                                    self.dbmgr.add_resource(abs_ref, ref_path)
                                    rel = os.path.relpath(ref_path,
                                                          os.path.dirname(os.path.join(*self.resdl.sanitize_path(url))))
                                    new_css = new_css.replace(ref, rel.replace(os.sep, "/"))
                                else:
                                    logger.warning(f"[Scheduler] Gagal simpan inline resource {abs_ref}")
                            else:
                                logger.warning(f"[Scheduler] Gagal unduh inline resource {abs_ref}")
                style_tag.string.replace_with(new_css)

            # H) Proses attribute style="background: url(...)" di semua tag
            for tag in soup.find_all(style=True):
                css_text = tag["style"]
                new_css = css_text
                for ref in re.findall(r"url\(\s*['\"]?(.*?)['\"]?\s*\)", css_text):
                    abs_ref = normalize_url(url, ref)
                    if is_same_domain(self.root_netloc, abs_ref) and not abs_ref.startswith("data:"):
                        existing = self.dbmgr.resource_exists(abs_ref)
                        if existing:
                            rel = os.path.relpath(existing,
                                                  os.path.dirname(os.path.join(*self.resdl.sanitize_path(url))))
                            new_css = new_css.replace(ref, rel.replace(os.sep, "/"))
                        else:
                            resp = self.resdl._retry_request("GET", abs_ref)
                            if resp and resp.content:
                                folder_ref, file_ref = self.resdl.sanitize_path(abs_ref)
                                ref_path = self.resdl._save_file(folder_ref, file_ref, resp.content)
                                if ref_path:
                                    self.dbmgr.add_resource(abs_ref, ref_path)
                                    rel = os.path.relpath(ref_path,
                                                          os.path.dirname(os.path.join(*self.resdl.sanitize_path(url))))
                                    new_css = new_css.replace(ref, rel.replace(os.sep, "/"))
                                else:
                                    logger.warning(f"[Scheduler] Gagal simpan inline style resource {abs_ref}")
                            else:
                                logger.warning(f"[Scheduler] Gagal unduh inline style resource {abs_ref}")
                tag["style"] = new_css

            # I) Simpan HTML final ke folder lokal
            folder_html, filename_html = self.resdl.sanitize_path(url)
            full_html_path = os.path.join(folder_html, filename_html)
            try:
                with open(full_html_path, "w", encoding="utf-8") as f:
                    f.write(soup.prettify())
                logger.info(f"[Scheduler] HTML disimpan: {full_html_path}")
            except Exception as e:
                logger.error(f"[Scheduler] Gagal simpan HTML {url}: {e}")

            # Mark visited di DB
            self.dbmgr.mark_visited(url)

        # Setelah loop berhenti, shutdown semua
        self.fetcher.shutdown()
        self.resdl.shutdown()
        logger.info("[Scheduler] Semua proses selesai. Semoga mirror-nya berhasil!")

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
    parser.add_argument("--ignore_robots", action="store_true", default=False,
                        help="Jika diset, abaikan rules di robots.txt.")
    parser.add_argument("--no_ssl_verify", action="store_true", default=False,
                        help="Jika diset, nonaktifkan verifikasi SSL.")
    parser.add_argument("--max_workers", "-w", type=int, default=10,
                        help="Jumlah worker untuk download resource statis (default: 10).")
    parser.add_argument("--timeout", "-t", type=float, default=15.0,
                        help="Timeout (detik) untuk setiap request HTTP (default: 15).")
    parser.add_argument("--max_retries", "-r", type=int, default=5,
                        help="Jumlah retry untuk request yang gagal (exponential backoff) (default: 5).")
    parser.add_argument("--delay", "-d", type=float, default=1.0,
                        help="Delay awal (detik) + jitter untuk retries & rate-limiting (default: 1).")
    parser.add_argument("--user_agent", "-u", default="MegaMirrorBot/3.5",
                        help="User-Agent header yang dipakai (default: MegaMirrorBot/3.5).")
    parser.add_argument("--force_render", action="store_true", default=False,
                        help="Jika diset, paksa gunakan Selenium Headless Chrome untuk merender halaman.")
    parser.add_argument("--selenium_wait", type=float, default=2.0,
                        help="Waktu tunggu (detik) agar JavaScript benar-benar selesai dieksekusi (default: 2).")

    args = parser.parse_args()

    root_url = args.url.rstrip("/")
    output_dir = args.output
    ignore_robots = args.ignore_robots
    verify_ssl = not args.no_ssl_verify
    max_workers = args.max_workers
    timeout = args.timeout
    max_retries = args.max_retries
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
    logger.info(f"Timeout (s)        : {timeout}")
    logger.info(f"Max retries        : {max_retries}")
    logger.info(f"Delay (s)          : {delay}")
    logger.info(f"User-Agent         : {user_agent}")
    logger.info(f"Force render JS    : {force_render}")
    logger.info(f"Selenium wait (s)  : {selenium_wait}")
    logger.info("================================================================================")

    # Buat folder output + path DB
    os.makedirs(output_dir, exist_ok=True)
    db_path = os.path.join(output_dir, "mirror.db")

    # Inisialisasi DBManager
    dbmgr = DBManager(db_path)

    # Inisialisasi Komponen
    fetcher = HTMLFetcher(user_agent=user_agent,
                          timeout=timeout,
                          max_retries=max_retries,
                          delay=delay,
                          verify_ssl=verify_ssl,
                          force_render=force_render,
                          selenium_wait=selenium_wait)

    linker = LinkExtractor(root_url=root_url,
                           root_netloc=urlparse(root_url).netloc)

    resdl = ResourceDownloader(output_dir=output_dir,
                               root_netloc=urlparse(root_url).netloc,
                               dbmgr=dbmgr,
                               user_agent=user_agent,
                               timeout=timeout,
                               max_retries=max_retries,
                               delay=delay,
                               verify_ssl=verify_ssl,
                               max_workers=max_workers)

    # Inisialisasi Scheduler & seed URL
    scheduler = CrawlScheduler(root_url=root_url,
                               output_dir=output_dir,
                               dbmgr=dbmgr,
                               fetcher=fetcher,
                               linker=linker,
                               resdl=resdl)

    # Seed (tambahkan root_url + parse sitemap)
    scheduler.seed()

    # Jalankan crawling hingga selesai
    scheduler.run()

    logger.info("MegaMirror: Proses mirror selesai. Periksa hasil di folder output.")
    sys.exit(0)
