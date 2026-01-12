import os
import time
import asyncio
from dotenv import load_dotenv
from openai import OpenAI
from jotform import JotformAPIClient
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
import json
import re

# Import database module
from database import (
    init_db, get_current_gb, set_current_gb, clear_current_gb,
    get_current_gb_info, is_admin, add_admin, remove_admin,
    get_all_admins, get_admin_count, get_deadline, set_deadline,
    clear_deadline, get_deadline_info, get_vendors, set_vendors,
    clear_vendors, get_vendors_info, get_status, set_status,
    clear_status, get_status_info, add_form_to_list, remove_form_from_list,
    get_forms_list, is_form_in_list, log_event, get_event_count,
    get_analytics_summary, get_recent_events, subscribe_to_reminders,
    unsubscribe_from_reminders, is_subscribed_to_reminders,
    get_all_reminder_subscribers, get_reminder_subscriber_count,
    log_sent_reminder
)
from datetime import datetime, timedelta
from dateutil import parser as date_parser

load_dotenv()

# Cache TTL configuration (default: 5 minutes)
CACHE_TTL_SECONDS = int(os.getenv('CACHE_TTL_SECONDS', 300))
OPENAI_TIMEOUT_SECONDS = int(os.getenv('OPENAI_TIMEOUT_SECONDS', 30))
OPENAI_MAX_RETRIES = int(os.getenv('OPENAI_MAX_RETRIES', 3))
OPENAI_BACKOFF_SECONDS = float(os.getenv('OPENAI_BACKOFF_SECONDS', 1))

# Admin contact for problem reports
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'Emilycarolinemarch')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', None)  # Optional: Admin's Telegram chat ID for direct forwarding

# Conversation states for multi-step interactions
REPORT_WAITING_INVOICE, REPORT_WAITING_DESCRIPTION, REPORT_WAITING_PHOTO = range(3)
# Check status conversation states
STATUS_WAITING_FORM, STATUS_WAITING_IDENTIFIER = range(10, 12)

# Conversation timeout (in seconds) - conversations expire after this time
CONVERSATION_TIMEOUT = int(os.getenv('CONVERSATION_TIMEOUT_SECONDS', 300))  # 5 minutes default

# Analytics event types
EVENT_COMMAND = 'command'
EVENT_MESSAGE = 'message'
EVENT_FAQ_MATCH = 'faq_match'
EVENT_PRODUCT_SEARCH = 'product_search'
EVENT_ORDER_LOOKUP = 'order_lookup'
EVENT_PROBLEM_REPORT = 'problem_report'
EVENT_ADMIN_ACTION = 'admin_action'
EVENT_ERROR = 'error'
EVENT_GREETING = 'greeting'
EVENT_REMINDER_SENT = 'reminder_sent'


class ExternalServiceError(Exception):
    """Raised when an external service call fails after retries."""


async def track_event(event_type: str, user=None, data: dict = None):
    """
    Track an analytics event.
    This is a fire-and-forget helper - errors are logged but don't affect the caller.
    """
    try:
        user_id = user.id if user else None
        username = (user.username or user.first_name) if user else None
        event_data = json.dumps(data) if data else None
        await log_event(event_type, event_data, user_id, username)
    except Exception as e:
        print(f"[DEBUG] track_event failed: {e}")


async def notify_admins(context, message: str, photo_file_id: str = None):
    """
    Send a notification to all admins.
    If ADMIN_CHAT_ID is set, sends to that chat. Otherwise, sends to all registered admins.
    """
    sent_count = 0

    # Try ADMIN_CHAT_ID first
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=message)
            if photo_file_id:
                await context.bot.send_photo(chat_id=int(ADMIN_CHAT_ID), photo=photo_file_id)
            sent_count += 1
        except Exception as e:
            print(f"[ERROR] notify_admins - Failed to send to ADMIN_CHAT_ID: {e}")

    # Also notify all registered admins
    try:
        admins = await get_all_admins()
        for admin in admins:
            try:
                # Only send if different from ADMIN_CHAT_ID
                if ADMIN_CHAT_ID and str(admin['user_id']) == str(ADMIN_CHAT_ID):
                    continue
                await context.bot.send_message(chat_id=admin['user_id'], text=message)
                if photo_file_id:
                    await context.bot.send_photo(chat_id=admin['user_id'], photo=photo_file_id)
                sent_count += 1
            except Exception as e:
                print(f"[DEBUG] notify_admins - Failed to send to admin {admin['user_id']}: {e}")
    except Exception as e:
        print(f"[ERROR] notify_admins - Failed to get admins: {e}")

    return sent_count


def validate_date_input(date_string: str):
    """
    Validate and parse a date string.
    Returns (parsed_date, error_message) tuple.
    """
    if not date_string or not date_string.strip():
        return None, "Please provide a date."

    try:
        # Try to parse with dateutil for flexibility
        parsed = date_parser.parse(date_string, fuzzy=True)
        return parsed, None
    except Exception:
        return None, f"Could not parse '{date_string}' as a date. Try formats like 'January 15, 2025' or '01/15/2025'."


def validate_form_id(form_id: str, available_forms: dict):
    """
    Validate a form ID exists.
    Returns (form_id, form_title, error_message) tuple.
    """
    if not form_id:
        return None, None, "Please provide a form ID."

    if form_id in available_forms:
        return form_id, available_forms[form_id].get('title', 'Unknown'), None

    # Try to find by title
    form_id_lower = form_id.lower()
    for fid, fdata in available_forms.items():
        if form_id_lower in fdata.get('title', '').lower():
            return fid, fdata.get('title', 'Unknown'), None

    return None, None, f"Could not find a form matching '{form_id}'."


def log_error(context, error, extra=None):
    print(f"[ERROR] {context} - {error}")
    if extra:
        for key, value in extra.items():
            print(f"[ERROR] {context} - {key}: {value}")


