import os
import time
from dotenv import load_dotenv
from openai import OpenAI
from jotform import JotformAPIClient
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import json
import re

load_dotenv()

# Cache TTL configuration (default: 5 minutes)
CACHE_TTL_SECONDS = int(os.getenv('CACHE_TTL_SECONDS', 300))

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
        "keywords": ["how to order", "how do i order", "how can i order", "place an order", "make an order", "ordering process", "how to place order", "how to buy", "how do i buy"],
        "answer": "To place an order:\n1. Find the current Group Buy form (ask about the 'current GB')\n2. Fill out the JotForm with your product selections\n3. Submit your order before the deadline\n4. Follow the payment instructions provided\n5. Wait for shipping confirmation\n\nIf you need help with a specific step, please ask!"
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
    "minimum order": {
        "keywords": ["minimum order", "min order", "moq", "minimum quantity", "minimum purchase", "smallest order"],
        "answer": "Minimum Order Quantities (MOQ) vary by product and are listed in each product's description on the order form. Some products have no minimum, while others require a minimum quantity to be ordered. Check the specific product listing for MOQ details."
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
            forms = self.client.get_forms()
            print(f"[DEBUG] JotFormHelper.get_all_forms - Retrieved {len(forms)} forms from API")

            # Clear old cache
            self.forms_cache = {}

            for form in forms:
                # Get latest submission date for each form
                latest_submission = None
                try:
                    submissions = self.client.get_form_submissions(form['id'], limit=1, orderby='created_at')
                    if submissions and len(submissions) > 0:
                        latest_submission = submissions[0].get('created_at', '')
                        print(f"[DEBUG] JotFormHelper.get_all_forms - Form {form['id']} latest submission: {latest_submission}")
                except Exception as e:
                    print(f"[DEBUG] JotFormHelper.get_all_forms - Could not fetch submissions for {form['id']}: {e}")

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

        except Exception as e:
            print(f"[ERROR] JotFormHelper.get_all_forms - Error fetching forms: {e}")
            # If we have stale cache data, return it rather than nothing
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
            properties = self.client.get_form_properties(form_id)
            raw_products = properties.get('products', [])
            print(f"[DEBUG] JotFormHelper.get_products - Raw products count: {len(raw_products)}")
            clean_products = self.clean_products(raw_products)
            print(f"[DEBUG] JotFormHelper.get_products - Clean products count: {len(clean_products)}")

            # Update cache and timestamp
            self.products_cache[form_id] = clean_products
            self.products_cache_timestamps[form_id] = time.time()
            print(f"[DEBUG] JotFormHelper.get_products - Cache refreshed for form {form_id}")

            return clean_products
        except Exception as e:
            print(f"[ERROR] JotFormHelper.get_products - Error fetching products: {e}")
            import traceback
            traceback.print_exc()
            # Return stale cache if available
            if form_id in self.products_cache:
                print(f"[DEBUG] JotFormHelper.get_products - Returning stale cache due to error")
                return self.products_cache[form_id]
            return []
        
    def clean_products(self, products):
        clean_products_list = []
        for product in products:
            product_data = {
                'name': product.get('name', 'N/A'),
                'price': product.get('price', 'N/A'),
                'description': product.get('description', 'N/A')
            }

            # Try to extract MOQ and other potentially useful fields
            if 'quantity' in product:
                product_data['quantity'] = product.get('quantity')
            if 'moq' in product:
                product_data['moq'] = product.get('moq')
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
- IMPORTANT: The Description field contains critical information including MOQ, lab details, testing info, and vendor specifics - ALWAYS read and use this information when answering questions
- If asked about MOQ, lab info, testing, or vendor details, search the Description field carefully
- Keep responses SHORT and direct"""

    print(f"[DEBUG] generate_answer_with_products - Generating answer for: '{user_question}'")
    print(f"[DEBUG] generate_answer_with_products - Using {len(products)} products")

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.95
    )

    answer = response.choices[0].message.content.strip()
    print(f"[DEBUG] generate_answer_with_products - Generated answer length: {len(answer)} chars")

    return answer

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

def find_form_by_product_names(message_text, available_forms):
    """
    Search through products in all forms to find which form contains
    products mentioned in the user's message. Uses fuzzy matching for product names.
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

    # Return the form with the highest score
    if form_matches:
        best_match = max(form_matches.items(), key=lambda x: x[1]['score'])
        form_id = best_match[0]
        match_info = best_match[1]
        print(f"[DEBUG] find_form_by_product_names - Best match: {form_id} ({match_info['title']}) with products: {match_info['products']}")
        return form_id

    print(f"[DEBUG] find_form_by_product_names - No product matches found")
    return None

def analyze_message_for_gb(message_text, available_forms):
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

    # Sort forms by latest submission date to identify the most recent/current GB
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
5. If the message is completely unclear or ambiguous, respond with "UNCLEAR"

NOTE: Forms are sorted by latest submission date, NOT creation date. The first form is the most currently active GB.

IMPORTANT: Respond with ONLY the form ID number (e.g., "253411113426040") or the word "UNCLEAR".
Do not include any other text, explanation, or formatting."""

    print(f"\n[DEBUG] User message: {message_text}")
    print(f"[DEBUG] Available forms: {len(available_forms)}")
    print(f"[DEBUG] Forms list sent to ChatGPT:\n{forms_list}\n")

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
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
        # Try product-based search as fallback
        print(f"[DEBUG] Trying product-based search as fallback...")
        return find_form_by_product_names(message_text, available_forms)
    else:
        print(f"[DEBUG] ChatGPT returned UNCLEAR, trying product-based search as fallback...")
        # Try to find form by searching for product names in the message
        return find_form_by_product_names(message_text, available_forms)

# Initialize global JotFormHelper instance
jotform_helper = JotFormHelper()

# =============================================================================
# BOT COMMAND HANDLERS
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message for new users."""
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
    await update.message.reply_text(
        "Available commands:\n"
        "/start - Welcome message\n"
        "/help - Show this message\n"
        "/faq - Show frequently asked questions\n"
        "/refresh - Refresh cached data (admin)\n\n"
        "You can also just ask me questions like:\n"
        "- 'What products are in the current GB?'\n"
        "- 'How do I place an order?'\n"
        "- 'What's the price of Retatrutide?'\n"
        "- 'How long does shipping take?'"
    )

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
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    text_lower = text.lower()

    # Check if this is a COA/test result question - redirect to admins
    if check_for_coa_test_question(text):
        print(f"[DEBUG] handle_message - COA/test question detected, redirecting to admins")
        await update.message.reply_text(get_admin_redirect_message())
        return

    # Check FAQ database first (fast, no API calls needed)
    faq_answer = check_faq_match(text)
    if faq_answer:
        print(f"[DEBUG] handle_message - FAQ match found, returning static answer")
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
        await update.message.reply_text("🤔 Let me check that for you...")

        # Get all available forms
        available_forms = jotform_helper.get_all_forms()
        print(f"\n[DEBUG] handle_message - Retrieved {len(available_forms)} forms from JotFormHelper")
        print(f"[DEBUG] handle_message - Form IDs: {list(available_forms.keys())}")

        # Use ChatGPT to analyze the message and identify the form
        form_id = analyze_message_for_gb(text, available_forms)
        print(f"[DEBUG] handle_message - analyze_message_for_gb returned: {form_id}")

        if form_id:
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

                print(f"[DEBUG] handle_message - Sending answer to user")
                await update.message.reply_text(answer)
            else:
                await update.message.reply_text(
                    "I found the form, but couldn't retrieve any products. Please try again later."
                )
        else:
            # List available forms to help the user
            forms_names = [f"• {form_data['title']}" for form_id, form_data in available_forms.items()]
            forms_text = "\n".join(forms_names[:5])  # Show up to 5 forms

            await update.message.reply_text(
                f"I'm not sure which Group Buy you're asking about. Could you please be more specific?\n\n"
                f"Available forms:\n{forms_text}\n\n"
                f"Try mentioning a month (e.g., 'January GB') or ask about the 'current' or 'latest' GB."
            )
    except Exception as e:
        print(f"Error in handle_message: {e}")
        await update.message.reply_text(
            "Sorry, I encountered an error processing your request. Please try again later."
        )
def main():
    app = Application.builder().token(TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("faq", faq_command))
    app.add_handler(CommandHandler("refresh", refresh_command))

    # Register message handler for non-command messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"Bot is running... (Cache TTL: {CACHE_TTL_SECONDS}s)")
    app.run_polling()

if __name__ == '__main__':
    main()