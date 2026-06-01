import sys
import os
import logging

# Ensure Windows terminal support for Unicode and Emojis
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

import asyncio
import requests
from bs4 import BeautifulSoup, Tag, NavigableString
from urllib.parse import urljoin
from telegram import Update, LinkPreviewOptions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from functools import wraps
import re
import html
import unicodedata
import json
import warnings
from keep_alive import keep_alive
keep_alive()

from dotenv import load_dotenv
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('TOKEN')
WEBPAGE_URL = os.getenv('WEBPAGE_URL')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
CHANNEL_OR_GROUP_ID = os.getenv('CHANNEL_OR_GROUP_ID')

MEMORY_FILE = "bot_memory.json"

def load_memory():
    """Loads bot memory from JSON file. Returns a dict."""
    if not os.path.exists(MEMORY_FILE):
        return {"chat_ids": [], "last_conference_title": ""}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "chat_ids" not in data:
                data["chat_ids"] = []
            if "last_conference_title" not in data:
                data["last_conference_title"] = ""
            return data
    except Exception as e:
        logger.error(f"Error loading memory: {e}")
        return {"chat_ids": [], "last_conference_title": ""}

def save_memory(data):
    """Saves bot memory dict to JSON file."""
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Error saving memory: {e}")

def admin_only(func):
    """Decorator to restrict handler functions to the configured ADMIN_CHAT_ID."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = None
        if update.effective_chat:
            chat_id = update.effective_chat.id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat_id
            
        if ADMIN_CHAT_ID and chat_id is not None:
            if str(chat_id) != str(ADMIN_CHAT_ID).strip():
                logger.warning(f"Unauthorized access attempt by chat_id {chat_id}")
                if update.message:
                    await update.message.reply_text("⛔ Access denied. You are not authorized to use this bot.")
                elif update.callback_query:
                    await update.callback_query.answer("⛔ Access denied.", show_alert=True)
                return
        return await func(update, context, *args, **kwargs)
    return wrapper

def format_html_links(text_line: str) -> str:
    """
    Converts HTML links <a href="URL">Text</a> in a text line to a cleaner format.
    If Text is a URL or same as URL, it replaces it with just the URL.
    Otherwise, it replaces it with 'Text: URL'.
    Also strips literal markdown brackets and parentheses surrounding URLs.
    """
    def repl(match):
        url = match.group(1).strip()
        label = match.group(2).strip()
        if label.startswith("http") or label == url or "forms.gle" in label or "lnkd.in" in label:
            return url
        return f"{label}: {url}"
        
    text_line = re.sub(r'<a\s+href="([^"]+)">([^<]+)</a>', repl, text_line)
    
    # Strip literal [URL1](URL2) patterns often found raw in description texts
    text_line = re.sub(r'\[(https?://[^\s\]]+)\]\((https?://[^\s)]+)\)', r'\1', text_line)
    # Strip standalone literal [URL] brackets
    text_line = re.sub(r'\[(https?://[^\s\]]+)\]', r'\1', text_line)
    
    return text_line

def load_chat_ids():
    """Loads all registered chat IDs from memory."""
    memory = load_memory()
    return set(memory["chat_ids"])

def save_chat_id(chat_id):
    """Saves a new chat ID to memory."""
    memory = load_memory()
    chat_str = str(chat_id)
    if chat_str not in memory["chat_ids"]:
        memory["chat_ids"].append(chat_str)
        save_memory(memory)
        logger.info(f"Registered new chat ID: {chat_id}")

def remove_chat_id(chat_id):
    """Removes a chat ID from memory."""
    memory = load_memory()
    chat_str = str(chat_id)
    if chat_str in memory["chat_ids"]:
        memory["chat_ids"].remove(chat_str)
        save_memory(memory)
        logger.info(f"Unregistered chat ID: {chat_id}")

def element_to_html(element):
    """Converts a BeautifulSoup element (like a paragraph) to Telegram-friendly basic HTML."""
    result = []
    if isinstance(element, Tag):
        for child in element.children:
            if isinstance(child, Tag):
                if child.name == 'a':
                    href = child.get('href', '')
                    href_str = href[0] if isinstance(href, list) else href
                    text = child.get_text()
                    if href_str:
                        t_clean = text.strip()
                        h_clean = href_str.strip()
                        if t_clean.startswith("http") or t_clean == h_clean or "forms.gle" in t_clean:
                            result.append(html.escape(h_clean))
                        else:
                            result.append(f'<a href="{html.escape(h_clean)}">{html.escape(text)}</a>')
                    else:
                        result.append(html.escape(text))
                elif child.name in ['strong', 'b']:
                    result.append(f"<b>{html.escape(child.get_text())}</b>")
                elif child.name in ['em', 'i']:
                    result.append(f"<i>{html.escape(child.get_text())}</i>")
                else:
                    result.append(element_to_html(child))
            elif isinstance(child, NavigableString):
                result.append(html.escape(str(child)))
    return "".join(result).strip()

def clean_title(title: str) -> str:
    """Removes standard starting emojis (📌, 🆕, 🔜), dashes, and leading spaces from the title, and normalizes styled mathematical fonts."""
    emojis_to_remove = ["📌", "🆕", "🔜"]
    
    # Decompose styled mathematical alphanumeric fonts back to normal ASCII text!
    cleaned = unicodedata.normalize('NFKD', title).strip()
    
    # Continuously strip these starting emojis
    while True:
        original = cleaned
        for emoji in emojis_to_remove:
            if cleaned.startswith(emoji):
                cleaned = cleaned[len(emoji):].strip()
        if cleaned == original:
            break
            
    # Also strip any leading formatting symbols and spaces
    cleaned = cleaned.lstrip(" -:|•")
    return cleaned

SYSTEM_PROMPT = """
You are an expert Telegram Channel Content Creator and Copywriter.
Your task is to take raw scraped content about a conference/webinar/workshop and rewrite it into a STUNNING, highly professional, premium Telegram broadcast post.

