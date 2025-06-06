# Website Cloner

This tool is designed to clone an entire website, including HTML pages, CSS, JavaScript, images, fonts, documents, and other assets. The cloned content is saved into a specified directory, preserving the original site’s directory structure so that it can be browsed offline as closely as possible to the live version.

---

## Directory Structure

```
SiteMirror/
├── Main/
│   └── app.py          # Main script for mirroring a website
├── README.md           # Documentation and usage instructions
└── License             # Project license (MIT)
```

* **SiteMirror/Main/app.py**
  The primary Python script that handles the crawling and mirroring process. All configuration and behavior are controlled via command-line arguments (CLI).
* **README.md**
  Detailed explanations on how to install, configure, and use the tool, including all updates and advanced features.
* **License**
  The `MIT License` file—permitting free use, modification, and redistribution with attribution.

---

## Key Features

1. **Complete Site Crawl & Mirror**

   * Crawls *all* internal pages (no depth limit) until no pending URLs remain.
   * Supports dynamic JavaScript rendering using Selenium Headless Chrome.
   * Automatically falls back to Requests+BeautifulSoup mode if Selenium fails or if `--force_render` is not specified.

2. **Multithreading & Concurrency**

   * Uses `ThreadPoolExecutor` to download static resources (CSS, JavaScript, images, fonts, PDFs, Office docs, etc.) in parallel.
   * The number of worker threads can be customized via the `--max_workers` argument, making it faster for large sites.

3. **Resumable Crawling with SQLite**

   * Maintains crawl state in an SQLite database (`mirror.db`) with three tables:

     * `urls(url, status, last_fetched)`: Tracks every URL that has been queued, processed, or visited.
     * `resources(url, local_path)`: Maps every static asset (CSS/JS/images/docs) to its local path.
     * `sitemap_urls(url)`: Prevents re-parsing the same sitemap multiple times.
   * If the process is interrupted, simply rerun the script with the same arguments; it will resume from the “pending” URLs.

4. **Respect (or Ignore) robots.txt**

   * By default, it reads and respects `robots.txt` rules (`Disallow`, `Crawl-delay`).
   * To override these rules, use the `--ignore_robots` flag.

5. **Automatic Sitemap Parsing**

   * If `robots.txt` includes lines starting with `Sitemap:`, the script downloads and parses that sitemap and enqueues all internal URLs found.
   * If no sitemap is found in `robots.txt`, it falls back to `/sitemap.xml`.

6. **Dual-Engine HTML Fetching**

   * **Requests + BeautifulSoup** for static HTML pages.
   * **Selenium Headless Chrome** for pages requiring JavaScript execution (dynamic content).
   * The `--force_render` argument forces the use of Selenium for *all* pages. If Selenium fails to initialize, it automatically falls back to static mode.

7. **Comprehensive Link Extraction**

   * Crawls links from:

     * `<a href="...">`
     * `<form action="...">`
     * `<button onclick="location.href='...'">` and `window.location.href` in inline JS
     * `<video src="...">`, `<audio src="...">`, `<source src="...">`
     * Inline CSS (`style="background:url(...)"`) and `<style>…</style>` blocks
     * `<meta http-equiv="refresh" content="3; url=...">`
     * Inline JavaScript patterns (`window.location.href=...`, `window.open()`, `fetch()`), captured via regex
     * `data-*` attributes (e.g., `data-url="..."`, `data-href="..."`) that hold URLs
   * All found URLs are normalized (remove fragments `#…`, convert relative to absolute, strip trailing slashes) and then only same-domain URLs are enqueued for further crawling.

8. **Resource Downloader & Link Rewriting**

   * Downloads all static resources: CSS, JavaScript, images, fonts, PDFs, Office docs, JSON, XML, and other assets.
   * **CSS Parsing**: Uses `cssutils` to parse CSS files, find all `url(...)` references inside them, download those assets, and rewrite the CSS so that all resource URLs point to local files.
   * **HTML Link Rewriting**:

     * Rewrites all `<a href>`, `<form action>`, `<button onclick>`, `<video src>`, `<audio src>`, `<source src>`, and inline‐CSS (`style="background:url(...)"`) so they point to the downloaded local files.
     * External resources (different domains) remain as absolute URLs so that offline browsing still shows external links when clicked.
   * **Local Filename Generation**:

     * If the path ends with `/` or lacks an extension, the file is saved as `index.html`.
     * If a URL has a query string (e.g., `?page=2&sort=asc`), the local filename becomes `index__page_2_sort_asc.html` to avoid naming collisions.
   * **Caching**: Before downloading each resource, it checks the `resources` table to avoid re-downloading identical files.

9. **Exponential Backoff + Jitter + Retry**

   * All HTTP requests (page fetches and resource downloads) use retry logic up to `max_retries` attempts, with exponential backoff (1× delay → 2× delay → 4× delay, etc.) mixed with slight random “jitter” to prevent request bursts.
   * Timeout and retry settings can be adjusted via `--timeout` and `--max_retries`.

