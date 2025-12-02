#  Scrapuj

**Scrapuj** (Polish for "Lets Scrape") is a versatile web scraping application built with Python and packaged for easy use. It features a user-friendly graphical interface using the **Flet** framework, allowing users to define scraping logic via JSON templates and process lists of URLs efficiently. It supports both the fast **Requests** library for static content and the robust **Playwright** browser automation tool for dynamic, JavaScript-heavy sites.

---

## ✨ Features

* **User-Friendly GUI (Flet):** Provides an intuitive, multi-step graphical interface for setup, configuration, and monitoring without touching the console.
* **Dual Engine Architecture:**
    * **Requests:** Utilizes the fast, lightweight **Requests** library for efficient scraping of **static HTML** content.
    * **Playwright:** Offers full browser automation (headless or headed) for handling **dynamic content** (JavaScript rendering) and complex interactions.
* **Intelligent Extraction:** Employs **BeautifulSoup** and **lxml** to parse and clean extracted data, preserving paragraph breaks and structure using a custom cleaning function.
* **Playwright Script Builder:** Includes a powerful visual editor for defining custom actions like **clicks**, **form inputs**, **scrolling**, **waits**, **loops**, and **conditional logic**, eliminating the need to write raw Python for basic automation.
* **Session Management:** Supports saving and loading **Playwright login sessions** (cookies) to scrape content behind authentication walls.
* **Robust Network Layer:**
    * Implements a resilient **Retry Strategy** (up to 3 times) for transient network errors (429, 500-level codes).
    * Features automatic **User-Agent rotation** and **randomized delays** (`1.0s` to `3.0s`) between requests.
* **Safe Execution:** Checks **`robots.txt`** before fetching a URL to ensure compliance with website rules.
* **Flexible Data Export Modes:**
    * **URLs Only:** Extracts matching links into a single `.txt` file.
    * **Text Only:** Exports raw extracted data for all fields to a single `.json` file.
    * **Text & Metadata (Export):** Creates a folder containing content `.txt` files, a structured **metadata `.xlsx`** (Excel) file, and a raw `.json` file for the entire batch.
* **Data Integrity:** Implements **batch saving** every 100 URLs to minimize data loss in case of interruptions or crashes.

---

## 💾 Download & Run (Recommended)

The easiest way to use Scrapuj is via the standalone executable.

1.  Navigate to the **[Releases Page](https://github.com/Rafal-P-Mazur/Scrapuj/releases)**
2.  Download full distribution ZIP and extract it.
3.  **Run `Scrapuj.exe`**. The necessary supporting folders (`templates/`, `output/`, `cookies/`) will be created automatically upon first run.

---

## 🛠️ Installation (Source Code)

This method is for developers or users who prefer to run from source code.

### Prerequisites

* Python 3.8+

### Steps

1.  **Clone the repository:**

    ```bash
    git clone [https://github.com/Rafal-P-Mazur/Scrapuj.git](https://github.com/Rafal-P-Mazur/Scrapuj.git)
    cd scrapuj
    ```

2.  **Install Core Dependencies:**

    ```bash
    pip install flet requests beautifulsoup4 pandas lxml soupsieve
    ```

3.  **For the Playwright Engine (Required for dynamic scraping):**

    Install Playwright and its necessary browser drivers:

    ```bash
    pip install playwright
    playwright install
    ```

---

## 🚀 Usage

### 1. Launch the Application

If using the source code, run the main Python file:

```bash
Scrapuj.py
 ```

### 2. Workflow Steps

The application guides you through four main steps in the GUI:

1.  **Template** 📁
    * **Select Existing Template:** Choose a **JSON template** file defining the selectors for the data you want to extract. </br>
     **OR**</br>
    * **Create New Template:** Click **"Create New Template..."** to launch the standalone **Template Creator utility**. This tool lets you visually browse a website, click on elements (text, price, link), and automatically generate stable CSS or XPath selectors which you can then save as a JSON file.
2.  **URLs** 🔗
    * Paste target **URLs** directly (one per line) or load a list from a `.txt` file.
3.  **Configuration** ⚙️
    * Set the **Output Name**.
    * Select the **Scraping Engine** (Requests or Playwright).
    * Choose the **Output Mode**:
        * **Text (JSON):** Export structured data as a single JSON file.
        * **URLs (TXT):** Scrape and save only links from the target pages.
        * **Text & Metadata (Export Folder):** Scrape the main content into individual `.txt` files and collect all metadata (including the file name) into an organized `.xlsx` spreadsheet.
    * *Engine-Specific Options:*
        * *(If Playwright selected)* Configure the **Script Builder** for custom actions and manage **Login Sessions**.
    * Click **Run Scraper**.
4.  **Scraping** 📊
    * Monitor the real-time logs.

### Output Structure (Text & Metadata Mode)

If you select the **Scrap text & metadata (Export)** mode with an output name of `product_data`, the application will create a folder structure like this in your `output/` directory:
```
output/
└── product_data/
    ├── metadane.xlsx       # Master Excel file with metadata and filenames
    ├── scraped_data.json   # Raw JSON output for all fields
    ├── 1.txt               # Content of the first scraped URL
    ├── 2.txt               # Content of the second scraped URL
    └── ...
product_data_errors.txt     # Log of failed URLs (saved in output/ for all modes)
```
---

## 📚 Templates and Selectors

The scraper supports standard **CSS selectors** and highly flexible **XPath selectors** (enabled by the `lxml` library).

A template is a simple JSON file saved in the `templates/` folder:

```json
{
  "selectors": {
    "title": "#content_inner > article.product_page > div > div.product_main > h1",
    "description": "#content_inner > article.product_page > p",
    "upc": "//th[normalize-space()='UPC']/following-sibling::td[1]",
    "type": "//th[normalize-space()='Product Type']/following-sibling::td[1]",
    "price": "//th[normalize-space()='Price (excl. tax)']/following-sibling::td[1]",
    "stock": "//th[normalize-space()='Availability']/following-sibling::td[1]"
  }
}
```
The output keys (product_name, price_text, etc.) are defined by the user in the selectors block. Fields in the metadata block are included directly in the final output files.