Follow these strict guidelines:
1. Title: Create a bold title at the very top framed between 🔰 emojis, e.g., 🔰 <b>CONFERENCE TITLE</b> 🔰. Always normalize styled font characters (such as math bold alphanumeric Unicode characters) back to standard uppercase letters.
2. Structure: Group the information into these sections using emojis:
   - 📝 <b>Overview:</b> (A brief, engaging 1-2 sentence description of the event)
   - 📚 <b>Key Highlights & Topics Covered:</b> (Bullet points of key topics or highlights)
   - 🎓 <b>Target Audience & Eligibility:</b> (Who can apply/attend)
   - 📅 <b>Important Dates:</b> (Extracted list of dates/deadlines. Split them into clean, separate bullet points, e.g. '• <b>Registration Deadline:</b> June 1, 2026'. Ensure all mixed text dates are extracted and formatted nicely! Do NOT include generic conversational or informational sentences here—extract ONLY the actual dates/deadlines and their corresponding labels!)
   - 💵 <b>Registration & Fees:</b> (Fees details if available)
   - 🤝 <b>Organizers & Partners:</b> (Who is organizing or sponsoring the event, including contact details if any)
   - 🔗 <b>Direct Application Link:</b> (List the registration, application, and submission links. Use '👉 <b>Link Label:</b> URL' format. Strip any literal brackets [] or parentheses () around the URLs!)
3. Formatting:
   - Use ONLY standard Telegram HTML tags: <b> (bold), <i> (italic), and <a href="..."> (links).
   - Do NOT use Markdown symbols (like * or _ or [text](url)).
   - Do NOT use unsupported HTML tags (like <p>, <ul>, <li>, <span>, <div>). Use simple '•' characters for list items.
   - Keep the links fully clickable. Ensure the URLs themselves are kept outside of <b> tags so they remain clickable (e.g. '👉 <b>Registration Link:</b> https://forms.gle/...').
4. Content Adaptation:
   - If the raw text lacks explicit sections or presents all fields inside unstructured paragraphs, use your own reasoning to classify them, organize them into clean categories, and output them as beautifully bulleted lists.
   - OMIT any section that has absolutely no information in the raw scraped text (do not show empty sections or hallucinated placeholder text).
   - If the raw text under a dates section contains long descriptive or conversational sentences alongside a date, extract ONLY the key date (e.g. '• <b>Application Deadline:</b> April 15, 2026') and discard the conversational text completely from this section.

---

### FEW-SHOT TRAINING EXAMPLES:

