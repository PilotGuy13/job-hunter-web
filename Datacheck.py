import pandas as pd
import requests
from urllib.parse import urlparse
from datetime import datetime

INPUT_FILE = "Global_Job_Source_Master_FINAL_Working.xlsx"
OUTPUT_FILE = "Global_Job_Source_Master_VALIDATED.xlsx"
SHEET_NAME = "Master_Source_Catalog"

URL_COLUMNS = [
    "Website URL",
    "API Documentation URL",
    "API Endpoint",
    "Source / Research URL"
]

TIMEOUT_SECONDS = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


def classify_status(status_code, error_text=""):
    if status_code is None:
        return "Error"
    if 200 <= status_code < 300:
        return "Live"
    if 300 <= status_code < 400:
        return "Redirected"
    if status_code == 401:
        return "Auth Required"
    if status_code == 403:
        return "Blocked / Forbidden"
    if status_code == 404:
        return "Not Found"
    if 500 <= status_code < 600:
        return "Server Error"
    return "Review"


def is_valid_url(url):
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url or url.lower() in ["unknown", "n/a", "none"]:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ["http", "https"] and bool(parsed.netloc)


def check_url(url):
    if not is_valid_url(url):
        return {
            "status_code": "",
            "live_flag": "No URL",
            "final_url": "",
            "notes": "Blank or invalid URL"
        }

    try:
        response = requests.get(
            url.strip(),
            headers=HEADERS,
            timeout=TIMEOUT_SECONDS,
            allow_redirects=True
        )

        return {
            "status_code": response.status_code,
            "live_flag": classify_status(response.status_code),
            "final_url": response.url,
            "notes": ""
        }

    except requests.exceptions.SSLError as e:
        return {
            "status_code": "",
            "live_flag": "SSL Error",
            "final_url": "",
            "notes": str(e)[:250]
        }

    except requests.exceptions.Timeout:
        return {
            "status_code": "",
            "live_flag": "Timeout",
            "final_url": "",
            "notes": f"Timed out after {TIMEOUT_SECONDS} seconds"
        }

    except requests.exceptions.ConnectionError as e:
        return {
            "status_code": "",
            "live_flag": "Connection Error",
            "final_url": "",
            "notes": str(e)[:250]
        }

    except Exception as e:
        return {
            "status_code": "",
            "live_flag": "Error",
            "final_url": "",
            "notes": str(e)[:250]
        }


def main():
    df = pd.read_excel(INPUT_FILE, sheet_name=SHEET_NAME)

    validation_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for col in URL_COLUMNS:
        if col not in df.columns:
            print(f"Skipping missing column: {col}")
            continue

        status_col = f"{col} HTTP Status"
        flag_col = f"{col} Live Flag"
        final_url_col = f"{col} Final URL"
        notes_col = f"{col} Validation Notes"

        statuses = []
        flags = []
        final_urls = []
        notes = []

        print(f"Validating column: {col}")

        for url in df[col]:
            result = check_url(url)
            statuses.append(result["status_code"])
            flags.append(result["live_flag"])
            final_urls.append(result["final_url"])
            notes.append(result["notes"])

        df[status_col] = statuses
        df[flag_col] = flags
        df[final_url_col] = final_urls
        df[notes_col] = notes

    df["Validation Date"] = validation_date

    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=SHEET_NAME)

    print(f"Validation complete: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()