10. **Detailed Logging & Log Rotation**

    * Logging levels: DEBUG, INFO, WARNING, ERROR using Python’s `logging` module.
    * Console output shows INFO level and above.
    * A log file (`logs/site_mirror.log`) captures all DEBUG and above messages and is automatically rotated (up to 5 files, each 5 MB).

---

## Installation & Requirements

1. **Python 3.8+**
   Ensure you have Python 3.8 or newer installed.

2. **Required Python Packages**
   Install dependencies with:

   ```bash
   pip install requests beautifulsoup4 cssutils selenium webdriver-manager urllib3 lxml
   ```

   * `requests`
   * `beautifulsoup4`
   * `cssutils`
   * `selenium`
   * `webdriver-manager`
   * `urllib3`
   * `lxml`

3. **Google Chrome / Chromium**

   * If you plan to use the `--force_render` option, you must have Google Chrome or Chromium installed.
   * `webdriver-manager` will automatically download and install a compatible ChromeDriver.

4. **Project Folder**
   After cloning the repository, the directory structure is:

   ```
   SiteMirror/
   ├── Main/
   │   └── app.py
   ├── README.md
   └── License
   ```

   Navigate into the `Main` folder before running the script.

---

## Usage

### 1. Open a Terminal / PowerShell

```bash
cd path/to/SiteMirror/Main
```

### 2. Basic One-Line Command

```bash
python app.py https://t2b.my.id/ --output my_full_mirror --max_workers 8 --delay 0.5 --force_render
```

Description of arguments:

| Argument                           | Explanation                                                                                                                     |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `https://t2b.my.id/`               | **Root URL** of the site to mirror. Must start with `http://` or `https://`.                                                    |
| `--output my_full_mirror`          | **Output folder**—all files will be stored under `<output>/<domain>/…`. The script will create this folder if it doesn’t exist. |
| `--max_workers 8`                  | Number of threads to download static resources (CSS/JS/images/fonts/docs). Defaults to 10.                                      |
| `--delay 0.5`                      | Base delay in seconds for exponential backoff + jitter for all requests; also serves as a rate limit.                           |
| `--force_render`                   | Use Selenium Headless Chrome to render every page’s JavaScript. If omitted, only Requests+BS4 (static mode) is used.            |
| `--ignore_robots`                  | (Optional) Ignore `robots.txt` rules entirely. By default, the script honors `robots.txt` (Disallow, Crawl-delay, etc.).        |
| `--no_ssl_verify`                  | (Optional) Disable SSL/TLS verification for `requests`. Useful if you encounter certificate errors, but less secure.            |
| `--timeout 15`                     | (Optional) HTTP request timeout in seconds (default: 15).                                                                       |
| `--max_retries 5`                  | (Optional) Maximum number of retry attempts for failed HTTP requests (default: 5).                                              |
| `--user_agent "MegaMirrorBot/3.5"` | (Optional) Custom User-Agent header to send to servers. Default is `MegaMirrorBot/3.5`.                                         |

> **Note**: For boolean flags like `--force_render`, `--ignore_robots`, or `--no_ssl_verify`, just include the flag without assigning a value (no need for `True`/`False`). If you don’t want to ignore robots.txt, simply omit `--ignore_robots`.

---

### 3. Multi-Line Command (PowerShell)