def extract_moq_from_description(description):
    """
    Extract MOQ (Minimum Order Quantity) from product description text.
    Returns the MOQ string if found, None otherwise.

    Common patterns:
    - "MOQ: 10", "MOQ 10", "MOQ:10"
    - "Min order: 5", "Min order 5", "Minimum order: 5"
    - "Minimum order quantity: 10"
    - "Min qty: 5", "Min qty 5"
    - "Minimum: 10 units", "Min: 5"
    - "10 unit minimum", "5 vial minimum"
    """
    if not description or description == 'N/A':
        return None

    description_lower = description.lower()

    # Pattern list - ordered by specificity (most specific first)
    patterns = [
        # MOQ explicit patterns
        r'moq[:\s]*(\d+(?:\s*(?:units?|vials?|bottles?|pieces?|pcs?|kits?))?)',
        r'minimum\s+order\s+(?:quantity|qty)?[:\s]*(\d+(?:\s*(?:units?|vials?|bottles?|pieces?|pcs?|kits?))?)',
        r'min(?:imum)?\s+order[:\s]*(\d+(?:\s*(?:units?|vials?|bottles?|pieces?|pcs?|kits?))?)',
        r'min(?:imum)?\s+qty[:\s]*(\d+(?:\s*(?:units?|vials?|bottles?|pieces?|pcs?|kits?))?)',
        r'min[:\s]+(\d+(?:\s*(?:units?|vials?|bottles?|pieces?|pcs?|kits?))?)',
        # Reverse patterns: "10 unit minimum"
        r'(\d+)\s*(?:units?|vials?|bottles?|pieces?|pcs?|kits?)?\s+min(?:imum)?(?:\s+order)?',
        # Simple "minimum X" at word boundary
        r'\bminimum[:\s]*(\d+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, description_lower)
        if match:
            moq_value = match.group(1).strip()
            # Clean up and format the result
            if moq_value:
                # If it's just a number, add "units"
                if moq_value.isdigit():
                    moq_value = f"{moq_value} units"
                print(f"[DEBUG] extract_moq_from_description - Found MOQ: '{moq_value}' using pattern: {pattern}")
                return moq_value

    return None


def call_openai_with_retry(operation_name, call_fn, max_retries=OPENAI_MAX_RETRIES,
                           backoff_seconds=OPENAI_BACKOFF_SECONDS,
                           timeout_seconds=OPENAI_TIMEOUT_SECONDS):
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return call_fn(timeout=timeout_seconds)
        except Exception as e:
            last_error = e
            log_error(f"{operation_name} attempt {attempt}/{max_retries}", e)
            if attempt >= max_retries:
                raise ExternalServiceError(
                    f"{operation_name} failed after {max_retries} attempts"
                ) from e
            sleep_seconds = backoff_seconds * (2 ** (attempt - 1))
            print(f"[DEBUG] {operation_name} - retrying in {sleep_seconds:.1f}s")
            time.sleep(sleep_seconds)

    raise ExternalServiceError(f"{operation_name} failed after retries") from last_error

# =============================================================================
# STATIC FAQ SYSTEM
# =============================================================================
# Common questions and answers that don't require JotForm/API lookups
FAQ_DATABASE = {
    # Group Buy Basics
    "what is a group buy": {
        "keywords": ["what is a group buy", "what's a group buy", "what is gb", "what's a gb", "explain group buy", "how does group buy work", "how do group buys work"],
        "answer": "A Group Buy (GB) is a collective purchasing arrangement where multiple buyers pool their orders together to get better pricing from vendors. By ordering in bulk as a group, we can negotiate lower prices than individual orders would receive. Each GB typically has a deadline for orders and an estimated delivery timeframe."
    },
    "what is bohemia": {
        "keywords": ["what is bohemia", "what's bohemia", "who is bohemia", "about bohemia"],
        "answer": "Bohemia is a Group Buy community that organizes collective purchases to help members get better pricing on products. We coordinate orders, handle vendor relationships, and manage the distribution process."
    },

    # Order Process
    "how to order": {
        "keywords": ["how to order", "how do i order", "how can i order", "place an order", "make an order", "ordering process", "how to place order", "how to buy", "how do i buy", "want to order", "want to place an order", "i want to order", "i want to place", "i'd like to order", "i would like to order", "ready to order", "submit an order", "fill out order"],
        "answer": "To place an order, use /jotform to get the current order form link!\n\nThe ordering process:\n1. Click the JotForm link\n2. Fill out the form with your product selections\n3. Submit your order before the deadline\n4. Follow the payment instructions provided\n5. Wait for shipping confirmation\n\nIf you need help with a specific step, please ask!"
    },
    "how to pay": {
        "keywords": ["how to pay", "payment method", "payment options", "how do i pay", "accepted payment", "pay for order", "payment instructions"],
        "answer": "Payment instructions are provided after you submit your order form. Typically, payment details will be sent via DM or included in the order confirmation. If you haven't received payment instructions after submitting your order, please DM an admin:\n- @Emilycarolinemarch\n- @Davesauce"
    },

    # Shipping & Delivery
    "shipping info": {
        "keywords": ["shipping", "ship to", "delivery", "where do you ship", "shipping countries", "international shipping", "do you ship to"],
        "answer": "Shipping details vary by Group Buy and vendor. Generally:\n- Shipping is handled after the GB closes and products are received\n- International shipping is available but may have longer delivery times\n- Tracking information is provided when available\n\nFor specific shipping questions about your order, please DM an admin."
    },
    "package seized": {
        "keywords": ["seized", "customs", "package seized", "confiscated", "customs issue", "stopped at customs", "lost package"],
        "answer": "If your package is seized by customs:\n1. Don't panic - this occasionally happens with international shipments\n2. Contact an admin immediately with your order details\n3. We'll work with you on reship options based on the situation\n\nPlease DM an admin:\n- @Emilycarolinemarch\n- @Davesauce"
    },

    # Policies
    "refund policy": {
        "keywords": ["refund", "money back", "return", "cancel order", "cancellation", "get refund"],
        "answer": "Refund and cancellation policies vary by Group Buy. Generally:\n- Orders can be modified/cancelled before the GB deadline\n- After the deadline, changes may not be possible as orders are already placed with vendors\n- Issues with received products are handled case-by-case\n\nFor specific refund requests, please DM an admin with your order details."
    },
    # Note: MOQ questions are now handled by the intelligent product lookup system
    # which extracts MOQ from product descriptions and uses ChatGPT to answer
    # specific product MOQ queries. General "what is MOQ" questions still need handling.
    "what is moq": {
        "keywords": ["what is moq", "what's moq", "what does moq mean", "moq meaning", "what is minimum order quantity"],
        "answer": "MOQ stands for Minimum Order Quantity - it's the smallest amount of a product you can order. MOQs vary by product. Ask me about a specific product to get its MOQ! For example: 'What's the MOQ for Retatrutide?'"
    },

    # Contact & Support
    "contact admin": {
        "keywords": ["contact", "admin", "support", "help", "who to contact", "dm admin", "talk to admin", "speak to admin", "customer service"],
        "answer": "For support, please DM one of our admins:\n- @Emilycarolinemarch\n- @Davesauce\n\nOr post your question in the Telegram group for community assistance."
    },
    "group rules": {
        "keywords": ["rules", "group rules", "guidelines", "what are the rules", "community rules"],
        "answer": "Please refer to the pinned messages in the Telegram group for the full list of community rules and guidelines. Key points:\n- Be respectful to all members\n- No spam or self-promotion\n- Keep discussions on-topic\n- Follow admin instructions\n\nViolations may result in warnings or removal from the group."
    },

    # Product & Quality
    "quality assurance": {
        "keywords": ["quality", "legit", "legitimate", "real", "authentic", "trustworthy", "safe", "is this safe", "is this legit"],
        "answer": "We work with verified vendors and many products come with Certificates of Analysis (COA) or third-party test results. For specific product testing information, please DM an admin:\n- @Emilycarolinemarch\n- @Davesauce"
    },

    # Timing
    "when next gb": {
        "keywords": ["next gb", "next group buy", "upcoming gb", "future gb", "when is next", "new gb"],
        "answer": "New Group Buys are announced in the Telegram group. Keep an eye on announcements and pinned messages for upcoming GBs. You can also ask about the 'current GB' to see what's available now."
    },
    "order status": {
        "keywords": ["order status", "where is my order", "track order", "tracking", "order update", "when will i receive", "when does my order"],
        "answer": "For order status updates:\n1. Check any tracking information provided to you\n2. Review announcements in the group for general GB updates\n3. For specific order inquiries, please DM an admin with your order details:\n   - @Emilycarolinemarch\n   - @Davesauce"
    }
}

def check_faq_match(message_text):
    """
    Check if the user's message matches any FAQ entry.
    Returns the FAQ answer if matched, None otherwise.
    """
    message_lower = message_text.lower().strip()

    # Remove common question words for better matching
    clean_message = message_lower
    for word in ['can you tell me', 'could you tell me', 'please tell me', 'i want to know', 'i need to know', 'can you explain', 'please explain']:
        clean_message = clean_message.replace(word, '')
    clean_message = clean_message.strip()

    best_match = None
    best_score = 0

    for faq_key, faq_data in FAQ_DATABASE.items():
        for keyword in faq_data['keywords']:
            # Check for exact keyword match
            if keyword in message_lower or keyword in clean_message:
                # Score based on keyword length (longer = more specific = better match)
                score = len(keyword)
                if score > best_score:
                    best_score = score
                    best_match = faq_data['answer']

    if best_match:
        print(f"[DEBUG] check_faq_match - FAQ match found with score {best_score}")

    return best_match
jotform = JotformAPIClient(os.getenv('JOTFORM_API_KEY'))
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

class JotFormHelper:
    def __init__(self):
        self.client = JotformAPIClient(os.getenv('JOTFORM_API_KEY'))
        self.forms_cache = {}
        self.products_cache = {}  # products are stored here
        self.form_metadata_cache = {}  # Store full form metadata including vendor info
        # Cache timestamps for TTL management
        self.forms_cache_timestamp = 0
        self.products_cache_timestamps = {}  # per-form timestamps
        self.form_metadata_cache_timestamps = {}  # per-form timestamps
        self.max_retries = int(os.getenv('JOTFORM_MAX_RETRIES', 3))
        self.backoff_seconds = float(os.getenv('JOTFORM_BACKOFF_SECONDS', 1))

    def _call_with_retry(self, operation_name, call_fn):
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return call_fn()
            except Exception as e:
                last_error = e
                log_error(f"JotFormHelper.{operation_name} attempt {attempt}/{self.max_retries}", e)
                if attempt >= self.max_retries:
                    raise ExternalServiceError(
                        f"JotFormHelper.{operation_name} failed after {self.max_retries} attempts"
                    ) from e
                sleep_seconds = self.backoff_seconds * (2 ** (attempt - 1))
                print(f"[DEBUG] JotFormHelper.{operation_name} - retrying in {sleep_seconds:.1f}s")
                time.sleep(sleep_seconds)

        raise ExternalServiceError(
            f"JotFormHelper.{operation_name} failed after retries"
        ) from last_error

    def is_cache_expired(self, timestamp):
        """Check if a cache entry has expired based on TTL."""
        return (time.time() - timestamp) > CACHE_TTL_SECONDS

    def clear_all_caches(self):
        """Force clear all caches - useful for admin refresh commands."""
        self.forms_cache = {}
        self.products_cache = {}
        self.form_metadata_cache = {}
        self.forms_cache_timestamp = 0
        self.products_cache_timestamps = {}
        self.form_metadata_cache_timestamps = {}
        print(f"[DEBUG] JotFormHelper.clear_all_caches - All caches cleared")

    def get_all_forms(self, force_refresh=False):
        """Get list of all forms with TTL-based caching."""
        # Check if cache is valid
        cache_valid = (
            self.forms_cache and
            not self.is_cache_expired(self.forms_cache_timestamp) and
            not force_refresh
        )

        if cache_valid:
            print(f"[DEBUG] JotFormHelper.get_all_forms - Using cached forms ({len(self.forms_cache)} forms, age: {int(time.time() - self.forms_cache_timestamp)}s)")
            return self.forms_cache

        # Cache expired or empty - fetch fresh data
        print(f"[DEBUG] JotFormHelper.get_all_forms - Fetching forms from JotForm API (cache expired or forced refresh)")
        try:
            forms = self._call_with_retry("get_forms", self.client.get_forms)
            print(f"[DEBUG] JotFormHelper.get_all_forms - Retrieved {len(forms)} forms from API")

            # Clear old cache
            self.forms_cache = {}

            for form in forms:
                # Get latest submission date for each form
                latest_submission = None
                try:
                    submissions = self._call_with_retry(
                        f"get_form_submissions:{form['id']}",
                        lambda: self.client.get_form_submissions(form['id'], limit=1, order_by='created_at')
                    )
                    if submissions and len(submissions) > 0:
                        latest_submission = submissions[0].get('created_at', '')
                        print(f"[DEBUG] JotFormHelper.get_all_forms - Form {form['id']} latest submission: {latest_submission}")
                except ExternalServiceError as e:
                    log_error(
                        "JotFormHelper.get_all_forms - Failed to fetch submissions",
                        e,
                        {"form_id": form.get('id')}
                    )
                except Exception as e:
                    log_error(
                        "JotFormHelper.get_all_forms - Could not fetch submissions",
                        e,
                        {"form_id": form.get('id')}
                    )

                self.forms_cache[form['id']] = {
                    'id': form['id'],
                    'title': form['title'],
                    'created': form.get('created_at', ''),
                    'latest_submission': latest_submission or form.get('created_at', '')
                }
                print(f"[DEBUG] JotFormHelper.get_all_forms - Added form: {form['id']} - {form['title']}")

            # Update cache timestamp
            self.forms_cache_timestamp = time.time()
            print(f"[DEBUG] JotFormHelper.get_all_forms - Cache refreshed at {self.forms_cache_timestamp}")

        except ExternalServiceError as e:
            log_error("JotFormHelper.get_all_forms - Error fetching forms", e)
            # If we have stale cache data, return it rather than nothing
            if self.forms_cache:
                print(f"[DEBUG] JotFormHelper.get_all_forms - Returning stale cache due to error")
                return self.forms_cache
            raise
        except Exception as e:
            log_error("JotFormHelper.get_all_forms - Error fetching forms", e)
            if self.forms_cache:
                print(f"[DEBUG] JotFormHelper.get_all_forms - Returning stale cache due to error")
                return self.forms_cache
            raise

        return self.forms_cache

    def get_form_metadata(self, form_id, force_refresh=False):
        """Get full form metadata including vendor, questions, and other properties with TTL-based caching."""
        # Check if cache is valid for this form
        cache_timestamp = self.form_metadata_cache_timestamps.get(form_id, 0)
        cache_valid = (
            form_id in self.form_metadata_cache and
            not self.is_cache_expired(cache_timestamp) and
            not force_refresh
        )

        if cache_valid:
            print(f"[DEBUG] JotFormHelper.get_form_metadata - Using cached metadata for form {form_id} (age: {int(time.time() - cache_timestamp)}s)")
            return self.form_metadata_cache[form_id]

        try:
            print(f"[DEBUG] JotFormHelper.get_form_metadata - Fetching full metadata for form {form_id}")

            # Get form properties
            properties = self.client.get_form_properties(form_id)

            # Get form questions to find vendor info
            questions = self.client.get_form_questions(form_id)

            metadata = {
                'properties': properties,
                'vendor': None,
                'suppliers': [],
                'notes': None,
                'deadline': None,
                'closing_date': None
            }

            # Try to extract vendor/supplier information and deadline from questions
            for q_id, question in questions.items():
                q_text = question.get('text', '').lower()
                q_name = question.get('name', '').lower()

                # Look for vendor/supplier fields
                if 'vendor' in q_text or 'vendor' in q_name or 'supplier' in q_text or 'supplier' in q_name:
                    # Check if it has a default value or text
                    vendor_value = question.get('text', '') or question.get('defaultValue', '')
                    if vendor_value and 'vendor' not in vendor_value.lower():
                        metadata['vendor'] = vendor_value
                        metadata['suppliers'].append(vendor_value)
                        print(f"[DEBUG] JotFormHelper.get_form_metadata - Found vendor: {vendor_value}")

                # Look for deadline/closing date
                if any(keyword in q_text or keyword in q_name for keyword in ['deadline', 'close', 'closing', 'end date', 'due date']):
                    deadline_value = question.get('text', '') or question.get('defaultValue', '')
                    if deadline_value:
                        metadata['deadline'] = deadline_value
                        metadata['closing_date'] = deadline_value
                        print(f"[DEBUG] JotFormHelper.get_form_metadata - Found deadline: {deadline_value}")

                # Look for notes or additional info
                if 'note' in q_text or 'note' in q_name or 'info' in q_text:
                    metadata['notes'] = question.get('text', '')

            # Also check form title for vendor info (sometimes included there)
            form_title = properties.get('title', '')
            if '-' in form_title or '|' in form_title:
                # Sometimes vendors are in the title like "January GB - VendorName"
                parts = form_title.replace('|', '-').split('-')
                if len(parts) > 1:
                    potential_vendor = parts[-1].strip()
                    if potential_vendor and not any(month in potential_vendor.lower() for month in
                        ['january', 'february', 'march', 'april', 'may', 'june',
                         'july', 'august', 'september', 'october', 'november', 'december']):
                        if not metadata['vendor']:
                            metadata['vendor'] = potential_vendor
                        if potential_vendor not in metadata['suppliers']:
                            metadata['suppliers'].append(potential_vendor)

            # Update cache and timestamp
            self.form_metadata_cache[form_id] = metadata
            self.form_metadata_cache_timestamps[form_id] = time.time()
            print(f"[DEBUG] JotFormHelper.get_form_metadata - Cached metadata for {form_id}: vendor={metadata['vendor']}, suppliers={metadata['suppliers']}, deadline={metadata['deadline']}")
            return metadata

        except Exception as e:
            print(f"[ERROR] JotFormHelper.get_form_metadata - Error: {e}")
            import traceback
            traceback.print_exc()
            # Return stale cache if available
            if form_id in self.form_metadata_cache:
                print(f"[DEBUG] JotFormHelper.get_form_metadata - Returning stale cache due to error")
                return self.form_metadata_cache[form_id]
            return {'properties': {}, 'vendor': None, 'suppliers': [], 'notes': None, 'deadline': None, 'closing_date': None}
    def find_form_by_month(self, month):
        # Find a form that matches a month name
        forms = self.get_all_forms()
        month_lower = month.lower()

        for form_id, form_data in forms.items():
            title_lower = form_data['title'].lower()
            if month_lower in title_lower and 'order' in title_lower:
                return form_id
        return None
    def get_products(self, form_id, force_refresh=False):
        """Get products from a specific form with TTL-based caching."""
        # Check if cache is valid for this form
        cache_timestamp = self.products_cache_timestamps.get(form_id, 0)
        cache_valid = (
            form_id in self.products_cache and
            not self.is_cache_expired(cache_timestamp) and
            not force_refresh
        )

        if cache_valid:
            print(f"[DEBUG] JotFormHelper.get_products - Using cached products for form {form_id} (age: {int(time.time() - cache_timestamp)}s)")
            return self.products_cache[form_id]

        try:
            print(f"[DEBUG] JotFormHelper.get_products - Fetching properties for form {form_id} (cache expired or forced refresh)")
            properties = self._call_with_retry(
                f"get_form_properties:{form_id}",
                lambda: self.client.get_form_properties(form_id)
            )
            raw_products = properties.get('products', [])
            print(f"[DEBUG] JotFormHelper.get_products - Raw products count: {len(raw_products)}")
            clean_products = self.clean_products(raw_products)
            print(f"[DEBUG] JotFormHelper.get_products - Clean products count: {len(clean_products)}")

            # Update cache and timestamp
            self.products_cache[form_id] = clean_products
            self.products_cache_timestamps[form_id] = time.time()
            print(f"[DEBUG] JotFormHelper.get_products - Cache refreshed for form {form_id}")

            return clean_products
        except ExternalServiceError as e:
            log_error("JotFormHelper.get_products - Error fetching products", e, {"form_id": form_id})
            import traceback
            traceback.print_exc()
            # Return stale cache if available
            if form_id in self.products_cache:
                print(f"[DEBUG] JotFormHelper.get_products - Returning stale cache due to error")
                return self.products_cache[form_id]
            raise
        except Exception as e:
            log_error("JotFormHelper.get_products - Error fetching products", e, {"form_id": form_id})
            import traceback
            traceback.print_exc()
            if form_id in self.products_cache:
                print(f"[DEBUG] JotFormHelper.get_products - Returning stale cache due to error")
                return self.products_cache[form_id]
            return []
        
    def clean_products(self, products):
        clean_products_list = []
        for product in products:
            description = product.get('description', 'N/A')
            product_data = {
                'name': product.get('name', 'N/A'),
                'price': product.get('price', 'N/A'),
                'description': description
            }

            # Try to extract MOQ from explicit field first
            if 'moq' in product and product.get('moq'):
                product_data['moq'] = product.get('moq')
            else:
                # Try to extract MOQ from description text
                extracted_moq = extract_moq_from_description(description)
                if extracted_moq:
                    product_data['moq'] = extracted_moq

            # Extract other potentially useful fields
            if 'quantity' in product:
                product_data['quantity'] = product.get('quantity')
            if 'stock' in product:
                product_data['stock'] = product.get('stock')

            clean_products_list.append(product_data)
        return clean_products_list
        
    def print_products(self, form_id):
        products = self.get_products(form_id)

        print(f"\n{'='*60}")
        print(f"FOUND {len(products)} PRODUCTS")
        print(f"{'='*60}\n")

        for product in products:
            print(f"Product: {product.get('name', 'N/A')}")
            print(f"Price: ${product.get('price', 'N/A')}")
            print(f"Description: {product.get('description', 'N/A')[:100]}...")
            print("-" * 60)

    def _parse_payment_items(self, answer, pretty_format):
        items = []

        if isinstance(answer, list):
            for item in answer:
                if isinstance(item, dict):
                    name = item.get('name') or item.get('text') or item.get('product')
                    quantity = item.get('quantity') or item.get('qty') or item.get('count')
                    price = item.get('price') or item.get('amount') or item.get('total')
                    if name:
                        items.append({'name': name, 'quantity': quantity or '', 'price': price or ''})
                elif item:
                    items.append({'name': str(item), 'quantity': '', 'price': ''})
        elif isinstance(answer, dict):
            for key in ['paymentItems', 'products', 'items']:
                if key in answer and isinstance(answer[key], list):
                    for item in answer[key]:
                        if not isinstance(item, dict):
                            continue
                        name = item.get('name') or item.get('text') or item.get('product')
                        quantity = item.get('quantity') or item.get('qty') or item.get('count')
                        price = item.get('price') or item.get('amount') or item.get('total')
                        if name:
                            items.append({'name': name, 'quantity': quantity or '', 'price': price or ''})

        if pretty_format:
            lines = [line.strip() for line in str(pretty_format).splitlines() if line.strip()]
            for line in lines:
                match = re.match(
                    r'^(?P<name>.+?)(?:\s*\(x(?P<qty>\d+)\))?(?:\s*x(?P<qty_alt>\d+))?(?:\s*-\s*\$?(?P<price>[\d.,]+))?$',
                    line
                )
                if match:
                    name = match.group('name').strip()
                    quantity = match.group('qty') or match.group('qty_alt') or ''
                    price = match.group('price') or ''
                    if name:
                        items.append({'name': name, 'quantity': quantity, 'price': price})

        return items

    def _add_products_from_items(self, submission_data, items):
        for item in items:
            name = item.get('name')
            if not name:
                continue
            quantity = item.get('quantity', '')
            price = item.get('price', '')
            signature = (name.strip().lower(), str(quantity).strip(), str(price).strip())
            existing = {
                (p.get('name', '').strip().lower(),
                 str(p.get('quantity', '')).strip(),
                 str(p.get('price', '')).strip())
                for p in submission_data['products']
            }
            if signature not in existing:
                submission_data['products'].append({
                    'name': name,
                    'quantity': quantity,
                    'price': price
                })

    def search_submission_by_invoice(self, invoice_id):
        """
        Search for a submission across all forms by Invoice ID.
        Returns submission details if found, None otherwise.

        Args:
            invoice_id: The invoice number/ID to search for

        Returns:
            dict with submission info or None if not found
        """
        print(f"[DEBUG] search_submission_by_invoice - Searching for invoice: {invoice_id}")

        # Normalize the invoice ID (remove spaces, make uppercase for comparison)
        invoice_normalized = str(invoice_id).strip().upper()

        try:
            forms = self.get_all_forms()

            for form_id, form_data in forms.items():
                try:
                    # Get submissions for this form (limit to recent ones for performance)
                    submissions = self._call_with_retry(
                        f"get_form_submissions:{form_id}",
                        lambda fid=form_id: self.client.get_form_submissions(fid, limit=500)
                    )

                    if not submissions:
                        continue

                    for submission in submissions:
                        answers = submission.get('answers', {})

                        # Search through all answer fields for invoice-related fields
                        for field_id, field_data in answers.items():
                            field_name = field_data.get('name', '').lower()
                            field_text = field_data.get('text', '').lower()
                            answer = str(field_data.get('answer', '')).strip().upper()

                            # Check if this is an invoice field
                            is_invoice_field = any(keyword in field_name or keyword in field_text
                                                   for keyword in ['invoice', 'order number', 'order id',
                                                                   'reference', 'confirmation'])

                            if is_invoice_field and answer == invoice_normalized:
                                print(f"[DEBUG] search_submission_by_invoice - Found match in form {form_id}")

                                # Extract useful information from the submission
                                result = {
                                    'found': True,
                                    'form_id': form_id,
                                    'form_title': form_data.get('title', 'Unknown Form'),
                                    'submission_id': submission.get('id'),
                                    'created_at': submission.get('created_at'),
                                    'status': submission.get('status', 'ACTIVE'),
                                    'invoice_id': invoice_id
                                }

                                # Try to extract customer name/email if available
                                for fid, fdata in answers.items():
                                    fname = fdata.get('name', '').lower()
                                    if 'name' in fname and 'first' in fname:
                                        result['customer_name'] = fdata.get('answer', '')
                                    elif 'email' in fname:
                                        result['email'] = fdata.get('answer', '')

                                return result

                except Exception as e:
                    print(f"[DEBUG] search_submission_by_invoice - Error searching form {form_id}: {e}")
                    continue

            print(f"[DEBUG] search_submission_by_invoice - No match found for invoice: {invoice_id}")
            return None

        except Exception as e:
            print(f"[ERROR] search_submission_by_invoice - Error: {e}")
            return None

    def search_submission_in_form(self, form_id, search_value, form_title=None):
        """
        Search for a submission in a specific form by invoice ID, name, or Telegram username.
        Returns full submission details including products ordered.

        Args:
            form_id: The JotForm form ID to search
            search_value: The value to search for (invoice, name, or TG username)
            form_title: Optional form title (for response)

        Returns:
            dict with full submission info or None if not found
        """
        print(f"[DEBUG] search_submission_in_form - Searching form {form_id} for: {search_value}")

        search_normalized = str(search_value).strip().lower()
        search_parts = search_normalized.split()  # Split for partial matching (e.g., "Emily March" -> ["emily", "march"])

        try:
            # Get submissions for this form
            submissions = self._call_with_retry(
                f"get_form_submissions:{form_id}",
                lambda: self.client.get_form_submissions(form_id, limit=500)
            )

            if not submissions:
                print(f"[DEBUG] search_submission_in_form - No submissions found in form {form_id}")
                return None

            print(f"[DEBUG] search_submission_in_form - Found {len(submissions)} submissions to search")

            for submission in submissions:
                answers = submission.get('answers', {})
                match_found = False
                all_text_values = []  # Collect all text for broad search

                # Collect all data from this submission
                submission_data = {
                    'found': False,
                    'form_id': form_id,
                    'form_title': form_title or 'Group Buy',
                    'submission_id': submission.get('id'),
                    'created_at': submission.get('created_at'),
                    'status': submission.get('status', 'ACTIVE'),
                    'invoice_id': None,
                    'customer_name': None,
                    'telegram_username': None,
                    'email': None,
                    'products': [],
                    'raw_answers': {}
                }

                # First pass: extract all relevant fields
                for field_id, field_data in answers.items():
                    field_name = field_data.get('name', '').lower()
                    field_text = field_data.get('text', '').lower()
                    field_type = field_data.get('type', '').lower()
                    answer = field_data.get('answer', '')
                    pretty_format = field_data.get('prettyFormat', '')

                    # Handle different answer formats
                    answer_str = ''
                    if isinstance(answer, dict):
                        # JotForm name fields often return {first: "...", last: "..."}
                        if 'first' in answer or 'last' in answer:
                            first = answer.get('first', '')
                            last = answer.get('last', '')
                            answer_str = f"{first} {last}".strip()
                        else:
                            answer_str = pretty_format or str(answer)
                    elif isinstance(answer, list):
                        # Handle list answers (products, checkboxes, etc.)
                        answer_str = ', '.join(str(x) for x in answer if x)
                    else:
                        answer_str = str(answer).strip() if answer else ''

                    # Use prettyFormat if available and answer_str is empty
                    if not answer_str and pretty_format:
                        answer_str = str(pretty_format).strip()

                    answer_lower = answer_str.lower()

                    # Store raw answer for reference
                    submission_data['raw_answers'][field_name or field_text] = answer_str

                    # Collect all text for broad search
                    if answer_str:
                        all_text_values.append(answer_lower)

                    # Check for invoice field
                    if any(kw in field_name or kw in field_text for kw in ['invoice', 'order number', 'order id', 'reference', 'confirmation', 'transaction']):
                        submission_data['invoice_id'] = answer_str
                        if search_normalized in answer_lower or answer_lower in search_normalized:
                            print(f"[DEBUG] search_submission_in_form - Invoice match: {answer_str}")
                            match_found = True

                    # Check for name fields (including JotForm's control_fullname type)
                    if field_type == 'control_fullname' or any(kw in field_name or kw in field_text for kw in ['name', 'full name', 'your name']):
                        # Handle JotForm's name field structure
                        if isinstance(answer, dict):
                            first = answer.get('first', '')
                            last = answer.get('last', '')
                            full_name = f"{first} {last}".strip()
                            submission_data['customer_name'] = full_name
                            # Check if search matches first, last, or full name
                            name_lower = full_name.lower()
                            if (search_normalized in name_lower or
                                name_lower in search_normalized or
                                all(part in name_lower for part in search_parts)):
                                print(f"[DEBUG] search_submission_in_form - Name match: {full_name}")
                                match_found = True
                        else:
                            if submission_data['customer_name']:
                                submission_data['customer_name'] += ' ' + answer_str
                            else:
                                submission_data['customer_name'] = answer_str
                            if search_normalized in answer_lower or all(part in answer_lower for part in search_parts):
                                print(f"[DEBUG] search_submission_in_form - Name match: {answer_str}")
                                match_found = True

                    # Check for Telegram username
                    if any(kw in field_name or kw in field_text for kw in ['telegram', 'tg', 'tg username', 'telegram username', 'tg handle', 'telegram handle']):
                        # Clean up @ symbol if present
                        tg_username = answer_str.lstrip('@')
                        submission_data['telegram_username'] = tg_username
                        search_tg = search_normalized.lstrip('@')
                        if search_tg in tg_username.lower() or tg_username.lower() in search_tg:
                            print(f"[DEBUG] search_submission_in_form - TG username match: {tg_username}")
                            match_found = True

                    # Check for email
                    if 'email' in field_name or 'email' in field_text or field_type == 'control_email':
                        submission_data['email'] = answer_str
                        if search_normalized == answer_lower or search_normalized in answer_lower:
                            print(f"[DEBUG] search_submission_in_form - Email match: {answer_str}")
                            match_found = True

                    # Check for products (payment field or product list)
                    if field_type == 'control_payment' or any(kw in field_name or kw in field_text for kw in ['product', 'item', 'purchase']):
                        items = self._parse_payment_items(answer, pretty_format or answer_str)
                        self._add_products_from_items(submission_data, items)
                        if not items and answer_str and answer_str != 'N/A':
                            self._add_products_from_items(
                                submission_data,
                                [{'name': answer_str, 'quantity': '', 'price': ''}]
                            )

                # If no specific field match, do a broad search across all values
                if not match_found:
                    combined_text = ' '.join(all_text_values)
                    if search_normalized in combined_text or all(part in combined_text for part in search_parts):
                        print(f"[DEBUG] search_submission_in_form - Broad match found in submission {submission.get('id')}")
                        match_found = True

                if match_found:
                    submission_data['found'] = True
                    print(f"[DEBUG] search_submission_in_form - Match found! Invoice: {submission_data['invoice_id']}, Name: {submission_data['customer_name']}")
                    return submission_data

            print(f"[DEBUG] search_submission_in_form - No match found for: {search_value}")
            return None

        except Exception as e:
            print(f"[ERROR] search_submission_in_form - Error: {e}")
            import traceback
            traceback.print_exc()
            return None


def generate_answer_with_products(user_question, form_title, products, vendor_info=None):
    """
    Uses ChatGPT to generate a natural conversational answer to the user's question
    based on the available products and form metadata.
    """
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    # Format products as a clean list for ChatGPT
    products_text = ""
    for idx, product in enumerate(products, 1):
        name = product.get('name', 'N/A')
        price = product.get('price', 'N/A')
        description = product.get('description', 'N/A')
        products_text += f"{idx}. {name}\n   Price: ${price}\n   Description: {description}\n"

        # Add MOQ and other fields if available
        if 'moq' in product:
            products_text += f"   MOQ (Minimum Order Quantity): {product['moq']}\n"
        if 'quantity' in product:
            products_text += f"   Quantity: {product['quantity']}\n"
        if 'stock' in product:
            products_text += f"   Stock: {product['stock']}\n"

        products_text += "\n"

    # Add vendor information if available
    vendor_text = ""
    if vendor_info:
        if vendor_info.get('vendor'):
            vendor_text += f"\nVendor/Supplier: {vendor_info['vendor']}"
        elif vendor_info.get('suppliers'):
            vendors_list = ', '.join(vendor_info['suppliers'])
            vendor_text += f"\nVendors/Suppliers: {vendors_list}"

    # Add deadline information if available
    deadline_text = ""
    if vendor_info and vendor_info.get('deadline'):
        deadline_text = f"\nDeadline/Closing Date: {vendor_info['deadline']}"

    prompt = f"""You are Bohemia's Steward, a helpful assistant for a Group Buy community.

Form: {form_title}{vendor_text}{deadline_text}

Products:
{products_text}

User asked: "{user_question}"

CRITICAL INSTRUCTIONS:
- ONLY answer the specific question asked - don't volunteer extra information
- If they ask a vague question like "What about X GB?", ask what specifically they want to know
- Be conversational and natural - vary your tone and style
- Don't follow a rigid format or template - be creative with your responses
- Match product abbreviations (Reta=Retatrutide, R30=products with 30, etc.)
- For ambiguous product names, ask for clarification
- For timeline questions: "4-8 weeks depending on customs, production, and shipping. Subject to delays for custom batches, seizures, or international shipping."

MOQ (Minimum Order Quantity) INSTRUCTIONS:
- If user asks about MOQ, minimum order, or minimum quantity for a product:
  1. First check if there's an explicit "MOQ" field listed for that product
  2. If not, search the Description field for MOQ info (look for "MOQ:", "minimum order", "min order", "X units minimum", etc.)
  3. If MOQ is found, state it clearly: "The MOQ for [product] is [amount]"
  4. If no MOQ info exists for the product, say: "I don't see a specific MOQ listed for [product]. Some products have no minimum - check the order form or ask an admin."
- Be specific about which product's MOQ you're answering about

GENERAL:
- The Description field contains critical information including MOQ, lab details, testing info, and vendor specifics - ALWAYS read and use this information
- Keep responses SHORT and direct"""

    print(f"[DEBUG] generate_answer_with_products - Generating answer for: '{user_question}'")
    print(f"[DEBUG] generate_answer_with_products - Using {len(products)} products")

    response = call_openai_with_retry(
        "generate_answer_with_products",
        lambda timeout: client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.95,
            timeout=timeout
        )
    )

    answer = response.choices[0].message.content.strip()
    print(f"[DEBUG] generate_answer_with_products - Generated answer length: {len(answer)} chars")

    return answer


