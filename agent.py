import imaplib
import email
import smtplib
import json
import logging
import os
import re
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
NOTIFY_EMAIL   = os.environ["NOTIFY_EMAIL"]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

SYSTEM_PROMPT = (
    "Du er en profesjonell frilansassistent. "
    "Du mottar jobbbestillinger fra Fiverr og leverer ferdig, komplett arbeid. "
    "Skriv alltid på samme språk som kunden. "
    "Lever aldri halvferdige svar eller skisser."
)

def imap_connect():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASSWORD)
    return mail

def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True)
                if raw:
                    body += raw.decode("utf-8", errors="ignore")
    else:
        raw = msg.get_payload(decode=True)
        if raw:
            body = raw.decode("utf-8", errors="ignore")
    return body.strip()

def fetch_unseen_fiverr():
    mail = imap_connect()
    mail.select("inbox")
    _, data = mail.search(None, '(UNSEEN FROM "fiverr.com")')
    ids = data[0].split()
    results = []
    for eid in ids:
        _, raw = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(raw[0][1])
        results.append({
            "id": eid,
            "subject": msg.get("Subject", ""),
            "body": get_body(msg),
        })
        mail.store(eid, "+FLAGS", "\\Seen")
    mail.logout()
    return results

def extract_task(subject, body):
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system="Trekk ut jobbdetaljer fra en Fiverr-epost. Svar KUN med JSON, ingen annen tekst.",
        messages=[{
            "role": "user",
            "content": (
                "Analyser denne Fiverr-eposen og svar med JSON:\n"
                '{"is_order": true/false, "order_id": "id eller null", '
                '"task": "hva kunden vil ha", "customer": "navn eller null"}\n\n'
                f"SUBJECT: {subject}\nBODY: {body[:1500]}"
            )
        }]
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"```json|```", "", text).strip()
    return json.loads(text)

def solve_task(task, order_id, customer):
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Fiverr-ordre: {order_id}\n"
                f"Kunde: {customer}\n\n"
                f"Oppgave:\n{task}\n\n"
                "Lever ferdig arbeid nå."
            )
        }]
    )
    return resp.content[0].text

def send_email(subject, body):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg["Subject"] = f"[Fiverr Agent] {subject}"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASSWORD)
        s.send_message(msg)

def process(mail):
    log.info(f"Behandler: {mail['subject']}")
    details = extract_task(mail["subject"], mail["body"])
    if not details.get("is_order"):
        log.info("Ikke en ny ordre, hopper over.")
        return
    task     = details.get("task", "")
    order_id = details.get("order_id", "ukjent")
    customer = details.get("customer", "Kunde")
    if not task:
        log.warning("Ingen oppgave funnet.")
        return
    log.info(f"Ordre {order_id}: sender til Claude...")
    delivery = solve_task(task, order_id, customer)
    notification = (
        f"Ordre ID: {order_id}\n"
        f"Kunde: {customer}\n"
        f"Oppgave: {task[:300]}\n\n"
        f"--- LEVERANSE ---\n{delivery}\n-----------------\n\n"
        "Ga inn pa Fiverr og lever dette til kunden."
    )
    send_email(f"Ordre {order_id} klar til levering", notification)
    log.info(f"Ordre {order_id} ferdig.")

def main():
    log.info("Fiverr Agent starter. Sjekker hvert %s sek.", CHECK_INTERVAL)
    while True:
        try:
            mails = fetch_unseen_fiverr()
            if mails:
                log.info(f"Fant {len(mails)} ny(e) Fiverr-epost(er).")
                for m in mails:
                    process(m)
            else:
                log.info("Ingen nye Fiverr-eposter.")
        except Exception as e:
            log.error(f"Feil: {e}")
            try:
                send_email("Agent-feil", f"Noe gikk galt:\n{e}")
            except Exception:
                pass
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