If you prefer splitting arguments across multiple lines in PowerShell, use a backtick ( `` ` `` ) at the end of each line:

```powershell
python app.py https://t2b.my.id/ `
    --output my_full_mirror `
    --max_workers 8 `
    --delay 0.5 `
    --force_render
```

> **Important**: Do not use a backslash (`\`) for line continuation in PowerShell—use backticks ( `` ` `` ).

---

## Workflow Overview

1. **Seed URLs & Sitemap**

   * The script enqueues the `root_url` into the database as `pending`.
   * It checks `robots.txt` for any `Sitemap:` entries. If found, it downloads and parses those sitemaps, enqueuing all same-domain URLs.
   * If no sitemap is found in `robots.txt`, it falls back to `/sitemap.xml`.

2. **Main Loop (BFS)**

   * Retrieves one `pending` URL (FIFO) from the database.
   * Checks `robots.txt` (unless `--ignore_robots` is set). If crawling is disallowed, marks the URL as `visited` and continues.
   * Fetches the page (via Selenium if `--force_render`, otherwise via Requests).
   * If the content type is non-HTML (e.g., PDF, image, Office doc), it downloads the resource via `ResourceDownloader`, marks it as `visited`, and continues the loop.
   * If it is HTML:

     * Uses `LinkExtractor` to find all links (both internal and external).
     * Enqueues any new same-domain links into the database as `pending`.
     * Parses the HTML with BeautifulSoup, rewrites links for `<a>`, `<form>`, `<button>`, `<video>`, `<audio>`, `<source>`, inline CSS, and scripts so they point to the local copies.
     * Submits resource download tasks (images, CSS, JS) to `ResourceDownloader`.
     * Waits for all resource downloads to finish, ensuring each tag’s `src` or `href` has been updated to the local path.
     * Processes inline `<style>` blocks and inline `style="url(...)"` attributes, downloading referenced assets and rewriting those URLs.
     * Saves the rewritten HTML to `<output>/<domain>/…`.
     * Marks the URL as `visited` in the database.

3. **ResourceDownloader**

   * Receives tasks with a `tag`, `attr` (either `src` or `href`), and `base_url`.
   * Normalizes the full URL. If it’s not HTTP/HTTPS or is external (different domain), it skips downloading.
   * Checks `resources` table to see if it’s already downloaded. If yes, returns the existing local path.
   * Otherwise, downloads the resource with retries. If it’s a CSS file, it parses it via `cssutils` to find all embedded `url(...)` references, downloads those sub-resources, rewrites the CSS, and saves the result. For other file types, it simply saves them as-is.
   * Updates the `resources` table with the local path.
   * Returns the new relative path so the calling code can rewrite the HTML tag.

4. **Automatic Resume**

   * If the script is interrupted, rerunning the same command resumes crawling from where it left off, since the database still has `pending` URLs.

5. **Final Output**

   * The entire mirrored site will be laid out under:

     ```
     <output>/
     └── <domain>/
         ├── index.html
         ├── about/
         │   └── index.html
         ├── css/
         │   └── styles.css
         ├── js/
         │   └── script.js
         ├── images/
         │   └── logo.png
         └── mirror.db             # SQLite database tracking URLs and resources
     ```
   * Pages with query strings (e.g., `?page=2`) are saved as `index__page_2.html` within their appropriate folders.

---

## Advanced Usage Examples

1. **Ignore robots.txt & Disable SSL Verification**
   To clone a site regardless of `robots.txt` rules and without verifying SSL certificates:

   ```bash
   python app.py https://t2b.my.id/ \
       --output my_full_mirror \
       --ignore_robots \
       --no_ssl_verify \
       --max_workers 12 \
       --delay 1.0 \
       --force_render
   ```

2. **Static-Only Mode (Requests + BeautifulSoup)**
   If the site is mostly static and you don’t need JavaScript rendering, omit `--force_render`:

   ```bash
   python app.py https://t2b.my.id/ \
       --output my_static_mirror \
       --max_workers 8 \
       --delay 0.5
   ```

3. **Adjust Timeout & Retries**
   For slow sites, increase timeout and retry count:

   ```bash
   python app.py https://t2b.my.id/ \
       --output slow_mirror \
       --timeout 30 \
       --max_retries 10 \
       --delay 2.0 \
       --force_render
   ```

4. **Monitor Logs**

   * The console shows high-level progress (INFO and above).
   * A detailed log file (`logs/site_mirror.log`) records all DEBUG-level messages and is automatically rotated (up to 5 files, each 5 MB).

---

## Updates & Changelog (Since the Initial Version)

* **SQLite Resume Support**: The original basic script only downloaded one page and did not support resume. Now there’s a full `mirror.db` to track crawl state and resources.
* **Advanced CSS Parsing & Rewriting**: Instead of saving CSS files as-is, we now parse with `cssutils`, download embedded assets, and rewrite CSS URLs so they point locally.
* **JavaScript Rendering Support**: Previous versions used only Requests + BeautifulSoup; now you can use Selenium Headless Chrome (dynamic mode) via `--force_render`.
* **Comprehensive Link Extraction**: The original basic script only scraped `<a>`, `<img>`, and `<script>` tags. Now it handles `<form>`, `<button onclick>`, `<video>`, `<audio>`, inline CSS, inline JS, meta-refresh, data-attribute links, and more.
* **Multithreading & Exponential Backoff**: Resource downloads run concurrently with configurable thread count, and all requests use retry + backoff + jitter.
* **Full Argparse CLI**: Users can now customize every aspect of crawling via clear command-line options (timeout, delay, number of workers, user-agent, ignoring robots.txt, and so on).

---

## Known Limitations & Notes

1. **Single-Page Applications (SPA)**
   Sites built as SPAs using React, Vue, or Angular may not be fully mirrored, because content often loads dynamically after user interactions.

   * You can increase `--selenium_wait` to wait longer for JavaScript to finish, but there’s no guaranteed way to capture every dynamic route or state.

2. **Certificates & Proxies**

   * The `--no_ssl_verify` option disables SSL/TLS certificate checks. Use this if you face certificate errors, but note it’s less secure.
   * If you are behind an HTTP/HTTPS proxy, set `HTTP_PROXY` and `HTTPS_PROXY` environment variables before running the script.

3. **Large Files & Streaming Media**

   * If the site contains very large videos or audio files, the script will attempt to download them as well. Ensure you have sufficient bandwidth and storage.
   * If you want to exclude certain file types (e.g., only HTML/CSS/JS), you would need to modify the `ResourceDownloader.download_and_rewrite` logic to skip those types.

4. **Disk Space & Runtime**

   * Crawling and mirroring can consume significant disk space, especially for large sites with thousands of pages and assets. Make sure you have adequate free space.
   * Execution time depends on site size, network speed, and the `--max_workers` and `--delay` settings.

---

## License

This project is licensed under the **MIT License**. You are free to copy, modify, and redistribute the code as long as you include attribution and keep the `License` file.

---

Thank you for using **Website Cloner**! If you have any questions, encounter bugs, or want to request features, please open an issue in the repository where you obtained this code.