def generate_answer_with_multi_form_products(user_question, forms_data):
    """
    Uses ChatGPT to generate a natural conversational answer to the user's question
    based on products from multiple forms. This handles cases where multiple forms
    (e.g., two January GBs) need to be searched.

    Args:
        user_question: The user's question
        forms_data: List of dicts, each containing:
            - 'form_id': The form ID
            - 'form_title': The form title
            - 'products': List of products from this form
            - 'vendor_info': Optional vendor metadata
    """
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    # Format products grouped by form
    all_products_text = ""
    form_titles = []

    for form_info in forms_data:
        form_title = form_info.get('form_title', 'Unknown Form')
        products = form_info.get('products', [])
        vendor_info = form_info.get('vendor_info', {})

        form_titles.append(form_title)

        if not products:
            continue

        all_products_text += f"\n=== {form_title} ===\n"

        # Add vendor info if available
        if vendor_info:
            if vendor_info.get('vendor'):
                all_products_text += f"Vendor: {vendor_info['vendor']}\n"
            if vendor_info.get('deadline'):
                all_products_text += f"Deadline: {vendor_info['deadline']}\n"

        all_products_text += "\n"

        for idx, product in enumerate(products, 1):
            name = product.get('name', 'N/A')
            price = product.get('price', 'N/A')
            description = product.get('description', 'N/A')
            all_products_text += f"{idx}. {name}\n   Price: ${price}\n   Description: {description}\n"

            # Add MOQ and other fields if available
            if 'moq' in product:
                all_products_text += f"   MOQ (Minimum Order Quantity): {product['moq']}\n"
            if 'quantity' in product:
                all_products_text += f"   Quantity: {product['quantity']}\n"
            if 'stock' in product:
                all_products_text += f"   Stock: {product['stock']}\n"

            all_products_text += "\n"

    forms_list_text = ", ".join(form_titles)

    prompt = f"""You are Bohemia's Steward, a helpful assistant for a Group Buy community.

IMPORTANT: The user's question may apply to MULTIPLE Group Buy forms. I've searched the following forms and found products in each:

Forms searched: {forms_list_text}

{all_products_text}

User asked: "{user_question}"

CRITICAL INSTRUCTIONS:
- Search ALL forms listed above for relevant information
- If the product exists in multiple forms, mention BOTH/ALL occurrences with their respective form names and details
- Clearly indicate which form each piece of information comes from (e.g., "In the January GB #1, the MOQ is X. In January GB #2, the MOQ is Y.")
- If the product only appears in one form, specify which form it's from
- ONLY answer the specific question asked - don't volunteer extra information
- Be conversational and natural - vary your tone and style
- Match product abbreviations (Reta=Retatrutide, R30=products with 30, etc.)

MOQ (Minimum Order Quantity) INSTRUCTIONS:
- If user asks about MOQ, minimum order, or minimum quantity for a product:
  1. Check ALL forms for that product
  2. Report MOQ info from EACH form where the product appears
  3. If MOQ differs between forms, clearly state both
  4. If no MOQ info exists in any form, say: "I don't see a specific MOQ listed for [product] in any of the forms."

GENERAL:
- The Description field contains critical information including MOQ, lab details, testing info, and vendor specifics - ALWAYS read and use this information
- Keep responses SHORT and direct
- Always clarify which form information comes from"""

    print(f"[DEBUG] generate_answer_with_multi_form_products - Generating answer for: '{user_question}'")
    print(f"[DEBUG] generate_answer_with_multi_form_products - Using {len(forms_data)} forms")

    response = call_openai_with_retry(
        "generate_answer_with_multi_form_products",
        lambda timeout: client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.95,
            timeout=timeout
        )
    )

    answer = response.choices[0].message.content.strip()
    print(f"[DEBUG] generate_answer_with_multi_form_products - Generated answer length: {len(answer)} chars")

    return answer


