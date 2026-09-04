import logging
import platform
import subprocess
import webbrowser
from livekit.agents import function_tool, RunContext
import requests
from langchain_community.tools import DuckDuckGoSearchRun
import os
import smtplib

from typing import Optional

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

@function_tool()

async def get_weather(
    context: RunContext,
    city: str) -> str:
    """
    Get the current weather for a given location.
    """
    try:
        response = requests.get(
            f"https://wttr.in/{city}?format=3")
        if response.status_code == 200:
            logging.info(f"Weather for {city}: {response.text.strip()}")
            return response.text.strip()
        else:
            logging.error(f"Failed to get weather for {city}. Status code: {response.status_code}")
            return f"Could not retrieve weather for {city}."
    except Exception as e:
        logging.error(f"Error retrieving weather for {city}: {e}")
        return f"An error occurred while retrieving weather for {city}."

@function_tool()
async def search_web(
    context: RunContext,
    query: str) -> str:
    """
    Search the web using DuckDuckGo.
    """
    try:
        results = DuckDuckGoSearchRun().run(query)
        logging.info(f"Search results for '{query}': {results}")
        return results
    except Exception as e:
        logging.error(f"Error searching the web for '{query}': {e}")
        return f"An error occurred while searching the web for '{query}'."


# Map common spoken app names to how they're actually launched on each OS.
# Extend this as you need more apps — the key is what the LLM will pass in
# (lowercased), the value is per-platform launch info.
_APP_LAUNCH_MAP = {
    "chrome": {
        "windows": ["start", "chrome"],
        "darwin":  ["open", "-a", "Google Chrome"],
        "linux":   ["google-chrome"],
    },
    "google chrome": {
        "windows": ["start", "chrome"],
        "darwin":  ["open", "-a", "Google Chrome"],
        "linux":   ["google-chrome"],
    },
    "notepad": {
        "windows": ["notepad"],
    },
    "calculator": {
        "windows": ["calc"],
        "darwin":  ["open", "-a", "Calculator"],
        "linux":   ["gnome-calculator"],
    },
    "explorer": {
        "windows": ["explorer"],
    },
    "file explorer": {
        "windows": ["explorer"],
    },
}


@function_tool()
async def open_application(
    context: RunContext,
    app_name: str) -> str:
    """
    Open a desktop application by name, e.g. "chrome", "notepad", "calculator".
    Use this whenever the user asks JARVIS to open, launch, or start a program.
    """
    key = app_name.strip().lower()
    system = platform.system().lower()  # "windows", "darwin", "linux"

    entry = _APP_LAUNCH_MAP.get(key)
    if not entry:
        logging.warning(f"[tools] No launch mapping for app '{app_name}'")
        return (
            f"I don't have a way to open '{app_name}' yet — that application "
            f"isn't configured in my launch list."
        )

    cmd = entry.get(system)
    if not cmd:
        logging.warning(f"[tools] '{app_name}' has no launch command for OS '{system}'")
        return f"I don't know how to open '{app_name}' on this operating system."

    try:
        if system == "windows":
            # 'start' is a cmd builtin, not a real executable — needs shell=True
            subprocess.Popen(" ".join(cmd), shell=True)
        else:
            subprocess.Popen(cmd)
        logging.info(f"[tools] Launched '{app_name}' via {cmd}")
        return f"Opened {app_name}."
    except Exception as e:
        logging.error(f"[tools] Failed to launch '{app_name}': {e}")
        return f"I tried to open {app_name} but it failed: {e}"


# Common spoken site names that don't map cleanly to "name.com" (either
# the brand name differs from the domain, or it's genuinely ambiguous).
# Anything not in here just gets ".com" appended — good enough for most
# well-known sites without needing an exhaustive list.
_WEBSITE_ALIASES = {
    "youtube":   "youtube.com",
    "google":    "google.com",
    "gmail":     "mail.google.com",
    "github":    "github.com",
    "reddit":    "reddit.com",
    "amazon":    "amazon.com",
    "wikipedia": "wikipedia.org",
    "twitter":   "twitter.com",
    "x":         "x.com",
    "facebook":  "facebook.com",
    "instagram": "instagram.com",
    "netflix":   "netflix.com",
    "linkedin":  "linkedin.com",
}


@function_tool()
async def open_website(
    context: RunContext,
    site_name: str) -> str:
    """
    Open a website in the user's default browser, e.g. "YouTube",
    "Gmail", "github.com". Use this whenever the user asks JARVIS to
    open, go to, or launch a WEBSITE — as opposed to open_application,
    which is only for desktop programs like Chrome or Notepad. This
    actually opens a real browser tab; never claim a website is open
    without calling this tool and it succeeding.
    """
    raw = site_name.strip().lower()
    key = raw.replace("www.", "").split("/")[0].split(".")[0] if "." in raw else raw

    if raw.startswith("http://") or raw.startswith("https://"):
        url = raw
    elif key in _WEBSITE_ALIASES:
        url = f"https://{_WEBSITE_ALIASES[key]}"
    elif "." in raw:
        url = f"https://{raw}"
    else:
        url = f"https://{raw}.com"

    try:
        opened = webbrowser.open(url)
        if not opened:
            # webbrowser.open() returning False means it genuinely
            # couldn't find/launch a browser controller — surface that
            # honestly instead of reporting success anyway.
            logging.warning(f"[tools] webbrowser.open() returned False for {url}")
            return f"I attempted to open {url} but the browser didn't launch — please check your default browser setting."
        logging.info(f"[tools] Opened website: {url}")
        return f"Opened {url}."
    except Exception as e:
        logging.error(f"[tools] Failed to open website '{url}': {e}")
        return f"I tried to open {url} but it failed: {e}"


@function_tool()
async def send_email(
    context: RunContext,
    to_email: str,
    subject: str,
    message: str,
    cc_email: Optional[str] = None,
) -> str:

    try:
        smtp_server = "smtp.gmail.com"
        smtp_port = 587

        gmail_user = os.getenv("GMAIL_USER")
        gmail_password = os.getenv("GMAIL_APP_PASSWORD")

        if not gmail_user or not gmail_password:
            return "Gmail credentials are not configured."

        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = to_email
        msg["Subject"] = subject

        recipients = [to_email]

        if cc_email:
            msg["Cc"] = cc_email
            recipients.append(cc_email)

        msg.attach(MIMEText(message, "plain"))

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(gmail_user, gmail_password)

        server.sendmail(
            gmail_user,
            recipients,
            msg.as_string()
        )

        server.quit()

        return f"Email sent successfully to {to_email}"

    except smtplib.SMTPAuthenticationError:
        return "Authentication failed."

    except Exception as e:
        return f"Email sending failed: {str(e)}"