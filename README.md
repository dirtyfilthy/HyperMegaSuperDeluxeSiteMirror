# Website Cloner

This tool is designed to clone websites by extracting their HTML, CSS, JavaScript, images, and other assets. It saves the extracted content into a specified directory, preserving the directory structure of the original site.

## Directory Structure

```
website-cloner/
├── Extracted Website/
│   └── (Cloned website files and directories will be saved here)
│
├── README.md
│
├── Main/
│   └── app.py
│
└── License
```

## Prerequisites

- Python 3.x
- `requests` library
- `beautifulsoup4` library

You can install the required libraries using pip:

```bash
pip install requests beautifulsoup4
```

## Usage

1. **Navigate to the Main Directory**: Open a terminal and navigate to the `Main` directory where `app.py` is located.

    ```bash
    cd path/to/website-cloner/Main
    ```

2. **Run the Script**: Execute the script with Python. You will be prompted to enter the URL of the website you want to clone.

    ```bash
    python app.py
    ```

3. **Enter the URL**: When prompted, enter the URL of the website you wish to extract. The URL should start with `http://` or `https://`.

    ```text
    Enter the URL you want to extract: https://example.com
    ```

4. **Check the Output**: After the script completes, the cloned website files will be saved in the `Extracted Website/` directory. The directory structure will mirror the original website.

## Features

- **HTML Extraction**: Saves the HTML of the website.
- **Assets Downloading**: Downloads CSS, JavaScript, images, and other assets.
- **Path Resolution**: Converts URLs to local paths for proper file referencing.
- **Multithreading**: Uses threading to download multiple files simultaneously.

## Notes

- **SSL Verification**: SSL verification is disabled for requests. This is to avoid issues with SSL certificates but may be less secure. If you encounter SSL issues, consider enabling SSL verification or configuring proxies.
- **Tor Network**: The script can be configured to use the Tor network for requests by setting the `use_tor_network` flag to `True` and ensuring Tor is properly configured.

## License

This project is licensed under the terms of the MIT License. See the `License` file for details.