def is_moq_question(message_text):
    """
    Detect if user is asking about MOQ/minimum order for a specific product.
    Returns True if this appears to be a product-specific MOQ question.
    """
    message_lower = message_text.lower()

    # MOQ-related keywords
    moq_keywords = [
        'moq', 'minimum order', 'min order', 'minimum quantity', 'min quantity',
        'minimum purchase', 'min purchase', 'how many do i need', 'how many needed',
        'smallest order', 'minimum amount', 'min amount', 'minimum to order',
        'at least order', 'minimum i can order', 'minimum can order'
    ]

    # Check if message contains MOQ keywords
    has_moq_keyword = any(keyword in message_lower for keyword in moq_keywords)

    if has_moq_keyword:
        print(f"[DEBUG] is_moq_question - MOQ question detected in: '{message_text}'")
        return True

    return False


def check_for_coa_test_question(message_text):
    """
    Detect if user is asking about COA, test results, or certificates of analysis.
    Returns True if this is a COA/test question that should be redirected to admins.
    """
    message_lower = message_text.lower()

    # Keywords that indicate COA/test questions
    coa_keywords = [
        'coa', 'certificate of analysis', 'test result', 'test report',
        'lab test', 'lab result', 'testing', 'purity test', 'quality test',
        'third party test', 'janoshik', 'jano test'
    ]

    # Check if message contains any COA-related keywords
    for keyword in coa_keywords:
        if keyword in message_lower:
            print(f"[DEBUG] check_for_coa_test_question - COA/test question detected: keyword '{keyword}' found")
            return True

    return False

def get_admin_redirect_message():
    """
    Returns the standard message redirecting users to admins for COA/test questions.
    """
    return """I don't have access to external links or vendor test reports. Please DM an admin:
- @Emilycarolinemarch
- @Davesauce

Or post your question in the Telegram group for further support."""

def fuzzy_match_product_name(message_lower, product_name_lower):
    """
    Fuzzy match product names to handle abbreviations and variations.
    Examples: 'Retatrutide 30' matches 'Reta 30', 'R30', 'Rita 30', etc.
    """
    # Extract key parts from product name (first significant word + numbers)
    import re

    # Get all numbers from the product name
    product_numbers = re.findall(r'\d+', product_name_lower)

    # Get first word (usually the main product name)
    product_words = product_name_lower.split()
    if not product_words:
        return 0

    main_word = product_words[0]

    # Score the match
    score = 0

    # Check for exact match
    if product_name_lower in message_lower:
        return 10  # Highest score

    # Check if numbers match (important for dosages like "30", "50", "100")
    numbers_in_message = re.findall(r'\d+', message_lower)
    if product_numbers and all(num in numbers_in_message for num in product_numbers):
        score += 3

    # Check for abbreviation matches
    # For "Retatrutide", match "Reta", "R", "Rita", "Retrograde"
    if len(main_word) >= 4:
        # Check for prefixes of various lengths
        for prefix_len in [1, 2, 3, 4, 5]:
            if prefix_len <= len(main_word):
                prefix = main_word[:prefix_len]
                # Match as whole word or followed by space/number
                if re.search(r'\b' + re.escape(prefix) + r'(?:\s|\d|$)', message_lower):
                    score += min(prefix_len, 3)  # Longer matches get higher scores

    # Check if the main word appears anywhere (fuzzy)
    if main_word in message_lower:
        score += 2

    # Check for common substitutions (l->r, etc.)
    # "Rita" for "Reta"
    variations = [
        main_word.replace('e', 'i'),
        main_word.replace('i', 'e'),
        main_word.replace('o', 'a'),
        main_word.replace('a', 'o'),
    ]
    for var in variations:
        if var in message_lower and len(var) > 3:
            score += 1

    return score

def find_form_by_product_names(message_text, available_forms, return_all_matches=False):
    """
    Search through products in all forms to find which form contains
    products mentioned in the user's message. Uses fuzzy matching for product names.

    Args:
        message_text: The user's message to search for product names
        available_forms: Dictionary of available forms
        return_all_matches: If True, returns all forms with matching products (not just the best)

    Returns:
        If return_all_matches=False: Single form_id string or None
        If return_all_matches=True: List of form_ids with matches, sorted by score (best first)
    """
    print(f"[DEBUG] find_form_by_product_names - Searching for products in message: '{message_text}'")

    message_lower = message_text.lower()
    form_matches = {}  # form_id -> number of product matches

    for form_id, form_data in available_forms.items():
        try:
            # Get products for this form
            products = jotform_helper.get_products(form_id)
            if not products:
                continue

            total_score = 0
            matched_products = []

            # Check if any product names appear in the user's message
            for product in products:
                product_name = product.get('name', '')
                if not product_name or product_name == 'N/A':
                    continue

                product_name_lower = product_name.lower()

                # Use fuzzy matching
                match_score = fuzzy_match_product_name(message_lower, product_name_lower)

                if match_score > 0:
                    total_score += match_score
                    matched_products.append(product_name)
                    print(f"[DEBUG] find_form_by_product_names - Match score {match_score}: '{product_name}' in form {form_id}")

            if total_score > 0:
                form_matches[form_id] = {
                    'score': total_score,
                    'products': matched_products,
                    'title': form_data.get('title')
                }
                print(f"[DEBUG] find_form_by_product_names - Form {form_id} ({form_data.get('title')}) has total score {total_score}")

        except Exception as e:
            print(f"[DEBUG] find_form_by_product_names - Error checking form {form_id}: {e}")
            continue

    if not form_matches:
        print(f"[DEBUG] find_form_by_product_names - No product matches found")
        return [] if return_all_matches else None

    # Sort matches by score (highest first)
    sorted_matches = sorted(form_matches.items(), key=lambda x: x[1]['score'], reverse=True)

    if return_all_matches:
        # Return all forms that have matching products
        form_ids = [form_id for form_id, _ in sorted_matches]
        print(f"[DEBUG] find_form_by_product_names - Returning all {len(form_ids)} matching forms: {form_ids}")
        return form_ids
    else:
        # Return just the best match (original behavior)
        best_match = sorted_matches[0]
        form_id = best_match[0]
        match_info = best_match[1]
        print(f"[DEBUG] find_form_by_product_names - Best match: {form_id} ({match_info['title']}) with products: {match_info['products']}")
        return form_id

def find_forms_by_month(month_name, available_forms):
    """
    Find all forms that match a specific month name.
    Returns a list of form_ids that have the month in their title.
    """
    month_lower = month_name.lower()
    matching_forms = []

    for form_id, form_data in available_forms.items():
        title = form_data.get('title', '').lower()
        if month_lower in title:
            matching_forms.append(form_id)

    return matching_forms


def detect_month_in_message(message_text):
    """
    Detect if the user's message mentions a specific month.
    Returns the month name if found, None otherwise.
    """
    months = [
        'january', 'february', 'march', 'april', 'may', 'june',
        'july', 'august', 'september', 'october', 'november', 'december',
        'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'
    ]

    message_lower = message_text.lower()

    for month in months:
        # Check for month as a word boundary (not part of another word)
        if re.search(rf'\b{month}\b', message_lower):
            # Return the full month name
            month_mapping = {
                'jan': 'january', 'feb': 'february', 'mar': 'march',
                'apr': 'april', 'jun': 'june', 'jul': 'july',
                'aug': 'august', 'sep': 'september', 'oct': 'october',
                'nov': 'november', 'dec': 'december'
            }
            return month_mapping.get(month, month)

    return None


def is_form_specific_query(message_text):
    """
    Check if the user is explicitly asking about a specific form/GB.
    Returns True if the message mentions forms, GBs, months, or form-specific keywords.
    """
    message_lower = message_text.lower()

    # Form-specific keywords
    form_keywords = [
        'form', 'gb', 'g&b', 'group buy', 'groupbuy', 'order form',
        'current', 'latest', 'newest', 'recent', 'this month',
        'halloween', 'fireworks', 'holiday', 'expo', 'november', 'october',
        'january', 'february', 'march', 'april', 'may', 'june',
        'july', 'august', 'september', 'december'
    ]

    return any(keyword in message_lower for keyword in form_keywords)


def analyze_message_for_gb(message_text, available_forms):
    """
    Analyze user message to determine which form(s) they're asking about.

    IMPORTANT: If the user appears to be asking about a PRODUCT (not a specific form),
    we search for the product across all forms FIRST. ChatGPT form selection is only
    used when the user explicitly mentions a form/GB/month.

    Returns:
        - A single form_id string if one form is clearly identified
        - A list of form_ids if multiple forms match (e.g., two January GBs)
        - None if no form could be identified
    """
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    # PRIORITY 1: If this looks like a product query (not form-specific),
    # search for the product across all forms FIRST
    if not is_form_specific_query(message_text):
        print(f"[DEBUG] analyze_message_for_gb - Message appears to be a product query, trying product search first")
        product_matches = find_form_by_product_names(message_text, available_forms, return_all_matches=True)

        if product_matches:
            print(f"[DEBUG] analyze_message_for_gb - Product search found matches: {product_matches}")
            return product_matches
        else:
            print(f"[DEBUG] analyze_message_for_gb - No product matches, will try ChatGPT form identification")

    # PRIORITY 2: Check if user mentions a specific month
    mentioned_month = detect_month_in_message(message_text)
    if mentioned_month:
        # Find all forms matching this month
        matching_month_forms = find_forms_by_month(mentioned_month, available_forms)
        print(f"[DEBUG] analyze_message_for_gb - User mentioned '{mentioned_month}', found {len(matching_month_forms)} matching forms")

        if len(matching_month_forms) > 1:
            # Multiple forms for this month - we'll need to check all of them
            print(f"[DEBUG] analyze_message_for_gb - Multiple forms for {mentioned_month}: {matching_month_forms}")
            return matching_month_forms
        elif len(matching_month_forms) == 1:
            return matching_month_forms[0]

    # PRIORITY 3: Use ChatGPT to identify the form (only for form-specific queries)
    sorted_forms = sorted(
        available_forms.items(),
        key=lambda x: x[1].get('latest_submission', x[1].get('created', '')),
        reverse=True
    )

    forms_list = "\n".join([
        f"- {form_data['title']} (ID: {form_id}, Latest Activity: {form_data.get('latest_submission', 'Unknown')})"
        for form_id, form_data in sorted_forms
    ])

    prompt = f"""You are helping identify which Group Buy (GB) form a user is asking about.

Available forms (sorted by most recent submission activity - FIRST = most active/current):
{forms_list}

User message: "{message_text}"

Analyze the user's message and determine which form they're asking about:
1. If they mention a specific month name (January, February, November, December, etc.), look for that month in the form title
2. CRITICAL: If they ask about "current", "latest", "newest", or "most recent" GB, choose the FIRST form in the list (it has the most recent submission activity)
3. If they mention a date, match it to the closest form by Latest Activity timestamp
4. If they mention a vendor name, try to match it to a form title
5. CRITICAL: If the user is asking about a PRODUCT (like "R30", "Retatrutide", "Tirz", etc.) and NOT mentioning a specific form, respond with "UNCLEAR" - the product should be searched across forms
6. If the message is completely unclear or ambiguous, respond with "UNCLEAR"

NOTE: Forms are sorted by latest submission date, NOT creation date. The first form is the most currently active GB.

IMPORTANT: Respond with ONLY the form ID number (e.g., "253411113426040") or the word "UNCLEAR".
Do not include any other text, explanation, or formatting."""

    print(f"\n[DEBUG] User message: {message_text}")
    print(f"[DEBUG] Available forms: {len(available_forms)}")
    print(f"[DEBUG] Forms list sent to ChatGPT:\n{forms_list}\n")

    response = call_openai_with_retry(
        "analyze_message_for_gb",
        lambda timeout: client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            timeout=timeout
        )
    )

    result = response.choices[0].message.content.strip()
    print(f"[DEBUG] ChatGPT raw response: '{result}'")

    # Check if the result is a valid form ID
    if result != "UNCLEAR" and result in available_forms:
        print(f"[DEBUG] ✓ Form ID '{result}' found in available forms")
        return result
    elif result != "UNCLEAR":
        print(f"[DEBUG] ✗ Form ID '{result}' NOT found in available forms")
        print(f"[DEBUG] Available form IDs: {list(available_forms.keys())}")
        # Try product-based search as fallback - return all matching forms
        print(f"[DEBUG] Trying product-based search as fallback (returning all matches)...")
        return find_form_by_product_names(message_text, available_forms, return_all_matches=True)
    else:
        print(f"[DEBUG] ChatGPT returned UNCLEAR, trying product-based search as fallback...")
        # Try to find form by searching for product names in the message - return all matching forms
        return find_form_by_product_names(message_text, available_forms, return_all_matches=True)

# Initialize global JotFormHelper instance
jotform_helper = JotFormHelper()

