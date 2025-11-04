import os, time, re, email, imaplib
import logging
from bs4 import BeautifulSoup
import requests
from dotenv import load_dotenv
from datetime import datetime, timezone

# === NEW: Persistent seen accounts storage ===
SEEN_ACCOUNTS_FILE = "seen_accounts.txt"

def load_seen_accounts():
    if not os.path.exists(SEEN_ACCOUNTS_FILE):
        return set()
    with open(SEEN_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_seen_accounts(accounts):
    with open(SEEN_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        for acc in sorted(accounts):
            f.write(acc + "\n")


# === CONFIG ===
load_dotenv()  # Load variables from .env file

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASS = os.getenv("IMAP_PASS")

JAP_API_KEY = os.getenv("JAP_API_KEY")
JAP_API_URL = os.getenv("JAP_API_URL", "https://justanotherpanel.com/api/v2")


# JAP service IDs
SERVICES = {
    "likes": 9326,
    "retweets": 5062,
    "comments": 98,
    "bookmarks": 1017,
    "impressions": 1375,
}

QUANTITIES = {
    "likes": 50,
    "retweets": 10,
    "comments": 5,
    "bookmarks": 20,
    "impressions": 2000,
}

# Followers order config (NEW)
FOLLOWERS_SERVICE_ID = 9011
FOLLOWERS_QUANTITY = 300

POLL_INTERVAL = 36000  # seconds between checks
# ====================================================================


# regex to match the feed link we want
FEED_LINK_RE = re.compile(r"https?://rss\.xcancel\.com/([A-Za-z0-9_]+)/status/(\d+)#m")

def create_jap_order(service_id, link, quantity):
    payload = {
        "key": JAP_API_KEY,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity
    }
    logging.info(f"Creating JAP order: service_id={service_id}, link={link}, quantity={quantity}")
    try:
        r = requests.post(JAP_API_URL, data=payload, timeout=20)
        try:
            logging.info(f"JAP API response: {r.text}")
            return r.json()
        except Exception as e:
            logging.warning(f"Failed to parse JAP API response as JSON: {e}")
            return {"raw_text": r.text}
    except Exception as e:
        logging.error(f"Exception while creating JAP order: {e}")
        return {"error": str(e)}

def extract_feed_link_from_html(html):
    logging.info("Extracting feed link from HTML content.")
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = FEED_LINK_RE.search(href)
        if m:
            username = m.group(1)
            tweet_id = m.group(2)
            link = f"https://x.com/{username}/status/{tweet_id}"
            logging.info(f"Found tweet link in HTML: {link}")
            return link
    logging.warning("No matching feed link found in HTML.")
    return None

def process_mailbox():

    logging.info("Connecting to IMAP server.")
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    seen_accounts = load_seen_accounts()
    new_accounts_found = False
    try:
        M.login(IMAP_USER, IMAP_PASS)
        logging.info("Logged in to IMAP server.")
        M.select("INBOX")
        logging.info("Selected INBOX.")
        # search unseen from feedrabbit
        typ, data = M.search(None, '(UNSEEN FROM "subscriptions@feedrabbit.com")')
        if typ != "OK":
            logging.error(f"IMAP search failed: {typ}, {data}")
            M.logout()
            return

        ids = data[0].split()
        logging.info(f"Found {len(ids)} unseen emails from feedrabbit.")
        for msgid in ids:
            typ, msgdata = M.fetch(msgid, "(RFC822)")
            if typ != "OK":
                logging.warning(f"Failed to fetch message {msgid.decode()}")
                continue
            raw = msgdata[0][1]
            msg = email.message_from_bytes(raw)
            # prefer html part
            html = None
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/html":
                        html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                        break
            else:
                if msg.get_content_type() == "text/html":
                    html = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")

            if not html:
                # fallback to plain text
                text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
                html = "<pre>" + text + "</pre>"

            link = extract_feed_link_from_html(html)
            if not link:
                logging.warning(f"No matching feed link found in message {msgid.decode()}. Marking as seen.")
                # mark seen anyway or leave? we mark seen to avoid repeating; you can change behavior
                M.store(msgid, "+FLAGS", "\\Seen")
                continue

            logging.info(f"Found tweet link: {link}")
            # === NEW: Extract username from link ===
            # link format: https://x.com/{username}/status/{tweet_id}
            try:
                username = link.split("/")[3]
            except Exception:
                username = None

            # === NEW: Place followers order if new account ===
            if username and username not in seen_accounts:
                logging.info(f"New account detected: {username}. Placing followers order.")
                res = create_jap_order(FOLLOWERS_SERVICE_ID, link, FOLLOWERS_QUANTITY)
                logging.info(f"Followers order result: {res}")
                seen_accounts.add(username)
                new_accounts_found = True
                time.sleep(1.2)
            else:
                logging.info(f"Account {username} already seen. No followers order placed.")

            # create JAP orders for this link (existing logic)
            for metric, sid in SERVICES.items():
                qty = QUANTITIES.get(metric, 0)
                if qty <= 0:
                    logging.info(f"Skipping metric '{metric}' with quantity <= 0.")
                    continue
                res = create_jap_order(sid, link, qty)
                logging.info(f"JAP order for {metric}: {res}")
                time.sleep(1.2)

            # mark the email as seen (processed)
            M.store(msgid, "+FLAGS", "\\Seen")

        # Save updated seen accounts if any new were found
        if new_accounts_found:
            save_seen_accounts(seen_accounts)

    except Exception as e:
        logging.error(f"Exception in process_mailbox: {e}")
    finally:
        M.logout()
        logging.info("Logged out from IMAP server.")

if __name__ == "__main__":
    if not all([IMAP_USER, IMAP_PASS, JAP_API_KEY]):
        logging.critical("Configure IMAP_USER / IMAP_PASS / JAP_API_KEY in .env")
        raise SystemExit(1)
    logging.info(f"Starting IMAP monitor, polling every {POLL_INTERVAL} seconds.")
    while True:
        try:
            process_mailbox()
        except Exception as e:
            logging.error(f"Error in mailbox processing: {e}")
        logging.info(f"Sleeping for {POLL_INTERVAL} seconds before next poll.")
        time.sleep(POLL_INTERVAL)