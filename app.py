import streamlit as st

import datetime
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote_plus, urljoin

import pandas as pd
import undetected_chromedriver as uc
from bs4 import BeautifulSoup


DB_PATH = "ajac_jobs.db"

PROFILE_DIR = str(Path(__file__).with_name(".uc_profile"))


DEFAULT_BOOLEAN_QUERY = 'Assembly Installer OR Assembler Installer OR 30204 OR 30304'
BOEING_CODES = ["30204", "30304"]


@dataclass
class JobRow:
    job_title: str
    company: str
    location: str
    link: str
    match_score: int
    matched_terms: str
    qualifications: str
    scraped_at: str


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_title TEXT NOT NULL,
                company TEXT,
                location TEXT,
                link TEXT NOT NULL UNIQUE,
                match_score INTEGER NOT NULL,
                matched_terms TEXT,
                qualifications TEXT,
                scraped_at TEXT NOT NULL
            )
            """
        )
        cols = [row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()]
        if "qualifications" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN qualifications TEXT")
        conn.commit()


def upsert_jobs(jobs: List[JobRow]) -> int:
    if not jobs:
        return 0
    inserted = 0
    with sqlite3.connect(DB_PATH) as conn:
        for j in jobs:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (job_title, company, location, link, match_score, matched_terms, qualifications, scraped_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    j.job_title,
                    j.company,
                    j.location,
                    j.link,
                    j.match_score,
                    j.matched_terms,
                    j.qualifications,
                    j.scraped_at,
                ),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    return inserted


def load_jobs_df() -> pd.DataFrame:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            """
            SELECT
                job_title AS "Job Title",
                company AS "Company",
                location AS "Location",
                link AS "Link",
                match_score AS "Match Score",
                matched_terms AS "Matched Terms",
                qualifications AS "Qualifications",
                scraped_at AS "Scraped At"
            FROM jobs
            ORDER BY scraped_at DESC
            """,
            conn,
        )
    return df


def get_existing_links() -> set[str]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT link FROM jobs").fetchall()
    return {r[0] for r in rows if r and r[0]}


def build_indeed_url(query: str, location: str, radius: int) -> str:
    params = f"q={quote_plus(query)}&l={quote_plus(location)}&radius={radius}"
    return f"https://www.indeed.com/jobs?{params}"


def normalize_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def compute_title_bonus(job_title: str) -> tuple[int, str]:
    title = (job_title or "").lower()
    matched = [c for c in BOEING_CODES if c.lower() in title]
    if not matched:
        return 0, ""
    return 25, ", ".join(matched)


def extract_qualifications(description_el) -> str:
    if description_el is None:
        return ""
    text = normalize_text(description_el.get_text("\n"))
    if not text:
        return ""

    lines = [normalize_text(l) for l in text.split("\n") if normalize_text(l)]
    section_markers = {"qualifications", "requirements"}
    current_section: Optional[str] = None
    bullets: List[str] = []

    for line in lines:
        lower = line.lower().rstrip(":")
        if lower in section_markers:
            current_section = lower
            continue

        if current_section in section_markers:
            if lower in {"responsibilities", "benefits", "schedule", "compensation", "about the job"}:
                current_section = None
                continue
            if re.match(r"^[-•*\u2022]\s+", line):
                bullets.append(re.sub(r"^[-•*\u2022]\s+", "", line).strip())
                continue
            if len(line.split()) <= 3 and line.endswith(":"):
                current_section = None

    if not bullets:
        return ""
    return "\n".join(bullets[:20])


def compute_match(description_text: str) -> tuple[int, str]:
    keywords = ["no experience", "entry level", "training"]
    haystack = (description_text or "").lower()
    matched = [k for k in keywords if k in haystack]
    if not matched:
        return 0, ""
    if len(matched) >= 2:
        return 90, ", ".join(matched)
    return 75, ", ".join(matched)


def get_installed_chrome_major_version() -> Optional[int]:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for path in candidates:
        try:
            out = subprocess.check_output([path, "--version"], text=True, stderr=subprocess.STDOUT).strip()
            m = re.search(r"(\d+)\.", out)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None


def create_driver(headless: bool, use_profile: bool, user_agent: str, proxy: str) -> uc.Chrome:
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--lang=en-US")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-site-isolation-trials")
    options.add_argument("--disable-features=IsolateOrigins,site-per-process")

    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    if proxy:
        options.add_argument(f"--proxy-server={proxy}")

    if headless:
        options.add_argument("--headless=new")

    if use_profile:
        Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")

    version_main = get_installed_chrome_major_version()
    if version_main is not None:
        driver = uc.Chrome(options=options, version_main=version_main)
    else:
        driver = uc.Chrome(options=options)
    driver.set_page_load_timeout(45)
    return driver


def scrape_first_page(
    query: str,
    location: str,
    radius: int,
    max_jobs: int = 15,
    headless: bool = True,
    use_profile: bool = False,
    verification_wait_seconds: int = 60,
    user_agent: str = "",
    proxy: str = "",
) -> tuple[List[JobRow], dict]:
    url = build_indeed_url(query=query, location=location, radius=radius)
    scraped_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    driver: Optional[uc.Chrome] = None
    jobs: List[JobRow] = []
    debug: dict = {"url": url}

    try:
        existing_links = get_existing_links()
        debug["existing_links"] = len(existing_links)
        debug["headless"] = bool(headless)
        debug["use_profile"] = bool(use_profile)
        debug["proxy"] = bool(proxy)
        driver = create_driver(headless=headless, use_profile=use_profile, user_agent=user_agent, proxy=proxy)
        driver.get(url)
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        debug["page_title"] = normalize_text(soup.title.get_text(" ")) if soup.title else ""

        page_text = normalize_text(soup.get_text(" "))
        debug["looks_blocked"] = bool(
            re.search(r"(verify|verification|captcha|unusual traffic|robot|blocked|consent)", page_text, re.I)
        )

        if debug["looks_blocked"] and (not headless) and verification_wait_seconds > 0:
            debug["verification_wait_seconds"] = int(verification_wait_seconds)
            waited = 0
            while waited < verification_wait_seconds:
                time.sleep(2)
                waited += 2
                soup = BeautifulSoup(driver.page_source, "html.parser")
                debug["page_title"] = normalize_text(soup.title.get_text(" ")) if soup.title else ""
                page_text = normalize_text(soup.get_text(" "))
                debug["looks_blocked"] = bool(
                    re.search(r"(verify|verification|captcha|unusual traffic|robot|blocked|consent)", page_text, re.I)
                )
                if not debug["looks_blocked"]:
                    break
            debug["waited_seconds"] = waited

        cards = soup.select('[data-testid="slider_container"], [data-testid="job-card"]')
        if not cards:
            cards = soup.select('div.job_seen_beacon, a.tapItem, div.cardOutline')
        seen_links = set()

        for card in cards:
            if len(jobs) >= max_jobs:
                break

            title_el = card.select_one('[data-testid="aj-job-title"]')
            if title_el is None:
                title_el = card.select_one('a.jcs-JobTitle, h2.jobTitle a, h2.jobTitle span[title], h2.jobTitle a span')

            company_el = card.select_one('[data-testid="company-name"], span.companyName')
            loc_el = card.select_one('[data-testid="text-location"], div.companyLocation')

            link_el = None
            if title_el is not None:
                link_el = title_el if title_el.name == "a" else title_el.find("a")
            if link_el is None:
                link_el = card.select_one('a.jcs-JobTitle, a.tapItem, a[href*="/viewjob"], a[href*="jk="]')

            title = normalize_text(title_el.get_text(" ")) if title_el else ""
            company = normalize_text(company_el.get_text(" ")) if company_el else ""
            loc = normalize_text(loc_el.get_text(" ")) if loc_el else ""

            href = ""
            if link_el and link_el.get("href"):
                href = link_el.get("href")

            if not href:
                jk = card.get("data-jk") or card.get("data-jobkey")
                if jk:
                    href = f"/viewjob?jk={jk}"
            if not href:
                continue
            full_link = urljoin("https://www.indeed.com", href)
            if full_link in seen_links:
                continue
            seen_links.add(full_link)

            match_score = 0
            matched_terms = ""
            qualifications = ""

            is_new = full_link not in existing_links

            title_bonus, title_matched = compute_title_bonus(title)
            if title_bonus:
                match_score += title_bonus
                matched_terms = title_matched

            if is_new:
                try:
                    driver.get(full_link)
                    time.sleep(2)
                    detail_soup = BeautifulSoup(driver.page_source, "html.parser")
                    desc_el = detail_soup.select_one('#jobDescriptionText, div#jobDescriptionText')
                    desc_text = normalize_text(desc_el.get_text(" ")) if desc_el else ""
                    desc_score, desc_matched = compute_match(desc_text)
                    match_score += desc_score
                    if desc_matched:
                        if matched_terms:
                            matched_terms = f"{matched_terms}, {desc_matched}"
                        else:
                            matched_terms = desc_matched

                    qualifications = extract_qualifications(desc_el)
                except Exception:
                    pass

            jobs.append(
                JobRow(
                    job_title=title,
                    company=company,
                    location=loc,
                    link=full_link,
                    match_score=match_score,
                    matched_terms=matched_terms,
                    qualifications=qualifications,
                    scraped_at=scraped_at,
                )
            )

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    debug["jobs_found"] = len(jobs)
    return jobs, debug


st.set_page_config(page_title="Indeed Job Search", layout="wide")

init_db()

st.title("Indeed Job Search")

with st.sidebar:
    st.header("Search")
    job_title = st.text_input("Job Title", value="Assembly Installer")
    location = st.text_input("Location", value="Kent, WA")
    radius = st.selectbox("Radius (miles)", options=[10, 25, 50], index=1)
    query_override = st.text_area("Query", value=DEFAULT_BOOLEAN_QUERY, height=90)

    st.divider()
    st.subheader("Browser")
    headless = st.toggle("Headless mode", value=True)
    use_profile = st.toggle("Use persistent Chrome profile", value=True)
    verification_wait_seconds = st.selectbox(
        "Verification wait (seconds)",
        options=[0, 30, 60, 120, 180],
        index=2,
    )
    user_agent = st.text_input(
        "User-Agent (optional)",
        value="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    )
    proxy = st.text_input("Proxy (optional, e.g. http://host:port)", value="")
    scrape_clicked = st.button("Scrape New Jobs", type="primary")


if scrape_clicked:
    if not job_title.strip() or not location.strip():
        st.sidebar.error("Please provide both a Job Title and Location.")
    else:
        with st.spinner("Scraping Indeed (first page) and saving results..."):
            try:
                query = query_override.strip() or DEFAULT_BOOLEAN_QUERY
                found, debug = scrape_first_page(
                    query=query,
                    location=location,
                    radius=int(radius),
                    headless=bool(headless),
                    use_profile=bool(use_profile),
                    verification_wait_seconds=int(verification_wait_seconds),
                    user_agent=user_agent.strip(),
                    proxy=proxy.strip(),
                )
                inserted = upsert_jobs(found)
                st.success(f"Found {len(found)} jobs. Saved {inserted} new jobs to {DB_PATH}.")
                if len(found) == 0:
                    with st.expander("Debug (0 jobs found)"):
                        st.write(
                            {
                                "url": debug.get("url"),
                                "page_title": debug.get("page_title"),
                                "headless": debug.get("headless"),
                                "use_profile": debug.get("use_profile"),
                                "proxy": debug.get("proxy"),
                                "waited_seconds": debug.get("waited_seconds"),
                            }
                        )
                        if debug.get("looks_blocked"):
                            st.warning(
                                "Indeed appears to be blocking automation. "
                                "Try turning OFF headless mode (so a Chrome window opens) and keep persistent profile ON. "
                                "If a verification/consent page appears, complete it once in that window, then click Scrape again."
                            )
            except Exception as e:
                st.error(
                    "Scrape failed. This can happen if Indeed blocks automation or ChromeDriver can't start. "
                    f"Error: {e}"
                )


df = load_jobs_df()
st.subheader("Saved Jobs")


def highlight_top_matches(row: pd.Series) -> List[str]:
    q = str(row.get("Qualifications", "") or "")
    keywords = ["no experience", "entry level", "will train", "ajac"]
    is_match = any(k in q.lower() for k in keywords)
    if is_match:
        return ["background-color: #d4edda"] * len(row)
    return [""] * len(row)


styled = df.style.apply(highlight_top_matches, axis=1)

st.dataframe(
    styled,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Link": st.column_config.LinkColumn("Link"),
        "Match Score": st.column_config.NumberColumn("Match Score", format="%d"),
    },
)