# =============================================================================
# BOT COMMAND HANDLERS
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message for new users."""
    user = update.effective_user
    await track_event(EVENT_COMMAND, user, {'command': 'start'})

    await update.message.reply_text(
        "Hello! I'm Bohemia's Steward, your Group Buy assistant.\n\n"
        "I can help you with:\n"
        "- Product information from current GBs\n"
        "- Common questions (how to order, shipping, etc.)\n"
        "- Finding the right Group Buy form\n\n"
        "Just ask me a question, or use /help to see available commands!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available commands and how to use the bot."""
    user = update.effective_user
    await track_event(EVENT_COMMAND, user, {'command': 'help'})
    user_is_admin = await is_admin(user.id)

    # Base help message for all users
    help_text = (
        "Available Commands:\n\n"
        "General:\n"
        "/start - Welcome message\n"
        "/help - Show this message\n"
        "/faq - Show frequently asked questions\n\n"
        "Group Buy Info:\n"
        "/currentgb - Show current GB details\n"
        "/products - List products in current GB\n"
        "/products <search> - Search products\n"
        "/deadline - Show current GB deadline\n"
        "/vendors - Show current GB vendors\n"
        "/status - Show current GB status\n"
        "/jotform - Get link to order form\n"
        "/listforms - List available order forms\n\n"
        "Order Support:\n"
        "/getorderstatus - Look up your order status\n"
        "/reportproblem - Report an issue with your order\n\n"
    )

    # Add admin commands only for admins
    if user_is_admin:
        help_text += (
            "Admin Commands:\n"
            "/setcurrentgb <id or name> - Set current GB\n"
            "/clearcurrentgb - Clear GB setting\n"
            "/setdeadline <date> - Set deadline\n"
            "/cleardeadline - Clear deadline\n"
            "/setvendors <names> - Set vendors\n"
            "/clearvendors - Clear vendors\n"
            "/setstatus <text> - Set status\n"
            "/clearstatus - Clear status\n"
            "/refresh - Refresh cached data\n"
            "/addadmin - Add a bot admin\n"
            "/removeadmin <id> - Remove an admin\n"
            "/listadmins - List all admins\n"
            "/listallforms - List all JotForm forms\n"
            "/addformtolist <id> - Add form to public list\n"
            "/removeformfromlist <id> - Remove form from list\n\n"
        )

    help_text += (
        "Reminders:\n"
        "/subscribe - Subscribe to deadline reminders\n"
        "/unsubscribe - Unsubscribe from reminders\n\n"
        "Or just ask me questions like:\n"
        "- 'What's the price of Retatrutide?'\n"
        "- 'How do I place an order?'"
    )

    # Add analytics command for admins
    if user_is_admin:
        help_text = help_text.replace(
            "Reminders:",
            "Analytics:\n"
            "/analytics - View bot usage statistics\n"
            "/broadcast <msg> - Send message to all subscribers\n\n"
            "Reminders:"
        )

    await update.message.reply_text(help_text)

async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of FAQ topics."""
    faq_topics = [
        "- What is a Group Buy?",
        "- How do I place an order?",
        "- How do I pay?",
        "- How long does shipping take?",
        "- What if my package is seized?",
        "- What's the refund policy?",
        "- What's the minimum order?",
        "- How do I contact an admin?",
        "- What are the group rules?",
        "- When is the next GB?"
    ]
    await update.message.reply_text(
        "Frequently Asked Questions:\n\n" +
        "\n".join(faq_topics) +
        "\n\nJust ask me any of these questions for more details!"
    )

async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to refresh cached data."""
    jotform_helper.clear_all_caches()
    await update.message.reply_text(
        "Cache cleared! Fresh data will be fetched on the next request.\n"
        f"Cache TTL is set to {CACHE_TTL_SECONDS} seconds."
    )


# =============================================================================
# CURRENT GB HELPER FUNCTION
# =============================================================================

async def get_current_gb_form_id():
    """
    Get the current GB form ID.
    First checks if admin has manually set one, otherwise falls back to auto-detection.
    Returns tuple of (form_id, is_manual) where is_manual indicates if it was set by admin.
    """
    # Check if there's a manually set current GB
    manual_gb = await get_current_gb()
    if manual_gb:
        print(f"[DEBUG] get_current_gb_form_id - Using manually set GB: {manual_gb}")
        return manual_gb, True

    # Fall back to auto-detection (most recent submission activity)
    forms = jotform_helper.get_all_forms()
    if not forms:
        return None, False

    # Sort by latest submission date
    sorted_forms = sorted(
        forms.items(),
        key=lambda x: x[1].get('latest_submission', x[1].get('created', '')),
        reverse=True
    )

    if sorted_forms:
        form_id = sorted_forms[0][0]
        print(f"[DEBUG] get_current_gb_form_id - Auto-detected current GB: {form_id}")
        return form_id, False

    return None, False


# =============================================================================
# PHASE 2 COMMANDS
# =============================================================================

async def listforms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List forms from the curated list (open GBs managed by admins)."""
    try:
        # Get the curated forms list from database
        forms_list = await get_forms_list()

        if not forms_list:
            await update.message.reply_text(
                "No forms are currently available.\n"
                "Please check back later or ask an admin."
            )
            return

        # Get current GB to mark it
        current_gb_id, is_manual = await get_current_gb_form_id()

        lines = ["Available Order Forms:\n"]
        for idx, form in enumerate(forms_list, 1):
            form_id = form['form_id']
            title = form['form_title']
            marker = " [CURRENT]" if form_id == current_gb_id else ""
            jotform_url = f"https://form.jotform.com/{form_id}"
            lines.append(f"{idx}. {title}{marker}\n   {jotform_url}")

        lines.append("\nClick a link to place your order!")
        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        print(f"[ERROR] listforms_command: {e}")
        await update.message.reply_text("Error retrieving forms. Please try again.")


async def setcurrentgb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the current Group Buy form."""
    user = update.effective_user

    # Check if user provided an argument
    if not context.args:
        await update.message.reply_text(
            "Usage: /setcurrentgb <form_id or search term>\n\n"
            "Examples:\n"
            "/setcurrentgb 253411113426040\n"
            "/setcurrentgb December\n"
            "/setcurrentgb QSC\n\n"
            "Use /listforms to see available forms and their IDs."
        )
        return

    search_term = " ".join(context.args)
    forms = jotform_helper.get_all_forms()

    # Try to find the form
    found_form_id = None
    found_form_title = None

    # First, check if it's an exact form ID
    if search_term in forms:
        found_form_id = search_term
        found_form_title = forms[search_term].get('title', 'Unknown')
    else:
        # Search by title (case-insensitive)
        search_lower = search_term.lower()
        for form_id, form_data in forms.items():
            title = form_data.get('title', '').lower()
            if search_lower in title:
                found_form_id = form_id
                found_form_title = form_data.get('title', 'Unknown')
                break

    if found_form_id:
        # Save to database
        await set_current_gb(
            found_form_id,
            user_id=user.id,
            username=user.username or user.first_name
        )
        await update.message.reply_text(
            f"Current GB set to:\n"
            f"{found_form_title}\n"
            f"(ID: {found_form_id})\n\n"
            f"All /products, /currentgb, and /deadline commands will now use this form."
        )
    else:
        await update.message.reply_text(
            f"Could not find a form matching '{search_term}'.\n\n"
            "Use /listforms to see available forms."
        )


async def clearcurrentgb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to clear the manual current GB setting."""
    await clear_current_gb()
    await update.message.reply_text(
        "Current GB setting cleared.\n"
        "The bot will now auto-detect the current GB based on latest submission activity."
    )


async def currentgb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show information about the current Group Buy."""
    try:
        form_id, _ = await get_current_gb_form_id()

        if not form_id:
            await update.message.reply_text(
                "No current GB found. Use /listforms to see available forms."
            )
            return

        # Get form info
        forms = jotform_helper.get_all_forms()
        form_data = forms.get(form_id, {})
        form_title = form_data.get('title', 'Unknown')

        # Get manually set deadline and vendors from database
        deadline = await get_deadline() or "Not set"
        vendors = await get_vendors() or "Not set"

        # Get product count
        products = jotform_helper.get_products(form_id)
        product_count = len(products) if products else 0

        response = (
            f"📋 {form_title}\n\n"
            f"🏭 Vendor(s): {vendors}\n"
            f"⏰ Deadline: {deadline}\n"
            f"📦 Products: {product_count} items\n\n"
            f"Use /products to see the full product list."
        )

        await update.message.reply_text(response)

    except Exception as e:
        print(f"[ERROR] currentgb_command: {e}")
        await update.message.reply_text("Error retrieving current GB info. Please try again.")


async def products_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all products in the current Group Buy."""
    try:
        # Check if user provided a search filter
        search_filter = " ".join(context.args).lower() if context.args else None

        form_id, _ = await get_current_gb_form_id()

        if not form_id:
            await update.message.reply_text(
                "No current GB set. Use /setcurrentgb to set one, or /listforms to see available forms."
            )
            return

        # Get products
        products = jotform_helper.get_products(form_id)

        if not products:
            await update.message.reply_text("No products found for the current GB.")
            return

        # Filter products if search term provided
        if search_filter:
            filtered_products = [
                p for p in products
                if search_filter in p.get('name', '').lower()
            ]
            if not filtered_products:
                await update.message.reply_text(
                    f"No products matching '{search_filter}' found.\n"
                    f"Use /products without arguments to see all {len(products)} products."
                )
                return
            products = filtered_products

        # Format product list
        lines = ["Current G&B Product List:\n"]

        for idx, product in enumerate(products, 1):
            name = product.get('name', 'N/A')
            price = product.get('price', 'N/A')
            lines.append(f"{idx}. {name} - ${price}")

            # Stop if message gets too long (Telegram limit ~4096 chars)
            if len("\n".join(lines)) > 3200:
                lines.append(f"\n... and {len(products) - idx} more products.")
                lines.append("Use /products <search> to filter (e.g., /products reta)")
                break

        if search_filter:
            lines.append(f"\nShowing {len(products)} products matching '{search_filter}'")

        # Add helpful footer
        lines.append("\nUse /jotform to place an order, or ask me about specific products for details on MOQ, testing, and more!")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        print(f"[ERROR] products_command: {e}")
        await update.message.reply_text("Error retrieving products. Please try again.")


async def deadline_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the deadline for the current Group Buy."""
    try:
        form_id, _ = await get_current_gb_form_id()

        if not form_id:
            await update.message.reply_text(
                "No current GB found. Use /setcurrentgb to set one."
            )
            return

        # Check database for manually set deadline
        db_deadline = await get_deadline()

        if db_deadline:
            # Just show the raw deadline text, no metadata
            await update.message.reply_text(db_deadline)
        else:
            await update.message.reply_text(
                "No deadline set.\n\n"
                "An admin can set it with /setdeadline"
            )

    except Exception as e:
        print(f"[ERROR] deadline_command: {e}")
        await update.message.reply_text("Error retrieving deadline. Please try again.")


async def setdeadline_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the deadline for the current GB."""
    user = update.effective_user

    # Get full message text to preserve formatting
    raw_text = update.message.text
    command_end = raw_text.find(' ')

    if command_end == -1 or not raw_text[command_end:].strip():
        await update.message.reply_text(
            "Usage: /setdeadline <deadline text>\n\n"
            "Examples:\n"
            "/setdeadline January 15, 2025\n"
            "/setdeadline Friday at midnight EST\n"
            "/setdeadline 01/15/25 11:59 PM"
        )
        return

    deadline_text = raw_text[command_end + 1:]

    await set_deadline(
        deadline_text,
        user_id=user.id,
        username=user.username or user.first_name
    )

    await update.message.reply_text(
        f"Deadline set to:\n{deadline_text}\n\n"
        "Users can now see this with /deadline"
    )


async def cleardeadline_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to clear the manually set deadline."""
    await clear_deadline()
    await update.message.reply_text(
        "Deadline cleared.\n"
        "The bot will now try to detect it from the JotForm (if available)."
    )


# =============================================================================
# VENDORS COMMANDS
# =============================================================================

async def vendors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the vendors for the current Group Buy."""
    try:
        form_id, _ = await get_current_gb_form_id()

        if not form_id:
            await update.message.reply_text(
                "No current GB found. Use /setcurrentgb to set one."
            )
            return

        # Get form info
        forms = jotform_helper.get_all_forms()
        form_title = forms.get(form_id, {}).get('title', 'the current GB')

        # Check database for manually set vendors
        db_vendors = await get_vendors()

        if db_vendors:
            await update.message.reply_text(
                f"The current vendor(s) for {form_title} is {db_vendors}.\n\n"
                "For more information on Vendor COAs, third-party test reports, "
                "questions, or concerns, please message an admin."
            )
        else:
            await update.message.reply_text(
                f"No vendors have been set for {form_title} yet.\n\n"
                "An admin can set them with /setvendors"
            )

    except Exception as e:
        print(f"[ERROR] vendors_command: {e}")
        await update.message.reply_text("Error retrieving vendors. Please try again.")


async def setvendors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the vendors for the current GB."""
    user = update.effective_user

    # Get full message text to preserve formatting
    raw_text = update.message.text
    command_end = raw_text.find(' ')

    if command_end == -1 or not raw_text[command_end:].strip():
        await update.message.reply_text(
            "Usage: /setvendors <vendor names>\n\n"
            "Examples:\n"
            "/setvendors QSC\n"
            "/setvendors QSC, Amo, Tracy\n"
            "/setvendors Multiple vendors - see product list"
        )
        return

    vendors_text = raw_text[command_end + 1:]

    await set_vendors(
        vendors_text,
        user_id=user.id,
        username=user.username or user.first_name
    )

    await update.message.reply_text(
        f"Vendors set to:\n{vendors_text}\n\n"
        "Users can now see this with /vendors"
    )


async def clearvendors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to clear the manually set vendors."""
    await clear_vendors()
    await update.message.reply_text(
        "Vendors cleared.\n"
        "The bot will now try to detect them from the JotForm (if available)."
    )


# =============================================================================
# STATUS COMMANDS
# =============================================================================

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the current status of the Group Buy."""
    try:
        form_id, _ = await get_current_gb_form_id()

        if not form_id:
            await update.message.reply_text(
                "No current GB found. Use /setcurrentgb to set one."
            )
            return

        # Check database for status
        db_status = await get_status()

        if db_status:
            # Just show the raw status text, no metadata
            await update.message.reply_text(db_status)
        else:
            await update.message.reply_text(
                "No status set.\n\n"
                "An admin can set it with /setstatus"
            )

    except Exception as e:
        print(f"[ERROR] status_command: {e}")
        await update.message.reply_text("Error retrieving status. Please try again.")