#### EXAMPLE 1: Detailed Structured Post
[RAW INPUT]
Scraped Title: 📌🆕🔜𝐎𝐧𝐞 𝐖𝐞𝐞𝐤 𝐎𝐧𝐥𝐢𝐧𝐞 𝐅𝐃𝐏 on: “𝐍𝐞𝐱𝐭-𝐆𝐞𝐧 𝐅𝐫𝐨𝐧𝐭𝐢𝐞𝐫𝐬: 𝐈𝐧𝐭𝐞𝐠𝐫𝐚𝐭𝐢𝐧𝐠 𝐆𝐞𝐧𝐞𝐫𝐚𝐭𝐢𝐯𝐞 𝐀𝐈, 𝐌𝐚𝐜𝐡𝐢𝐧𝐞 𝐋𝐞𝐚𝐫𝐧𝐢𝐧𝐠 & 𝐂𝐲𝐛𝐞𝐫 𝐒𝐞𝐜𝐮𝐫𝐢𝐭𝐲 (𝐍𝐆2𝐌𝐗-2026)” on 1st – 5th June 2026 in Online mode
Scraped Body:
Key Highlights:
• Generative AI & Large Language Models (LLMs)
• Advanced Machine Learning & Deep Learning
• AI-Driven Cyber Security & Threat Detection
• Real-world Case Studies & Hands-on Sessions
Who can apply ?
Faculty | Researchers | PG Students | Industry Professionals
Registration Fee: ₹300
Last Date to Apply: 30th May, 2026
Payment Here: [https://lnkd.in/gQS3cBxp?fbclid=123](https://lnkd.in/gQS3cBxp?fbclid=123)
Register Here: [https://lnkd.in/g6pCnqaG]
Contact: Dr. Lokesh Jain – +919891012540 | Ms. Megha Gupta – +919999316421
Certification: E-certificate will be provided upon successful completion.

[TARGET TELEGRAM HTML OUTPUT]
🔰 <b>ONE WEEK ONLINE FDP ON: "NEXT-GEN FRONTIERS: INTEGRATING GENERATIVE AI, MACHINE LEARNING & CYBER SECURITY (NG2MX-2026)" ON 1ST - 5TH JUNE 2026 IN ONLINE MODE</b> 🔰

📝 <b>Overview:</b>
A comprehensive One Week Online Faculty Development Program (FDP) focusing on the cutting-edge integration of Generative AI, Machine Learning, and Cyber Security. Designed for faculty, researchers, PG students, and industry professionals.

📚 <b>Key Highlights & Topics Covered:</b>
• Generative AI & Large Language Models (LLMs)
• Advanced Machine Learning & Deep Learning
• AI-Driven Cyber Security & Threat Detection
• Real-world Case Studies & Hands-on Sessions

🎓 <b>Target Audience & Eligibility:</b>
• Faculty | Researchers | PG Students | Industry Professionals

📅 <b>Important Dates:</b>
• <b>Event Dates:</b> 1st – 5th June 2026
• <b>Last Date to Apply:</b> 30th May 2026

💵 <b>Registration & Fees:</b>
• <b>Registration Fee:</b> ₹300
• <b>Certification:</b> E-certificate will be provided upon successful completion.

🤝 <b>Organizers & Partners:</b>
• <b>Contact:</b> Dr. Lokesh Jain (+919891012540) | Ms. Megha Gupta (+919999316421)

🔗 <b>Direct Application Link:</b>
👉 <b>Payment Link:</b> https://lnkd.in/gQS3cBxp?fbclid=123
👉 <b>Registration Link:</b> https://lnkd.in/g6pCnqaG


#### EXAMPLE 2: Mixed / Unstructured Paragraph Layout
[RAW INPUT]
Scraped Title: 📌🆕🔜 Online workshops on AI for researchers, LaTeX for academia, and Essentials on Machine Learning designed exclusively for PhD scholars, Bachelor’s/Master’s students, and science enthusiasts.
Scraped Body:
Register now@ https://forms.gle/QLLCpkSwK91vcnnNA
Contact: 8606512587
Topics:
1. AI Tools for Researchers with Manoj Kumar T, AI Educator
2. LaTeX for Researchers with Dr. Aarathi T, St Aloysius University
3. Introduction to Machine Learning – The Essentials with Dr. S. Gopal Krishna Patro, Sreenidhi University
Best regards,
Schrodinger’s Student Team
https://sites.google.com/view/schrodingersstudent/schedule

[TARGET TELEGRAM HTML OUTPUT]
🔰 <b>ONLINE WORKSHOPS ON AI FOR RESEARCHERS, LATEX FOR ACADEMIA, AND ESSENTIALS ON MACHINE LEARNING DESIGNED EXCLUSIVELY FOR PHD SCHOLARS, BACHELOR'S/MASTER'S STUDENTS, AND SCIENCE ENTHUSIASTS</b> 🔰

📝 <b>Overview:</b>
Online workshops on AI, LaTeX, and Machine Learning designed exclusively for PhD scholars, undergraduate/postgraduate students, and science enthusiasts.

📚 <b>Key Highlights & Topics Covered:</b>
• <b>AI Tools for Researchers:</b> with Manoj Kumar T, AI Educator
• <b>LaTeX for Researchers:</b> with Dr. Aarathi T, St Aloysius University
• <b>Introduction to Machine Learning (The Essentials):</b> with Dr. S. Gopal Krishna Patro, Sreenidhi University

🎓 <b>Target Audience & Eligibility:</b>
• PhD scholars, Bachelor’s/Master’s students, and science enthusiasts.

🤝 <b>Organizers & Partners:</b>
• <b>Organized by:</b> Schrodinger’s Student Team
• <b>Contact:</b> 8606512587
• <b>Schedule:</b> https://sites.google.com/view/schrodingersstudent/schedule

🔗 <b>Direct Application Link:</b>
👉 <b>Registration Link:</b> https://forms.gle/QLLCpkSwK91vcnnNA


#### EXAMPLE 3: Minimalist Short Post (Adapting without hallucinating sections)
[RAW INPUT]
Scraped Title: 📌🆕🔜 WIDUSHI: Call is open throughout the year, No last date.
Scraped Body:
WIDUSHI provides an excellent opportunity to Indian women scientists who are retiring or retired from jobs (Category A) or those not in regular employment (Category B) with age eligibility of 57-62 yrs and 45-62 yrs respectively.
More details: https://dst.gov.in/sites/default/files/WIDUSHI%20Guidelines.pdf

[TARGET TELEGRAM HTML OUTPUT]
🔰 <b>WIDUSHI: CALL IS OPEN THROUGHOUT THE YEAR, NO LAST DATE</b> 🔰

📝 <b>Overview:</b>
WIDUSHI provides an excellent opportunity to Indian women scientists who are retiring or retired from academic/scientific careers (Category A) or those not in regular employment (Category B).

🎓 <b>Target Audience & Eligibility:</b>
• <b>Category A (Retired/Retiring):</b> Age 57-62 years
• <b>Category B (Not in regular employment):</b> Age 45-62 years

📅 <b>Important Dates:</b>
• <b>Last Date:</b> Call is open throughout the year (No last date)

🔗 <b>Direct Application Link:</b>
👉 <b>More Details & Guidelines:</b> https://dst.gov.in/sites/default/files/WIDUSHI%20Guidelines.pdf


#### EXAMPLE 4: Dates Concatenated in a Single Paragraph Text
[RAW INPUT]
Scraped Title: 📌🆕🔜 SPECIAL SESSION 5: ADVANCES IN DATA-DRIVEN OPTIMIZATION AND AI-ENHANCED BUSINESS PROCESS MINING from June 15–19, 2026, in Milan, Italy
Scraped Body:
The growing availability of rich event-log data has opened new opportunities for combining data-driven optimization, artificial intelligence, and business process mining to achieve significant improvements in operational performance and decision-making. Process Mining has matured from a diagnostic tool into a comprehensive optimization framework integrating Process Science, Data Science, and AI. https://lion20.org We sincerely hope you will accept this invitation and contribute to making this session a valuable part of LION20 in Milan.
Please feel free to reach out if you require any further information. Full (long and short) paper deadline (To appear in the proceedings): Feb 1, 2026. Author notification: March 15, 2026. Abstract submission (for presentation only): March 1 – March 31, 2026 Abstract notification: April 7, 2026 Registration opens: March 15, 2026.

[TARGET TELEGRAM HTML OUTPUT]
🔰 <b>SPECIAL SESSION 5: ADVANCES IN DATA-DRIVEN OPTIMIZATION AND AI-ENHANCED BUSINESS PROCESS MINING FROM JUNE 15-19, 2026, IN MILAN, ITALY</b> 🔰

📝 <b>Overview:</b>
An exciting special session focusing on the combination of data-driven optimization, artificial intelligence, and business process mining to improve operational performance and decision-making, held as part of LION20 in Milan, Italy.

📚 <b>Key Highlights & Topics Covered:</b>
• Integration of Process Science, Data Science, and AI
• Event-log data analysis and operational performance optimization

📅 <b>Important Dates:</b>
• <b>Paper Submission Deadline (Long & Short):</b> Feb 1, 2026
• <b>Author Notification Date:</b> March 15, 2026
• <b>Abstract Submission (Presentation only):</b> March 1 – March 31, 2026
• <b>Abstract Notification:</b> April 7, 2026
• <b>Registration Opens:</b> March 15, 2026
• <b>Event Dates:</b> June 15–19, 2026

🤝 <b>Organizers & Partners:</b>
• <b>Venue:</b> Milan, Italy
• <b>Conference:</b> LION20 (20th International Conference on Learning and Intelligent Optimization)

🔗 <b>Direct Application Link:</b>
👉 <b>Session Details & Registration Link:</b> https://lion20.org
"""

def rewrite_post_for_channel_ai(title: str, paragraphs: list) -> str:
    """Uses Google Gemini 2.5 Flash to write a stunning, perfectly structured Telegram post."""
    if not GEMINI_API_KEY:
        return None
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    paragraphs_text = "\n\n".join(paragraphs)
    prompt = f"Scraped Title: {title}\n\nScraped Body:\n{paragraphs_text}"
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": SYSTEM_PROMPT},
                    {"text": prompt}
                ]
            }
        ]
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        res_json = response.json()
        
        # Extract response text
        ai_text = res_json['candidates'][0]['content']['parts'][0]['text']
        
        # Clean any raw markdown blocks if AI returned them
        if ai_text.startswith("```html"):
            ai_text = ai_text.replace("```html", "", 1)
        if ai_text.startswith("```"):
            ai_text = ai_text.replace("```", "", 1)
        if ai_text.endswith("```"):
            ai_text = ai_text.rsplit("```", 1)[0]
            
        return ai_text.strip()
    except Exception as e:
        logger.error(f"Gemini API formatting failed, falling back to rule-based parser: {e}")
        return None

def rewrite_post_for_channel(title: str, paragraphs: list, image_url: str) -> str:
    """
    Intelligently rewrites and structures raw scraped paragraphs into a stunning,
    highly engaging Telegram channel broadcast format.
    """
    # 1. Attempt to format with Gemini AI if API key is configured
    ai_content = rewrite_post_for_channel_ai(title, paragraphs)
    if ai_content:
        logger.info("Successfully formatted post using Gemini AI.")
        return ai_content

    # 2. Fall back to standard rule-based parsing on failure or if key is missing
    cleaned_title = clean_title(title)
    
    overview_text = []
    topics_list = []
    deadlines = []
    apply_links = []
    eligibility = []
    organizers = []
    
    for p in paragraphs:
        text = format_html_links(p.strip())
        if not text:
            continue
            
        lower_text = text.lower()
        
        # 1. Identify Deadlines / Dates (Checked FIRST to avoid "register" keyword override)
        if "deadline" in lower_text or "last date" in lower_text or "important dates" in lower_text or "date of registration" in lower_text:
            deadlines.append(text)
        # 2. Identify Links (Checked second; also matches raw http/https links)
        elif "link:" in lower_text or "register" in lower_text or "apply" in lower_text or "forms.gle" in lower_text or "http" in lower_text:
            apply_links.append(text)
        # 3. Identify Eligibility / Audience
        elif "eligibility" in lower_text or "designed for" in lower_text or "students" in lower_text or "scholars" in lower_text or "eligible" in lower_text:
            eligibility.append(text)
        # 4. Identify Sponsors / Organizers
        elif "support of" in lower_text or "grateful to" in lower_text or "in partnership with" in lower_text:
            organizers.append(text)
        # 5. Identify Topics List
        elif "topics:" in lower_text or text.startswith("-") or text.startswith("1.") or text.startswith("2.") or text.startswith("3."):
            topics_list.append(text)
        # 6. General Overview
        else:
            overview_text.append(text)
            
    # Rebuild into a beautiful channel announcement layout
    broadcast = []
    
    # Title framed between 🔰
    broadcast.append(f"🔰 <b>{cleaned_title.upper()}</b> 🔰\n")
    
    # Overview Section
    if overview_text:
        broadcast.append("📝 <b>Overview:</b>")
        for line in overview_text:
            broadcast.append(line)
        broadcast.append("")
        
    # Key Highlights / Topics Section
    if topics_list:
        broadcast.append("📚 <b>Key Highlights & Topics Covered:</b>")
        for line in topics_list:
            if "topics:" in line.lower():
                continue
            broadcast.append(line)
        broadcast.append("")
        
    # Target Audience Section
    if eligibility:
        broadcast.append("🎓 <b>Target Audience & Eligibility:</b>")
        for line in eligibility:
            broadcast.append(f"• {line}")
        broadcast.append("")
        
    # Important Dates Section
    if deadlines:
        broadcast.append("📅 <b>Important Dates:</b>")
        for line in deadlines:
            # First, normalize bullet characters (including \uf0b7 and )
            normalized = line.replace("\uf0b7", "•").replace("", "•").strip()
            
            # Strip common headers/emojis/prefixes to get clean entries
            headers_to_remove = [
                "📌", "📅", "<b>Important Dates:</b>", "Important Dates:", 
                "<b>Important Date:</b>", "Important Date:", "<b>Dates:</b>", "Dates:"
            ]
            for h in headers_to_remove:
                if h in normalized:
                    normalized = normalized.replace(h, "")
            
            normalized = normalized.strip().lstrip(" -:*•")
            
            # Split by bullet point character
            if "•" in normalized:
                entries = [e.strip() for e in normalized.split("•") if e.strip()]
            else:
                # Fallback to split by newline
                entries = [e.strip() for e in normalized.split("\n") if e.strip()]
                
            for entry in entries:
                if entry:
                    entry_cleaned = entry.lstrip("•- ")
                    if ": " in entry_cleaned:
                        parts = entry_cleaned.split(": ", 1)
                        label = parts[0].strip()
                        if len(label) < 40:
                            val = parts[1].strip()
                            label_cleaned = label.replace("<b>", "").replace("</b>", "").replace("*", "")
                            broadcast.append(f"• <b>{label_cleaned}:</b> {val}")
                            continue
                    entry_cleaned_nobold = entry_cleaned.replace("<b>", "").replace("</b>", "").replace("*", "")
                    broadcast.append(f"• <b>{entry_cleaned_nobold}</b>")
        broadcast.append("")
        
    # Supporters Section
    if organizers:
        broadcast.append("🤝 <b>Organizers & Partners:</b>")
        for line in organizers:
            broadcast.append(f"• {line}")
        broadcast.append("")
        
    # Registration Link Section
    if apply_links:
        broadcast.append("🔗 <b>Direct Application Link:</b>")
        for line in apply_links:
            cleaned_line = format_html_links(line)
            if ": " in cleaned_line:
                parts = cleaned_line.split(": ", 1)
                label = parts[0].strip()
                if len(label) < 40:
                    val = parts[1].strip()
                    label_cleaned = label.replace("<b>", "").replace("</b>", "").replace("*", "").lstrip("👉 ")
                    broadcast.append(f"👉 <b>{label_cleaned}:</b> {val}")
                    continue
            broadcast.append(f"👉 {cleaned_line.lstrip('👉 ')}")
        broadcast.append("")
        
    # Channel Footer Signature
    broadcast.append("───────────────────────")
    broadcast.append("📢 <b>Join our channel for more premium updates!</b>")
    
    return "\n".join(broadcast)

def scrape_latest_10_titles():
    """Scrapes up to 10 latest conference titles from the webpage."""
    if not WEBPAGE_URL:
        raise ValueError("WEBPAGE_URL is not set in environment variables.")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    response = requests.get(WEBPAGE_URL, headers=headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    headings = soup.find_all("h3")
    
    titles = []
    # headings[0] is general section header, actual items start from index 1
    for index in range(1, min(11, len(headings))):
        h = headings[index]
        cleaned = clean_title(h.get_text(strip=True))
        titles.append((index, cleaned))
    return titles

def scrape_conference_by_index(post_index: int):
    """Scrapes the conference details at a specific index and rewrites them into a premium Telegram Channel post."""
    if not WEBPAGE_URL:
        raise ValueError("WEBPAGE_URL is not set in environment variables.")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    response = requests.get(WEBPAGE_URL, headers=headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    headings = soup.find_all("h3")
    
    if len(headings) <= post_index:
        return None, f"Could not find post index {post_index}.", None
        
    target_heading = headings[post_index]
    if not isinstance(target_heading, Tag):
        return None, "Invalid heading tag type.", None
        
    title = clean_title(target_heading.get_text(strip=True))
    
    paragraphs = []
    image_url = None
    next_heading = headings[post_index + 1] if len(headings) > (post_index + 1) else None
    
    curr = target_heading.next_sibling
    while curr and curr != next_heading:
        if isinstance(curr, Tag):
            if curr.name == 'p':
                text_html = element_to_html(curr)
                if text_html:
                    paragraphs.append(text_html)
            elif curr.name in ['ul', 'ol']:
                for li in curr.find_all('li'):
                    if isinstance(li, Tag):
                        li_html = element_to_html(li)
                        paragraphs.append(f"- {li_html}")
            elif curr.name in ['figure', 'img']:
                img = curr if curr.name == 'img' else curr.find('img')
                if img and isinstance(img, Tag):
                    img_src = img.get('src')
                    if img_src:
                        img_src_str = img_src[0] if isinstance(img_src, list) else img_src
                        image_url = urljoin(WEBPAGE_URL, img_src_str)
                    
        curr = curr.next_sibling
        
    # Use our intelligence to rewrite the raw scraped paragraphs into a beautiful Telegram Channel post!
    markdown_content = rewrite_post_for_channel(title, paragraphs, image_url)
    return title, markdown_content, image_url

def scrape_latest_details():
    """Wrapper function to scrape details of the absolute latest conference (index 1)."""
    return scrape_conference_by_index(1)

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command handler to register the user for notifications."""
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    await update.message.reply_text(
        "👋 Welcome! You have subscribed to Web Scraping notifications.\n"
        "You will automatically receive alerts whenever a new post is added to the website.\n\n"
        "Available commands:\n"
        "/check - Manually fetch the latest conference post right now\n"
        "/latest - List the latest 10 posts with clickable query buttons\n"
        "/help - List all available commands and tips"
    )

@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command handler to display the list of all available commands."""
    await update.message.reply_html(
        "ℹ️ <b>Available Bot Commands:</b>\n\n"
        "🚀 /start - Subscribe to automatic notifications when a new post is added\n"
        "📋 /latest - List the latest 10 posts with clickable numbered buttons\n"
        "🔍 /check - Instantly check and show the absolute latest post\n"
        "❓ /help - Show this help message with all commands\n\n"
        "💡 <b>Tip:</b> You can also simply type and send a number between <b>1 and 10</b> to get full details for that post!"
    )

@admin_only
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command handler to manually check and send the latest conference details."""
    await update.message.reply_text("🔍 Checking the website for the latest post, please wait...")
    try:
        title, content, image_url = scrape_latest_details()
        if title:
            preview_options = None
            if image_url:
                content = f'<a href="{image_url}">&#8205;</a>{content}'
                preview_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True)

            if len(content) > 4000:
                content = content[:4000] + "\n\n<b>(Truncated due to length)</b>"
            
            # Add "📢 Send to Channel/Group" button below the post description (latest is index 1)
            keyboard = [[InlineKeyboardButton(text="📢 Send to Channel/Group", callback_data="broadcast_post:1")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_html(
                content,
                link_preview_options=preview_options,
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("Could not find any conferences on the site.")
    except Exception as e:
        logger.error(f"Error during manual check: {e}")
        await update.message.reply_text(f"⚠️ Error checking the website: {e}")

@admin_only
async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command handler to display the titles of the latest 10 posts with buttons."""
    await update.message.reply_text("📋 Fetching the latest 10 posts, please wait...")
    try:
        titles = scrape_latest_10_titles()
        if not titles:
            await update.message.reply_text("Could not find any posts on the website.")
            return

        message_lines = ["📋 <b>Latest 10 Workshops / Conferences:</b>\n"]
        keyboard_buttons = []
        
        for idx, title in titles:
            message_lines.append(f"{idx}️⃣ {html.escape(title)}")
            keyboard_buttons.append(
                InlineKeyboardButton(text=str(idx), callback_data=f"select_post:{idx}")
            )

        message_lines.append("\n👉 <b>Click a button below or simply reply with a number (1-10) to view full details!</b>")
        
        # Structure buttons: 2 rows of 5 buttons
        row1 = keyboard_buttons[:5]
        row2 = keyboard_buttons[5:]
        keyboard = [row1]
        if row2:
            keyboard.append(row2)
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_html(
            "\n".join(message_lines),
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error listing latest posts: {e}")
        await update.message.reply_text(f"⚠️ Error fetching titles: {e}")

@admin_only
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button clicks for selecting a post number and manual broadcasting."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data:
        return
        
    if data.startswith("select_post:"):
        post_idx = int(data.split(":")[1])
        await query.message.reply_text(f"🔍 Fetching details for Post #{post_idx}, please wait...")
        try:
            title, content, image_url = scrape_conference_by_index(post_idx)
            if title:
                preview_options = None
                if image_url:
                    content = f'<a href="{image_url}">&#8205;</a>{content}'
                    preview_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True)

                if len(content) > 4000:
                    content = content[:4000] + "\n\n<b>(Truncated due to length)</b>"
                
                # Add "📢 Send to Channel/Group" button below the post description
                keyboard = [[InlineKeyboardButton(text="📢 Send to Channel/Group", callback_data=f"broadcast_post:{post_idx}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.message.reply_html(
                    content,
                    link_preview_options=preview_options,
                    reply_markup=reply_markup
                )
            else:
                await query.message.reply_text(f"Could not find details for post #{post_idx}.")
        except Exception as e:
            logger.error(f"Error fetching post callback: {e}")
            await query.message.reply_text(f"⚠️ Error: {e}")
            
    elif data.startswith("broadcast_post:"):
        post_idx = int(data.split(":")[1])
        await query.message.reply_text(f"📡 Broadcasting Post #{post_idx} to your channel/group, please wait...")
        try:
            title, content, image_url = scrape_conference_by_index(post_idx)
            if title:
                preview_options = None
                if image_url:
                    content = f'<a href="{image_url}">&#8205;</a>{content}'
                    preview_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True)

                if len(content) > 4000:
                    content = content[:4000] + "\n\n<b>(Truncated)</b>"

                if not CHANNEL_OR_GROUP_ID:
                    await query.message.reply_text("❌ Error: CHANNEL_OR_GROUP_ID is not configured in your .env file.")
                    return

                # Support handles and numbers
                if str(CHANNEL_OR_GROUP_ID).startswith("@"):
                    c_id = str(CHANNEL_OR_GROUP_ID).strip()
                else:
                    c_id = int(CHANNEL_OR_GROUP_ID)

                await context.bot.send_message(
                    chat_id=c_id,
                    text=content,
                    parse_mode="HTML",
                    link_preview_options=preview_options
                )
                
                await query.message.reply_text(f"🎉 SUCCESS! Post #{post_idx} has been successfully broadcast to {CHANNEL_OR_GROUP_ID}!")
                
                # Disable the button and change label to "✅ Sent to Channel" to prevent double clicks
                keyboard = [[InlineKeyboardButton(text="✅ Sent to Channel", callback_data="already_sent")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_reply_markup(reply_markup=reply_markup)
            else:
                await query.message.reply_text(f"Could not find details for post #{post_idx} to broadcast.")
        except Exception as e:
            logger.error(f"Error manually broadcasting post #{post_idx}: {e}")
            await query.message.reply_text(f"⚠️ Error broadcasting post: {e}")
            
    elif data == "already_sent":
        await query.answer("This post has already been broadcast!", show_alert=True)

@admin_only
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles plain text messages. If the user types a number between 1 and 10, fetches that post."""
    text = update.message.text.strip()
    if text.isdigit():
        num = int(text)
        if 1 <= num <= 10:
            await update.message.reply_text(f"🔍 You entered #{num}. Fetching details, please wait...")
            try:
                title, content, image_url = scrape_conference_by_index(num)
                if title:
                    preview_options = None
                    if image_url:
                        content = f'<a href="{image_url}">&#8205;</a>{content}'
                        preview_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True)

                    if len(content) > 4000:
                        content = content[:4000] + "\n\n<b>(Truncated due to length)</b>"
                    
                    # Add "📢 Send to Channel/Group" button below the post description
                    keyboard = [[InlineKeyboardButton(text="📢 Send to Channel/Group", callback_data=f"broadcast_post:{num}")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await update.message.reply_html(
                        content,
                        link_preview_options=preview_options,
                        reply_markup=reply_markup
                    )
                else:
                    await update.message.reply_text(f"Could not find details for post #{num}.")
            except Exception as e:
                logger.error(f"Error fetching post by typed number: {e}")
                await update.message.reply_text(f"⚠️ Error: {e}")
            return
            
    # For any other random text, show a helpful hint
    await update.message.reply_text(
        "💡 *Tip:* Reply with a number from 1 to 10 to instantly get details for that post, or type /latest to view the full list."
    )

async def check_and_notify(application: Application):
    """Background task logic that checks the website and notifies registered users if a new post is found."""
    try:
        # Check if we have a recorded last title in memory
        memory = load_memory()
        last_title = memory["last_conference_title"]

        # Fetch the latest heading directly from HTML to compare WITHOUT calling Gemini
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(WEBPAGE_URL, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        headings = soup.find_all("h3")
        if len(headings) <= 1:
            return

        latest_title = clean_title(headings[1].get_text(strip=True))

        # If it's the first time running, just save the title to avoid spamming past posts
        if not last_title:
            memory["last_conference_title"] = latest_title
            save_memory(memory)
            logger.info(f"Initialized last conference title with: '{latest_title}'")
            return

        # If a new title is detected (compare cleaned versions to avoid emoji format mismatch loops)
        if clean_title(latest_title) != clean_title(last_title):
            logger.info(f"🆕 New post detected: '{latest_title}'! Calling Gemini & dispatching notifications...")
            
            # Call the full scraper (this now invokes Gemini to build the premium post)
            title, content, image_url = scrape_latest_details()
            if not title:
                return
                
            # Save the new clean title
            memory["last_conference_title"] = clean_title(title)
            save_memory(memory)

            # Retrieve registered targets for notifications
            targets = set(load_chat_ids())
            
            # Also add admin chat ID
            if ADMIN_CHAT_ID:
                targets.add(ADMIN_CHAT_ID.strip())
                
            # Also add any custom channel/group ID configured in .env
            if CHANNEL_OR_GROUP_ID:
                targets.add(CHANNEL_OR_GROUP_ID.strip())

            if not targets:
                logger.info("No targets to notify.")
                return

            preview_options = None
            if image_url:
                content = f'<a href="{image_url}">&#8205;</a>{content}'
                preview_options = LinkPreviewOptions(is_disabled=False, prefer_large_media=True)

            # Truncate content if too long for Telegram
            if len(content) > 4000:
                content = content[:4000] + "\n\n<b>(Truncated)</b>"

            # Broadcast message to all target chats, groups, and channels
            for target_id in targets:
                if not target_id:
                    continue
                try:
                    # Support both text handles (e.g. @mychannel) and numeric IDs
                    if str(target_id).startswith("@"):
                        c_id = str(target_id).strip()
                    else:
                        c_id = int(target_id)

                    await application.bot.send_message(
                        chat_id=c_id,
                        text=content,
                        parse_mode="HTML",
                        link_preview_options=preview_options
                    )
                    logger.info(f"Notification sent successfully to {target_id}")
                except Exception as ex:
                    logger.error(f"Failed to send notification to {target_id}: {ex}")
                    # If the bot was blocked by the user, remove them from subscribers list
                    if "Forbidden" in str(ex) or "blocked" in str(ex).lower():
                        remove_chat_id(target_id)
    except Exception as e:
        logger.error(f"Error checking website in background: {e}")

async def periodic_check_task(application: Application):
    """Asynchronous background loop running every 60 seconds."""
    logger.info("Background periodic checker started (Interval: 60s).")
    while True:
        await check_and_notify(application)
        await asyncio.sleep(60)

async def post_init(application: Application) -> None:
    """Gets called during Application startup. Launches the background periodic checker task."""
    asyncio.create_task(periodic_check_task(application))

def main() -> None:
    if not TOKEN or TOKEN == "your_telegram_bot_token_here":
        print("\n❌ Error: Please configure a valid Telegram bot 'TOKEN' in your '.env' file before running the bot.")
        sys.exit(1)

    # Start the Keep Alive Flask server for Render port binding
    keep_alive()
    
    logger.info("Starting Telegram Bot...")
    
    # Initialize the Application builder
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check))
    application.add_handler(CommandHandler("latest", latest))
    application.add_handler(CommandHandler("help", help_command))

    # Register callback query handler for inline button clicks
    application.add_handler(CallbackQueryHandler(button_click))

    # Register text message handler for plain typed numbers (1-10)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    # Run the bot until the process is interrupted
    application.run_polling()

if __name__ == '__main__':
    main()
