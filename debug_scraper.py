"""Debug script to inspect ejudgment portal HTML structure"""
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://ejudgment.kehakiman.gov.my/ejudgmentweb"
SEARCH_URL = f"{BASE_URL}/SearchPage.aspx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()

# Step 1 - Load search page
print("Step 1 - Loading search page...")
resp = session.get(SEARCH_URL, headers=HEADERS, timeout=30)
print(f"Status: {resp.status_code}")
soup = BeautifulSoup(resp.text, "html.parser")

# Check form fields
viewstate = soup.find("input", {"id": "__VIEWSTATE"})
print(f"VIEWSTATE found: {viewstate is not None}")

# Step 2 - Submit search
print("\nStep 2 - Submitting search form...")
form_data = {
    "__VIEWSTATE": viewstate["value"] if viewstate else "",
    "__VIEWSTATEGENERATOR": soup.find("input", {"id": "__VIEWSTATEGENERATOR"})["value"] if soup.find("input", {"id": "__VIEWSTATEGENERATOR"}) else "",
    "__EVENTVALIDATION": soup.find("input", {"id": "__EVENTVALIDATION"})["value"] if soup.find("input", {"id": "__EVENTVALIDATION"}) else "",
    "__EVENTTARGET": "",
    "__EVENTARGUMENT": "",
    "ctl00$ContentPlaceHolder1$btnCari": "Cari",
}

import time
time.sleep(2)
resp2 = session.post(
    SEARCH_URL,
    data=form_data,
    headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    timeout=30,
)
print(f"Status: {resp2.status_code}")
soup2 = BeautifulSoup(resp2.text, "html.parser")

# Check all tables
tables = soup2.find_all("table")
print(f"\nTotal tables found: {len(tables)}")
for i, t in enumerate(tables):
    rows = t.find_all("tr")
    print(f"Table {i}: {len(rows)} rows, id={t.get('id','none')}, class={t.get('class','none')}")
    if rows:
        print(f"  First row text: {rows[0].get_text()[:100]}")

# Save HTML for inspection
with open("/tmp/ejudgment_result.html", "w") as f:
    f.write(resp2.text)
print("\nFull HTML saved to /tmp/ejudgment_result.html")
print(f"Page title: {soup2.title.string if soup2.title else 'none'}")