async def setstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the status for the current GB."""
    user = update.effective_user

    # Get full message text to preserve formatting (newlines, emojis, etc.)
    raw_text = update.message.text
    # Remove the command part, keeping everything after "/setstatus "
    command_end = raw_text.find(' ')

    if command_end == -1 or not raw_text[command_end:].strip():
        await update.message.reply_text(
            "Usage: /setstatus <status text>\n\n"
            "Examples:\n"
            "/setstatus Orders open - deadline Jan 15\n"
            "/setstatus Waiting on shipment from vendor\n"
            "/setstatus Packages shipped - tracking sent via DM\n"
            "/setstatus GB closed - processing orders\n\n"
            "Tip: You can use multiple lines and emojis!"
        )
        return

    status_text = raw_text[command_end + 1:]

    await set_status(
        status_text,
        user_id=user.id,
        username=user.username or user.first_name
    )

    await update.message.reply_text(
        f"Status set to:\n{status_text}\n\n"
        "Users can now see this with /status"
    )


async def clearstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to clear the status."""
    await clear_status()
    await update.message.reply_text("Status cleared.")


# =============================================================================
# JOTFORM LINK COMMAND
# =============================================================================

async def jotform_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the JotForm link for the current Group Buy."""
    try:
        form_id, is_manual = await get_current_gb_form_id()

        if not form_id:
            await update.message.reply_text(
                "No current GB found. Use /setcurrentgb to set one."
            )
            return

        # Get form info
        forms = jotform_helper.get_all_forms()
        form_title = forms.get(form_id, {}).get('title', 'Current GB')

        # JotForm URLs follow this pattern
        jotform_url = f"https://form.jotform.com/{form_id}"

        await update.message.reply_text(
            f"Order Form for {form_title}:\n\n"
            f"{jotform_url}\n\n"
            "Click the link above to place your order!"
        )

    except Exception as e:
        print(f"[ERROR] jotform_command: {e}")
        await update.message.reply_text("Error retrieving form link. Please try again.")


# =============================================================================
# ORDER STATUS LOOKUP COMMAND (Interactive Conversation Handler)
# =============================================================================

async def getorderstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the order status lookup flow with form selection."""
    try:
        # Get forms from the curated list
        forms_list = await get_forms_list()

        if not forms_list:
            # Fallback to all forms if no curated list
            all_forms = jotform_helper.get_all_forms()
            forms_list = [{'form_id': fid, 'form_title': fdata.get('title', 'Unknown')}
                          for fid, fdata in all_forms.items()]

        if not forms_list:
            await update.message.reply_text(
                "No Group Buy forms are currently available.\n"
                f"Please DM @{ADMIN_USERNAME} for assistance."
            )
            return ConversationHandler.END

        # Create inline keyboard with form options
        keyboard = []
        for form in forms_list[:10]:  # Limit to 10 forms for UI
            form_id = form['form_id']
            title = form['form_title']
            # Truncate long titles
            display_title = title[:30] + "..." if len(title) > 30 else title
            keyboard.append([InlineKeyboardButton(display_title, callback_data=f"status_form_{form_id}")])

        # Add cancel button
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="status_cancel")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Let's look up your order status.\n\n"
            "Please select the Group Buy your order is from:",
            reply_markup=reply_markup
        )

        return STATUS_WAITING_FORM

    except Exception as e:
        print(f"[ERROR] getorderstatus_command: {e}")
        await update.message.reply_text(
            "I encountered an error. Please try again later or "
            f"DM @{ADMIN_USERNAME} for assistance."
        )
        return ConversationHandler.END


async def status_form_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle form selection from inline keyboard."""
    query = update.callback_query
    await query.answer()

    if query.data == "status_cancel":
        await query.edit_message_text("Order lookup cancelled.")
        return ConversationHandler.END

    # Extract form ID from callback data
    form_id = query.data.replace("status_form_", "")

    # Get form title
    forms = jotform_helper.get_all_forms()
    form_title = forms.get(form_id, {}).get('title', 'Selected Group Buy')

    # Store in user context
    context.user_data['status_form_id'] = form_id
    context.user_data['status_form_title'] = form_title

    await query.edit_message_text(
        f"Selected: {form_title}\n\n"
        "Now, please enter ONE of the following to find your order:\n\n"
        "• Your Invoice Number (from your confirmation email)\n"
        "• Your name (as entered on the form)\n"
        "• Your Telegram username\n\n"
        "(Type /cancel to cancel)"
    )

    return STATUS_WAITING_IDENTIFIER


async def status_receive_identifier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the identifier and look up the order."""
    search_value = update.message.text.strip()
    form_id = context.user_data.get('status_form_id')
    form_title = context.user_data.get('status_form_title', 'Group Buy')

    if not form_id:
        await update.message.reply_text(
            "Something went wrong. Please start over with /getorderstatus"
        )
        return ConversationHandler.END

    await update.message.reply_text("🔍 Searching for your order... please wait.")

    try:
        # Search for the submission
        result = jotform_helper.search_submission_in_form(form_id, search_value, form_title)

        if result and result.get('found'):
            # Format the order display (plain text to avoid HTML parse errors)
            response = format_order_display(result)
            await update.message.reply_text(response)
        else:
            await update.message.reply_text(
                f"I couldn't find an order matching \"{search_value}\" in {form_title}.\n\n"
                "This could mean:\n"
                "• The information entered doesn't match our records\n"
                "• The order may be under a different name/email\n"
                "• The order may not have been submitted yet\n\n"
                f"If you believe this is an error, please DM @{ADMIN_USERNAME} with your order details."
            )

    except Exception as e:
        print(f"[ERROR] status_receive_identifier: {e}")
        await update.message.reply_text(
            "I encountered an error while looking up your order.\n"
            f"Please try again later or DM @{ADMIN_USERNAME} for assistance."
        )

    # Clear user data
    context.user_data.pop('status_form_id', None)
    context.user_data.pop('status_form_title', None)

    return ConversationHandler.END


async def status_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the status lookup flow."""
    context.user_data.pop('status_form_id', None)
    context.user_data.pop('status_form_title', None)
    await update.message.reply_text(
        "Order lookup cancelled.\n"
        "Use /getorderstatus to start again."
    )
    return ConversationHandler.END


def format_order_display(order_data):
    """
    Format the order data for display to the user.
    Uses plain text formatting to avoid HTML parse errors from JotForm data.

    Args:
        order_data: dict with order information from search_submission_in_form

    Returns:
        Formatted string for Telegram message (plain text)
    """
    form_title = str(order_data.get('form_title', 'Group Buy'))
    telegram_username = str(order_data.get('telegram_username', 'N/A'))
    customer_name = str(order_data.get('customer_name', ''))
    invoice_id = str(order_data.get('invoice_id', 'N/A'))
    products = order_data.get('products', [])

    # Build the display string using plain text (no HTML)
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📋 {form_title}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ""
    ]

    # User info
    if telegram_username and telegram_username != 'N/A':
        lines.append(f"👤 User: @{telegram_username}")
    elif customer_name:
        lines.append(f"👤 User: {customer_name}")

    lines.append(f"🧾 Invoice: {invoice_id}")
    lines.append("")

    # Products list
    if products:
        lines.append("📦 Order Items:")
        for i, product in enumerate(products, 1):
            name = str(product.get('name', 'Unknown Item'))
            # Strip any HTML tags from name
            import re
            name = re.sub(r'<[^>]+>', '', name)

            quantity = str(product.get('quantity', ''))
            price = str(product.get('price', ''))

            if quantity and price:
                lines.append(f"  {i}. {name} (x{quantity}) - ${price}")
            elif quantity:
                lines.append(f"  {i}. {name} (x{quantity})")
            else:
                lines.append(f"  {i}. {name}")
    else:
        lines.append("No product details available")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 ORDER STATUS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"For status updates, check group announcements or DM @{ADMIN_USERNAME}")

    return "\n".join(lines)


# =============================================================================
# REPORT A PROBLEM FEATURE (Conversation Handler)
# =============================================================================

async def reportproblem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the problem reporting flow."""
    await update.message.reply_text(
        "I'm sorry to hear you're having an issue with your order.\n\n"
        "Please enter your Invoice ID so we can locate your order:\n\n"
        "(Type /cancel at any time to cancel this report)"
    )
    return REPORT_WAITING_INVOICE


