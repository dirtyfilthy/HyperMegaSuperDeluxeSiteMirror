import os
import sys
import shutil
import requests
import codecs
import functools
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

def get_user_input():
    url = input("Enter the URL you want to extract: ").strip()
    if not url.startswith(('http://', 'https://')):
        print("Invalid URL. Please enter a URL that starts with 'http://' or 'https://'.")
        sys.exit(1)
    return url

# Get URL from user input
url = get_user_input()
output_folder = os.path.join("Extracted Website", urlparse(url).netloc)

# Initialize a session
session = requests.session()
use_tor_network = False  # Set this flag to True if you want to use Tor network
if use_tor_network:
    session.request = functools.partial(session.request, timeout=30)
    session.proxies = {'http': 'socks5h://localhost:9050',
                        'https': 'socks5h://localhost:9050'}

# Define workspace from script location
workspace = os.path.dirname(os.path.realpath(__file__))

class Extractor:
    def __init__(self, url):
        self.url = url
        self.soup = BeautifulSoup(self.get_page_content(url), "html.parser")
        self.scraped_urls = self.scrap_all_urls()
    
    def run(self):
        self.save_files(self.scraped_urls)
        self.save_html()
    
    def get_page_content(self, url):
        try:
            response = session.get(url, verify=False)  # Disable SSL verification
            response.raise_for_status()
            response.encoding = 'utf-8'
            return response.text
        except requests.RequestException as e:
            print(f"Error fetching page content: {e}")
            return None

    def scrap_scripts(self):
        script_urls = []
        for script_tag in self.soup.find_all("script"):
            script_url = script_tag.attrs.get("src")
            if script_url:
                script_url = urljoin(self.url, script_url)
                new_url = self.url_to_local_path(script_url)
                if new_url:
                    script_tag['src'] = new_url
                    script_urls.append(script_url.split('?')[0])
        return list(set(script_urls))

    def scrap_form_attr(self):
        urls = []
        for form_tag in self.soup.find_all("form"):
            form_url = form_tag.attrs.get("action")
            if form_url:
                form_url = urljoin(self.url, form_url)
                new_url = self.url_to_local_path(form_url)
                if new_url:
                    form_tag['action'] = new_url
                    urls.append(form_url.split('?')[0])
        return list(set(urls))

    def scrap_a_attr(self):
        urls = []
        for link_tag in self.soup.find_all('a'):
            link_url = link_tag.attrs.get('href')
            if link_url:
                link_url = urljoin(self.url, link_url)
                new_url = self.url_to_local_path(link_url)
                if new_url:
                    link_tag['href'] = new_url
                    urls.append(link_url.split('?')[0])
        return list(set(urls))

    def scrap_img_attr(self):
        urls = []
        for img_tag in self.soup.find_all('img'):
            img_url = img_tag.attrs.get('src')
            if img_url:
                img_url = urljoin(self.url, img_url)
                new_url = self.url_to_local_path(img_url)
                if new_url:
                    img_tag['src'] = new_url
                    urls.append(img_url.split('?')[0])
        return list(set(urls))

    def scrap_link_attr(self):
        urls = []
        for link_tag in self.soup.find_all('link'):
            link_url = link_tag.attrs.get('href')
            if link_url:
                link_url = urljoin(self.url, link_url)
                new_url = self.url_to_local_path(link_url)
                if new_url:
                    link_tag['href'] = new_url
                    urls.append(link_url.split('?')[0])
        return list(set(urls))

    def scrap_btn_attr(self):
        urls = []
        for button in self.soup.find_all('button'):
            button_url = button.attrs.get('onclick')
            if button_url:
                button_url = button_url.replace(' ', '')
                button_url = button_url[button_url.find('location.href='):].replace('location.href=', '').strip("'\"`")
                if button_url.startswith('/'):
                    button_url = urljoin(self.url, button_url)
                    new_url = self.url_to_local_path(button_url)
                    if new_url:
                        button['onclick'] = new_url
                        urls.append(button_url.split('?')[0])
        return list(set(urls))

    def scrap_assets(self):
        asset_urls = []
        asset_urls.extend(self.scrap_form_attr())
        asset_urls.extend(self.scrap_a_attr())
        asset_urls.extend(self.scrap_img_attr())
        asset_urls.extend(self.scrap_link_attr())
        asset_urls.extend(self.scrap_btn_attr())
        return list(set(asset_urls))

    def scrap_all_urls(self):
        urls = []
        urls.extend(self.scrap_scripts())
        urls.extend(self.scrap_assets())
        return list(set(urls))

    def url_to_local_path(self, url):
        try:
            parsed_url = urlparse(url)
            path = parsed_url.path.lstrip('/')
            return os.path.join(*path.split('/'))
        except Exception as e:
            print(f"Error converting URL to local path: {e}")
            return None

    def download_file(self, url, output_path):
        try:
            url = url.split('?')[0]
            file_name = os.path.basename(url)
            if not file_name: return False

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            response = session.get(url, stream=True, verify=False)  # Disable SSL verification
            response.raise_for_status()
            with open(output_path, "wb") as file:
                file.write(response.content)
                print(f"Downloaded {file_name} to {os.path.relpath(output_path)}")
            return True
        except requests.RequestException as e:
            print(f"Error downloading file {url}: {e}")
            return False

    def save_files(self, urls):
        output_path = os.path.join(workspace, output_folder)
        shutil.rmtree(output_path, ignore_errors=True)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self.download_file, url, os.path.join(output_path, self.url_to_local_path(url))): url for url in urls}
            for future in as_completed(futures):
                future.result()

    def save_html(self):
        output_path = os.path.join(workspace, output_folder, 'index.html')
        prettyHTML = self.soup.prettify()
        try:
            with codecs.open(output_path, 'w', 'utf-8') as file:
                file.write(prettyHTML)
            print(f"Saved index.html to {os.path.relpath(output_path)}")
        except IOError as e:
            print(f"Error saving HTML file: {e}")

extractor = Extractor(url)
print(f"Extracting files from {url}\n")
extractor.run()
print(f"\nTotal extracted files: {len(extractor.scraped_urls)}")