async def report_receive_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the invoice ID for the problem report."""
    invoice_id = update.message.text.strip()

    # Store the invoice ID in user context
    context.user_data['report_invoice'] = invoice_id

    await update.message.reply_text(
        f"Invoice ID: {invoice_id}\n\n"
        "Please describe the problem you're experiencing:\n"
        "(Be as detailed as possible - what happened, what you expected, etc.)"
    )
    return REPORT_WAITING_DESCRIPTION


async def report_receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the problem description."""
    description = update.message.text.strip()

    # Store the description in user context
    context.user_data['report_description'] = description

    # Create inline keyboard for photo option
    keyboard = [
        [
            InlineKeyboardButton("Yes, attach photo", callback_data="report_photo_yes"),
            InlineKeyboardButton("No, submit now", callback_data="report_photo_no")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Got it. Would you like to attach a photo showing the issue?\n\n"
        "(Photos can help us understand and resolve the problem faster)",
        reply_markup=reply_markup
    )
    return REPORT_WAITING_PHOTO


async def report_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the photo yes/no button callback."""
    query = update.callback_query
    await query.answer()

    if query.data == "report_photo_yes":
        await query.edit_message_text(
            "Please send a photo showing the issue.\n\n"
            "(Just upload the image directly to this chat)"
        )
        return REPORT_WAITING_PHOTO
    else:
        # Submit without photo
        context.user_data['report_photo'] = None
        return await submit_problem_report(update, context, from_callback=True)


async def report_receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the photo for the problem report."""
    if update.message.photo:
        # Get the largest photo (best quality)
        photo = update.message.photo[-1]
        context.user_data['report_photo'] = photo.file_id
        context.user_data['report_photo_file'] = photo
    else:
        context.user_data['report_photo'] = None

    return await submit_problem_report(update, context)


async def submit_problem_report(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    """Submit the problem report and forward to admin."""
    user = update.effective_user
    invoice_id = context.user_data.get('report_invoice', 'Not provided')
    description = context.user_data.get('report_description', 'No description')
    photo_file_id = context.user_data.get('report_photo')

    # Try to look up order details from JotForm
    order_details = ""
    try:
        # Search for the order across all forms
        result = jotform_helper.search_submission_by_invoice(invoice_id)
        if result and result.get('found'):
            order_details = (
                f"\n📦 ORDER DETAILS FOUND:\n"
                f"Form: {result.get('form_title', 'Unknown')}\n"
                f"Customer: {result.get('customer_name', 'N/A')}\n"
                f"TG Username: @{result.get('telegram_username', 'N/A')}\n"
            )
            products = result.get('products', [])
            if products:
                order_details += "Products:\n"
                for p in products[:5]:  # Limit to 5 products
                    import re
                    name = re.sub(r'<[^>]+>', '', str(p.get('name', 'Unknown')))
                    qty = p.get('quantity', '')
                    order_details += f"  - {name}"
                    if qty:
                        order_details += f" (x{qty})"
                    order_details += "\n"
    except Exception as e:
        print(f"[DEBUG] submit_problem_report - Could not look up order: {e}")
        order_details = "\n⚠️ Could not look up order details automatically.\n"

    # Format the report message
    report_message = (
        "🚨 NEW PROBLEM REPORT 🚨\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 FROM: @{user.username or 'No username'} ({user.first_name})\n"
        f"🆔 User ID: {user.id}\n"
        f"🧾 Invoice: {invoice_id}\n"
        f"{order_details}\n"
        f"❗ PROBLEM DESCRIPTION:\n{description}\n\n"
        f"{'📷 Photo attached below' if photo_file_id else '📷 No photo attached'}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    # Send to all admins (ADMIN_CHAT_ID + registered admins)
    admin_notified = False
    sent_count = 0

    # Try ADMIN_CHAT_ID first
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=report_message
            )
            if photo_file_id:
                await context.bot.send_photo(
                    chat_id=int(ADMIN_CHAT_ID),
                    photo=photo_file_id,
                    caption=f"📷 Photo for problem report\nInvoice: {invoice_id}\nFrom: @{user.username or user.first_name}"
                )
            sent_count += 1
            admin_notified = True
            print(f"[DEBUG] submit_problem_report - Sent to ADMIN_CHAT_ID: {ADMIN_CHAT_ID}")
        except Exception as e:
            print(f"[ERROR] submit_problem_report - Failed to send to ADMIN_CHAT_ID: {e}")

    # Also send to all registered admins
    try:
        admins = await get_all_admins()
        for admin in admins:
            admin_id = admin.get('user_id')
            # Skip if same as ADMIN_CHAT_ID (already sent)
            if ADMIN_CHAT_ID and str(admin_id) == str(ADMIN_CHAT_ID):
                continue
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=report_message
                )
                if photo_file_id:
                    await context.bot.send_photo(
                        chat_id=admin_id,
                        photo=photo_file_id,
                        caption=f"📷 Photo for problem report\nInvoice: {invoice_id}\nFrom: @{user.username or user.first_name}"
                    )
                sent_count += 1
                admin_notified = True
                print(f"[DEBUG] submit_problem_report - Sent to admin: {admin_id}")
            except Exception as e:
                print(f"[DEBUG] submit_problem_report - Failed to send to admin {admin_id}: {e}")
    except Exception as e:
        print(f"[ERROR] submit_problem_report - Failed to get admins: {e}")

    # Log if no admins were notified
    if not admin_notified:
        print(f"[WARNING] submit_problem_report - No admins were notified! Check ADMIN_CHAT_ID or add admins with /addadmin")

    # Store the report in the database for record keeping
    try:
        await log_event(
            event_type='problem_report',
            event_data=json.dumps({
                'invoice_id': invoice_id,
                'description': description,
                'has_photo': bool(photo_file_id),
                'admins_notified': sent_count
            }),
            user_id=user.id,
            username=user.username
        )
    except Exception as e:
        print(f"[ERROR] submit_problem_report - Failed to log event: {e}")

    # Send confirmation to user
    confirmation_msg = (
        "✅ Your problem report has been submitted!\n\n"
        f"🧾 Invoice: {invoice_id}\n"
        f"❗ Issue: {description[:100]}{'...' if len(description) > 100 else ''}\n\n"
        f"An admin (@{ADMIN_USERNAME}) will review your report and get back to you.\n"
        "Please be patient - they typically respond within 24-48 hours.\n\n"
        "Thank you for letting us know about this issue!"
    )

    if from_callback:
        await update.callback_query.edit_message_text(confirmation_msg)
    else:
        await update.message.reply_text(confirmation_msg)

    # Clear user data
    context.user_data.clear()

    return ConversationHandler.END


async def report_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the problem report flow."""
    context.user_data.clear()
    await update.message.reply_text(
        "Problem report cancelled.\n\n"
        "If you need help, you can always start a new report with /reportproblem "
        f"or DM @{ADMIN_USERNAME} directly."
    )
    return ConversationHandler.END


# =============================================================================
# BOUNDARY ENFORCEMENT
# =============================================================================

def check_out_of_scope_request(message_text):
    """
    Check if the user's message is asking for something outside the bot's scope.
    Returns a tuple of (is_out_of_scope, response_message).
    """
    message_lower = message_text.lower()

    # Pricing change requests
    pricing_keywords = [
        'lower the price', 'reduce the price', 'cheaper', 'discount',
        'price match', 'bulk discount', 'special price', 'negotiate price',
        'can you lower', 'too expensive', 'lower price', 'better price',
        'deal on', 'cut me a deal'
    ]

    # Exception/special treatment requests
    exception_keywords = [
        'make an exception', 'exception for me', 'special case',
        'just this once', 'bend the rules', 'skip the', 'bypass',
        'can you just', 'please just', 'just for me', 'as a favor'
    ]

    # Admin decision requests
    admin_decision_keywords = [
        'when will admin', 'tell the admin to', 'make them',
        'force them to', 'why won\'t they', 'they should',
        'admin needs to', 'get the admin to'
    ]

    # Negotiation attempts
    negotiation_keywords = [
        'what if i', 'how about', 'counter offer', 'best you can do',
        'meet me halfway', 'split the difference', 'come down on',
        'work with me', 'cut a deal'
    ]

    # Check for pricing requests
    if any(keyword in message_lower for keyword in pricing_keywords):
        return True, (
            "I can't modify pricing or offer discounts - those decisions are made by the admins.\n\n"
            "The prices listed on the order form are final for this Group Buy.\n\n"
            f"If you have questions about pricing, please DM @{ADMIN_USERNAME}."
        )

    # Check for exception requests
    if any(keyword in message_lower for keyword in exception_keywords):
        return True, (
            "I'm not able to make exceptions to the Group Buy rules or processes.\n\n"
            "These policies exist to ensure fairness for all members.\n\n"
            f"If you have a special circumstance, please DM @{ADMIN_USERNAME} to discuss."
        )

    # Check for admin decision interference
    if any(keyword in message_lower for keyword in admin_decision_keywords):
        return True, (
            "I can't influence or speak on behalf of admin decisions.\n\n"
            "Admins make decisions based on what's best for the community and logistics.\n\n"
            f"For questions about admin decisions, please DM @{ADMIN_USERNAME} directly."
        )

    # Check for negotiations
    if any(keyword in message_lower for keyword in negotiation_keywords):
        return True, (
            "I'm not set up to negotiate terms, prices, or conditions.\n\n"
            "The Group Buy operates on fixed terms to keep things fair and simple.\n\n"
            f"If you have concerns, please reach out to @{ADMIN_USERNAME}."
        )

    return False, None


# =============================================================================
# ADMIN MANAGEMENT COMMANDS
# =============================================================================

async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a user as a bot admin. First admin can be added by anyone, subsequent admins require existing admin."""
    user = update.effective_user
    admin_count = await get_admin_count()

    # If there are already admins, check if current user is admin
    if admin_count > 0:
        if not await is_admin(user.id):
            await update.message.reply_text("Only existing admins can add new admins.")
            return

    # Check if replying to a message or provided user ID
    target_user = None
    target_username = None

    if update.message.reply_to_message:
        # Adding the user being replied to
        target_user = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username or \
                         update.message.reply_to_message.from_user.first_name
    elif context.args:
        # Try to parse user ID from args
        try:
            target_user = int(context.args[0])
            target_username = context.args[1] if len(context.args) > 1 else f"User {target_user}"
        except ValueError:
            await update.message.reply_text(
                "Usage: /addadmin <user_id> [username]\n"
                "Or reply to a user's message with /addadmin"
            )
            return
    else:
        # Add self as admin (useful for first admin setup)
        target_user = user.id
        target_username = user.username or user.first_name

    await add_admin(
        target_user,
        target_username,
        added_by_user_id=user.id,
        added_by_username=user.username or user.first_name
    )

    if admin_count == 0:
        await update.message.reply_text(
            f"@{target_username} added as the first admin!\n"
            "You can now use admin commands like /setcurrentgb, /refresh, etc."
        )
    else:
        await update.message.reply_text(f"@{target_username} added as admin.")


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a user from bot admins."""
    user = update.effective_user

    if not await is_admin(user.id):
        await update.message.reply_text("Only admins can remove other admins.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return

    try:
        target_user = int(context.args[0])
        await remove_admin(target_user)
        await update.message.reply_text(f"User {target_user} removed from admins.")
    except ValueError:
        await update.message.reply_text("Please provide a valid user ID.")


async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all bot admins."""
    admins = await get_all_admins()

    if not admins:
        await update.message.reply_text(
            "No admins configured yet.\n"
            "Use /addadmin to add yourself as the first admin."
        )
        return

    lines = ["Bot Admins:\n"]
    for admin in admins:
        username = admin.get('username', 'Unknown')
        user_id = admin.get('user_id')
        lines.append(f"- @{username} ({user_id})")

    await update.message.reply_text("\n".join(lines))


# =============================================================================
# FORMS LIST MANAGEMENT COMMANDS
# =============================================================================

async def addformtolist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to add a form to the curated forms list shown by /listforms."""
    user = update.effective_user

    # Check if user is admin
    if not await is_admin(user.id):
        await update.message.reply_text("Only admins can add forms to the list.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /addformtolist <form_id or search term>\n\n"
            "Examples:\n"
            "/addformtolist 253411113426040\n"
            "/addformtolist December\n"
            "/addformtolist QSC\n\n"
            "This adds a form to the curated list shown by /listforms."
        )
        return

    search_term = " ".join(context.args)
    forms = jotform_helper.get_all_forms()

    # Try to find the form
    found_form_id = None
    found_form_title = None

    # First, check if it's an exact form ID
    if search_term in forms:
        found_form_id = search_term
        found_form_title = forms[search_term].get('title', 'Unknown')
    else:
        # Search by title (case-insensitive)
        search_lower = search_term.lower()
        for form_id, form_data in forms.items():
            title = form_data.get('title', '').lower()
            if search_lower in title:
                found_form_id = form_id
                found_form_title = form_data.get('title', 'Unknown')
                break

    if found_form_id:
        # Check if already in list
        if await is_form_in_list(found_form_id):
            await update.message.reply_text(
                f"'{found_form_title}' is already in the forms list."
            )
            return

        # Add to list
        await add_form_to_list(
            found_form_id,
            found_form_title,
            user_id=user.id,
            username=user.username or user.first_name
        )
        await update.message.reply_text(
            f"Added to forms list:\n"
            f"{found_form_title}\n"
            f"(ID: {found_form_id})\n\n"
            "Users will now see this form when using /listforms."
        )
    else:
        await update.message.reply_text(
            f"Could not find a form matching '{search_term}'.\n\n"
            "Use /listallforms to see all available JotForm forms."
        )


async def removeformfromlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to remove a form from the curated forms list."""
    user = update.effective_user

    # Check if user is admin
    if not await is_admin(user.id):
        await update.message.reply_text("Only admins can remove forms from the list.")
        return

    if not context.args:
        # Show current forms list to help admin choose
        forms_list = await get_forms_list()
        if not forms_list:
            await update.message.reply_text("The forms list is empty. Nothing to remove.")
            return

        lines = ["Current forms in list:\n"]
        for idx, form in enumerate(forms_list, 1):
            lines.append(f"{idx}. {form['form_title']}\n   ID: {form['form_id']}")

        lines.append("\nUsage: /removeformfromlist <form_id or search term>")
        await update.message.reply_text("\n".join(lines))
        return

    search_term = " ".join(context.args)
    forms_list = await get_forms_list()

    # Try to find the form in the list
    found_form = None

    # First, check if it's an exact form ID
    for form in forms_list:
        if form['form_id'] == search_term:
            found_form = form
            break

    # If not found by ID, search by title
    if not found_form:
        search_lower = search_term.lower()
        for form in forms_list:
            if search_lower in form['form_title'].lower():
                found_form = form
                break

    if found_form:
        await remove_form_from_list(found_form['form_id'])
        await update.message.reply_text(
            f"Removed from forms list:\n"
            f"{found_form['form_title']}\n"
            f"(ID: {found_form['form_id']})"
        )
    else:
        await update.message.reply_text(
            f"Could not find a form matching '{search_term}' in the forms list.\n\n"
            "Use /removeformfromlist without arguments to see the current list."
        )


async def listallforms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to list ALL JotForm forms (for adding to the curated list)."""
    user = update.effective_user

    # Check if user is admin
    if not await is_admin(user.id):
        await update.message.reply_text("Only admins can view all forms.")
        return

    try:
        forms = jotform_helper.get_all_forms()
        if not forms:
            await update.message.reply_text("No forms found in JotForm.")
            return

        # Sort by latest submission date
        sorted_forms = sorted(
            forms.items(),
            key=lambda x: x[1].get('latest_submission', x[1].get('created', '')),
            reverse=True
        )

        # Get current forms list to mark which are already added
        forms_list = await get_forms_list()
        forms_in_list = {f['form_id'] for f in forms_list}

        # Get current GB to mark it
        current_gb_id, is_manual = await get_current_gb_form_id()

        lines = ["All JotForm Forms:\n"]
        for idx, (form_id, form_data) in enumerate(sorted_forms, 1):
            title = form_data.get('title', 'Untitled')
            markers = []
            if form_id == current_gb_id:
                markers.append("CURRENT")
            if form_id in forms_in_list:
                markers.append("IN LIST")
            marker_str = f" [{', '.join(markers)}]" if markers else ""
            lines.append(f"{idx}. {title}{marker_str}\n   ID: {form_id}")

        lines.append("\nUse /addformtolist <id> to add a form to the public list.")
        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        print(f"[ERROR] listallforms_command: {e}")
        await update.message.reply_text("Error retrieving forms. Please try again.")


# =============================================================================
# ANALYTICS COMMAND (Admin only)
# =============================================================================

async def analytics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to view bot usage analytics."""
    user = update.effective_user

    # Check if user is admin
    if not await is_admin(user.id):
        await update.message.reply_text("Only admins can view analytics.")
        return

    await track_event(EVENT_ADMIN_ACTION, user, {'action': 'view_analytics'})

    try:
        # Get analytics summary for last 7 days
        days = 7
        if context.args:
            try:
                days = int(context.args[0])
                days = min(max(days, 1), 90)  # Clamp between 1 and 90 days
            except ValueError:
                pass

        summary = await get_analytics_summary(days)

        # Format the response
        lines = [
            f"Bot Analytics (Last {days} days)",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Total Events: {summary['total_events']}",
            f"Unique Users: {summary['unique_users']}",
            f"Reminder Subscribers: {await get_reminder_subscriber_count()}",
            "",
            "Events by Type:",
        ]

        for event_type, count in summary['by_type'].items():
            lines.append(f"  • {event_type}: {count}")

        if summary['daily']:
            lines.append("")
            lines.append("Daily Activity:")
            for date, count in list(summary['daily'].items())[:7]:  # Last 7 days
                lines.append(f"  • {date}: {count} events")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        print(f"[ERROR] analytics_command: {e}")
        await update.message.reply_text("Error retrieving analytics. Please try again.")


# =============================================================================
# REMINDER SUBSCRIPTION COMMANDS
# =============================================================================

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Subscribe to deadline reminders."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    await track_event(EVENT_COMMAND, user, {'command': 'subscribe'})

    try:
        # Check if already subscribed
        if await is_subscribed_to_reminders(user.id):
            await update.message.reply_text(
                "You're already subscribed to deadline reminders.\n"
                "Use /unsubscribe to stop receiving reminders."
            )
            return

        # Subscribe the user
        await subscribe_to_reminders(user.id, chat_id, user.username or user.first_name)

        await update.message.reply_text(
            "You're now subscribed to deadline reminders.\n\n"
            "You'll receive notifications when GB deadlines are approaching.\n"
            "Use /unsubscribe to stop receiving reminders."
        )

    except Exception as e:
        print(f"[ERROR] subscribe_command: {e}")
        await update.message.reply_text("Error subscribing. Please try again.")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unsubscribe from deadline reminders."""
    user = update.effective_user

    await track_event(EVENT_COMMAND, user, {'command': 'unsubscribe'})

    try:
        # Check if subscribed
        if not await is_subscribed_to_reminders(user.id):
            await update.message.reply_text(
                "You're not currently subscribed to reminders.\n"
                "Use /subscribe to start receiving deadline reminders."
            )
            return

        # Unsubscribe the user
        await unsubscribe_from_reminders(user.id)

        await update.message.reply_text(
            "You've been unsubscribed from deadline reminders.\n"
            "You can re-subscribe anytime with /subscribe."
        )

    except Exception as e:
        print(f"[ERROR] unsubscribe_command: {e}")
        await update.message.reply_text("Error unsubscribing. Please try again.")


# =============================================================================
# BROADCAST COMMAND (Admin only)
# =============================================================================

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to broadcast a message to all reminder subscribers."""
    user = update.effective_user

    # Check if user is admin
    if not await is_admin(user.id):
        await update.message.reply_text("Only admins can send broadcast messages.")
        return

    # Get the message to broadcast
    raw_text = update.message.text
    command_end = raw_text.find(' ')

    if command_end == -1 or not raw_text[command_end:].strip():
        subscriber_count = await get_reminder_subscriber_count()
        await update.message.reply_text(
            f"Usage: /broadcast <message>\n\n"
            f"This will send a message to all {subscriber_count} reminder subscribers.\n\n"
            f"Example:\n"
            f"/broadcast Deadline reminder: January GB closes tomorrow at midnight!"
        )
        return

    message = raw_text[command_end + 1:]

    await track_event(EVENT_ADMIN_ACTION, user, {'action': 'broadcast', 'message_length': len(message)})

    try:
        subscribers = await get_all_reminder_subscribers()

        if not subscribers:
            await update.message.reply_text("No subscribers to broadcast to.")
            return

        # Send progress message
        await update.message.reply_text(
            f"Broadcasting to {len(subscribers)} subscribers..."
        )

        # Broadcast the message
        sent_count = 0
        failed_count = 0
        broadcast_text = f"📢 Announcement from Bohemia:\n\n{message}"

        for subscriber in subscribers:
            try:
                await context.bot.send_message(
                    chat_id=subscriber['chat_id'],
                    text=broadcast_text
                )
                sent_count += 1
            except Exception as e:
                print(f"[DEBUG] broadcast_command - Failed to send to {subscriber['user_id']}: {e}")
                failed_count += 1

        # Log the broadcast
        await log_sent_reminder('broadcast', None, message, sent_count)

        await update.message.reply_text(
            f"Broadcast complete.\n"
            f"Sent: {sent_count}\n"
            f"Failed: {failed_count}"
        )

    except Exception as e:
        print(f"[ERROR] broadcast_command: {e}")
        await update.message.reply_text("Error sending broadcast. Please try again.")


# =============================================================================
# SEND DEADLINE REMINDER (Admin only)
# =============================================================================

async def sendreminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually send a deadline reminder to all subscribers."""
    user = update.effective_user

    # Check if user is admin
    if not await is_admin(user.id):
        await update.message.reply_text("Only admins can send reminders.")
        return

    await track_event(EVENT_ADMIN_ACTION, user, {'action': 'send_reminder'})

    try:
        # Get current GB and deadline
        form_id, _ = await get_current_gb_form_id()
        deadline = await get_deadline()

        if not form_id:
            await update.message.reply_text(
                "No current GB is set. Use /setcurrentgb first."
            )
            return

        if not deadline:
            await update.message.reply_text(
                "No deadline is set. Use /setdeadline first."
            )
            return

        # Get form info
        forms = jotform_helper.get_all_forms()
        form_title = forms.get(form_id, {}).get('title', 'Current GB')

        # Build reminder message
        jotform_url = f"https://form.jotform.com/{form_id}"
        reminder_message = (
            f"⏰ Deadline Reminder\n\n"
            f"The deadline for {form_title} is: {deadline}\n\n"
            f"Don't forget to place your order!\n"
            f"Order form: {jotform_url}"
        )

        # Get subscribers
        subscribers = await get_all_reminder_subscribers()

        if not subscribers:
            await update.message.reply_text("No subscribers to remind.")
            return

        # Send progress message
        await update.message.reply_text(
            f"Sending deadline reminder to {len(subscribers)} subscribers..."
        )

        # Send reminders
        sent_count = 0
        failed_count = 0

        for subscriber in subscribers:
            try:
                await context.bot.send_message(
                    chat_id=subscriber['chat_id'],
                    text=reminder_message
                )
                sent_count += 1
            except Exception as e:
                print(f"[DEBUG] sendreminder_command - Failed to send to {subscriber['user_id']}: {e}")
                failed_count += 1

        # Log the reminder
        await log_sent_reminder('deadline', deadline, reminder_message, sent_count)

        await update.message.reply_text(
            f"Deadline reminder sent.\n"
            f"Sent: {sent_count}\n"
            f"Failed: {failed_count}"
        )

    except Exception as e:
        print(f"[ERROR] sendreminder_command: {e}")
        await update.message.reply_text("Error sending reminder. Please try again.")


# =============================================================================
# CONVERSATION TIMEOUT HANDLER
# =============================================================================

async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle conversation timeout - called when conversation expires."""
    # Try to send a timeout message if we have a way to reach the user
    if update and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="This conversation has timed out due to inactivity.\n"
                     "Please start again with the appropriate command."
            )
        except Exception as e:
            print(f"[DEBUG] conversation_timeout - Failed to send timeout message: {e}")

    # Clear any user data
    if context and context.user_data:
        context.user_data.clear()

    return ConversationHandler.END


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    text_lower = text.lower().strip()
    user = update.effective_user

    # Handle greetings and casual messages quickly (no API calls needed)
    greetings = ['hello', 'hi', 'hey', 'howdy', 'hola', 'yo', 'sup', 'whats up', "what's up",
                 'good morning', 'good afternoon', 'good evening', 'greetings']
    thanks = ['thanks', 'thank you', 'thx', 'ty', 'appreciate it', 'appreciated']

    # Check for simple greetings
    if any(text_lower == g or text_lower.startswith(g + ' ') or text_lower.startswith(g + '!')
           or text_lower.startswith(g + ',') for g in greetings):
        await track_event(EVENT_GREETING, user, {'type': 'greeting'})
        await update.message.reply_text(
            "Hello! I'm Bohemia's Steward. How can I help you today?\n\n"
            "You can ask me about products, prices, or use /help to see available commands."
        )
        return

    # Check for thanks
    if any(t in text_lower for t in thanks):
        await track_event(EVENT_GREETING, user, {'type': 'thanks'})
        await update.message.reply_text("You're welcome! Let me know if you need anything else.")
        return

    # Check for goodbye
    if text_lower in ['bye', 'goodbye', 'see ya', 'later', 'cya']:
        await track_event(EVENT_GREETING, user, {'type': 'goodbye'})
        await update.message.reply_text("Goodbye! Feel free to reach out anytime.")
        return

    # Check if this is a COA/test result question - redirect to admins
    if check_for_coa_test_question(text):
        print(f"[DEBUG] handle_message - COA/test question detected, redirecting to admins")
        await update.message.reply_text(get_admin_redirect_message())
        return

    # Check for out-of-scope requests (pricing, exceptions, negotiations)
    is_out_of_scope, boundary_response = check_out_of_scope_request(text)
    if is_out_of_scope:
        print(f"[DEBUG] handle_message - Out-of-scope request detected")
        await update.message.reply_text(boundary_response)
        return

    # Check if user is asking for the JotForm link / order form
    jotform_keywords = [
        'jotform', 'jot form', 'order form', 'ordering form', 'form link',
        'where is the form', "where's the form", 'link to form', 'link to order',
        'get the form', 'give me the form', 'send me the form', 'need the form',
        'where can i order', 'where do i order', 'where to order',
        'form for the gb', 'form for the current', 'form for current gb',
        'current gb form', 'gb form', 'group buy form'
    ]
    if any(keyword in text_lower for keyword in jotform_keywords):
        print(f"[DEBUG] handle_message - JotForm link request detected")
        try:
            form_id, is_manual = await get_current_gb_form_id()
            if form_id:
                forms = jotform_helper.get_all_forms()
                form_title = forms.get(form_id, {}).get('title', 'Current GB')
                jotform_url = f"https://form.jotform.com/{form_id}"
                await update.message.reply_text(
                    f"Here's the order form for {form_title}:\n\n"
                    f"{jotform_url}\n\n"
                    "Click the link above to place your order!"
                )
            else:
                await update.message.reply_text(
                    "No current GB form is set. Please check back later or DM an admin for assistance."
                )
            return
        except Exception as e:
            print(f"[ERROR] handle_message - JotForm link lookup failed: {e}")
            await update.message.reply_text(
                "I had trouble fetching the form link. Try /jotform or ask an admin."
            )
            return

    # Check FAQ database first (fast, no API calls needed)
    faq_answer = check_faq_match(text)
    if faq_answer:
        print(f"[DEBUG] handle_message - FAQ match found, returning static answer")
        await track_event(EVENT_FAQ_MATCH, user, {'query': text[:100]})
        await update.message.reply_text(faq_answer)
        return

    # Handle timeline questions
    if 'how long' in text_lower or 'timeline' in text_lower or 'timeframe' in text_lower:
        await update.message.reply_text(
            "Due to the volume of GBs, standard production times, shipping speeds, and custom processing timeframes, we estimate that you will receive your items in 4-8 weeks. This timeframe is subject to change if any of the following scenarios apply:\n"
            "- Custom made batches\n"
            "- Package Seizures/Reships\n"
            "- International Shipping\n\n"
            "Please DM an admin if you have any further questions."
        )
        return

    # Try to identify which form the user is asking about using ChatGPT
    try:
        # Get all available forms
        available_forms = jotform_helper.get_all_forms()
        print(f"\n[DEBUG] handle_message - Retrieved {len(available_forms)} forms from JotFormHelper")
        print(f"[DEBUG] handle_message - Form IDs: {list(available_forms.keys())}")

        # Use ChatGPT to analyze the message and identify the form(s)
        form_result = analyze_message_for_gb(text, available_forms)
        print(f"[DEBUG] handle_message - analyze_message_for_gb returned: {form_result}")

        # Check if we got multiple forms (list) or a single form (string)
        if isinstance(form_result, list) and len(form_result) > 1:
            # Multiple forms match - fetch products from all of them
            print(f"[DEBUG] handle_message - Multiple forms detected: {form_result}")

            forms_data = []
            for fid in form_result:
                print(f"[DEBUG] handle_message - Fetching products for form_id: {fid}")
                products = jotform_helper.get_products(fid)

                if products:
                    form_title = available_forms.get(fid, {}).get('title', 'Group Buy')
                    vendor_info = jotform_helper.get_form_metadata(fid)

                    forms_data.append({
                        'form_id': fid,
                        'form_title': form_title,
                        'products': products,
                        'vendor_info': vendor_info
                    })
                    print(f"[DEBUG] handle_message - Form {fid} ({form_title}): {len(products)} products")

            if forms_data:
                print(f"[DEBUG] handle_message - Generating multi-form answer with {len(forms_data)} forms")
                answer = generate_answer_with_multi_form_products(text, forms_data)
                await update.message.reply_text(answer)
            else:
                await update.message.reply_text(
                    "I found multiple forms that might match, but couldn't retrieve products from any of them. "
                    "Please try again later."
                )

        elif form_result:
            # Single form identified (either string or single-item list)
            form_id = form_result[0] if isinstance(form_result, list) else form_result

            # Get products for the identified form
            print(f"[DEBUG] handle_message - Fetching products for form_id: {form_id}")
            products = jotform_helper.get_products(form_id)
            print(f"[DEBUG] handle_message - Retrieved {len(products) if products else 0} products")

            if products:
                # Get form title and metadata (including vendor info)
                form_title = available_forms.get(form_id, {}).get('title', 'Group Buy')

                print(f"[DEBUG] handle_message - Fetching form metadata for vendor info")
                vendor_info = jotform_helper.get_form_metadata(form_id)

                print(f"[DEBUG] handle_message - Generating conversational answer with ChatGPT")

                # Use ChatGPT to generate a natural answer to the user's question
                answer = generate_answer_with_products(text, form_title, products, vendor_info)

                # Track the product search
                await track_event(EVENT_PRODUCT_SEARCH, user, {
                    'query': text[:100],
                    'form_id': form_id,
                    'product_count': len(products)
                })

                print(f"[DEBUG] handle_message - Sending answer to user")
                await update.message.reply_text(answer)
            else:
                await update.message.reply_text(
                    "I found the form, but couldn't retrieve any products. Please try again later."
                )
        else:
            # No form identified - list available forms to help the user
            forms_names = [f"• {form_data['title']}" for fid, form_data in available_forms.items()]
            forms_text = "\n".join(forms_names[:5])  # Show up to 5 forms

            await update.message.reply_text(
                f"I'm not sure which Group Buy you're asking about. Could you please be more specific?\n\n"
                f"Available forms:\n{forms_text}\n\n"
                f"Try mentioning a month (e.g., 'January GB') or ask about the 'current' or 'latest' GB."
            )
    except ExternalServiceError as e:
        log_error("handle_message - External service failure", e, {"user_message": text})
        await update.message.reply_text(
            "I'm having trouble reaching the data source—please try again."
        )
    except Exception as e:
        log_error("handle_message - Unexpected error", e, {"user_message": text})
        await update.message.reply_text(
            "Sorry, I encountered an error processing your request. Please try again later."
        )

async def post_init(application):
    """Initialize database and other startup tasks."""
    print("[STARTUP] Initializing database...")
    await init_db()
    print("[STARTUP] Database initialized.")

    # Register bot commands with Telegram (shows in command menu when user types '/')
    # Only register user-facing commands - admin commands are hidden from menu
    commands = [
        BotCommand("start", "Welcome message"),
        BotCommand("help", "Show all commands"),
        BotCommand("faq", "Frequently asked questions"),
        BotCommand("currentgb", "Show current GB details"),
        BotCommand("products", "List products in current GB"),
        BotCommand("deadline", "Show current GB deadline"),
        BotCommand("vendors", "Show current GB vendors"),
        BotCommand("status", "Show current GB status"),
        BotCommand("jotform", "Get link to order form"),
        BotCommand("listforms", "List available order forms"),
        BotCommand("getorderstatus", "Check your order status"),
        BotCommand("reportproblem", "Report an issue with your order"),
        BotCommand("subscribe", "Subscribe to deadline reminders"),
        BotCommand("unsubscribe", "Unsubscribe from reminders"),
    ]
    await application.bot.set_my_commands(commands)
    print("[STARTUP] Bot commands registered with Telegram.")


def main():
    # Build application with post_init callback
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Register command handlers - General
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("faq", faq_command))

    # Register command handlers - Group Buy Info
    app.add_handler(CommandHandler("currentgb", currentgb_command))
    app.add_handler(CommandHandler("products", products_command))
    app.add_handler(CommandHandler("deadline", deadline_command))
    app.add_handler(CommandHandler("vendors", vendors_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("jotform", jotform_command))
    app.add_handler(CommandHandler("listforms", listforms_command))

    # Register command handlers - Order Support (Conversation Handlers)
    # Check Status Conversation Handler with timeout
    check_status_handler = ConversationHandler(
        entry_points=[CommandHandler("getorderstatus", getorderstatus_command)],
        states={
            STATUS_WAITING_FORM: [
                CallbackQueryHandler(status_form_selected, pattern="^status_")
            ],
            STATUS_WAITING_IDENTIFIER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, status_receive_identifier)
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, conversation_timeout)
            ],
        },
        fallbacks=[CommandHandler("cancel", status_cancel)],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=CONVERSATION_TIMEOUT
    )
    app.add_handler(check_status_handler)

    # Report Problem Conversation Handler with timeout (must be before generic message handler)
    report_problem_handler = ConversationHandler(
        entry_points=[CommandHandler("reportproblem", reportproblem_command)],
        states={
            REPORT_WAITING_INVOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, report_receive_invoice)
            ],
            REPORT_WAITING_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, report_receive_description)
            ],
            REPORT_WAITING_PHOTO: [
                CallbackQueryHandler(report_photo_callback, pattern="^report_photo_"),
                MessageHandler(filters.PHOTO, report_receive_photo),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, conversation_timeout)
            ],
        },
        fallbacks=[CommandHandler("cancel", report_cancel)],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=CONVERSATION_TIMEOUT
    )
    app.add_handler(report_problem_handler)

    # Register command handlers - Reminders
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))

    # Register command handlers - Admin
    app.add_handler(CommandHandler("setcurrentgb", setcurrentgb_command))
    app.add_handler(CommandHandler("clearcurrentgb", clearcurrentgb_command))
    app.add_handler(CommandHandler("setdeadline", setdeadline_command))
    app.add_handler(CommandHandler("cleardeadline", cleardeadline_command))
    app.add_handler(CommandHandler("setvendors", setvendors_command))
    app.add_handler(CommandHandler("clearvendors", clearvendors_command))
    app.add_handler(CommandHandler("setstatus", setstatus_command))
    app.add_handler(CommandHandler("clearstatus", clearstatus_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("listadmins", listadmins_command))
    app.add_handler(CommandHandler("listallforms", listallforms_command))
    app.add_handler(CommandHandler("addformtolist", addformtolist_command))
    app.add_handler(CommandHandler("removeformfromlist", removeformfromlist_command))

    # Register command handlers - Admin Analytics & Broadcast
    app.add_handler(CommandHandler("analytics", analytics_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("sendreminder", sendreminder_command))

    # Register message handler for non-command messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"Bot is running... (Cache TTL: {CACHE_TTL_SECONDS}s)")
    app.run_polling()


if __name__ == '__main__':
    main()
