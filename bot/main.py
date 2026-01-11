import os
from dotenv import load_dotenv
from openai import OpenAI
from jotform import JotformAPIClient
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import json
import re

load_dotenv()
jotform = JotformAPIClient(os.getenv('JOTFORM_API_KEY'))
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

class JotFormHelper:
    def __init__(self):
        self.client = JotformAPIClient(os.getenv('JOTFORM_API_KEY'))
        self.forms_cache = {}
        self.products_cache = {} #products are stored here

    def get_all_forms(self):
        # Get list of all forms
        if not self.forms_cache:
            print(f"[DEBUG] JotFormHelper.get_all_forms - Fetching forms from JotForm API")
            forms = self.client.get_forms()
            print(f"[DEBUG] JotFormHelper.get_all_forms - Retrieved {len(forms)} forms from API")
            for form in forms:
                self.forms_cache[form['id']] = {
                    'id': form['id'],
                    'title': form['title'],
                    'created': form.get('created_at','')
                }
                print(f"[DEBUG] JotFormHelper.get_all_forms - Added form: {form['id']} - {form['title']}")
        else:
            print(f"[DEBUG] JotFormHelper.get_all_forms - Using cached forms ({len(self.forms_cache)} forms)")
        return self.forms_cache
    def find_form_by_month(self, month):
        # Find a form that matches a month name
        forms = self.get_all_forms()
        month_lower = month.lower()

        for form_id, form_data in forms.items():
            title_lower = form_data['title'].lower()
            if month_lower in title_lower and 'order' in title_lower:
                return form_id
        return None
    def get_products(self, form_id):
        #Get products from a specific form
        if form_id in self.products_cache:
            print(f"[DEBUG] JotFormHelper.get_products - Using cached products for form {form_id}")
            return self.products_cache[form_id]

        try:
            print(f"[DEBUG] JotFormHelper.get_products - Fetching properties for form {form_id}")
            properties = self.client.get_form_properties(form_id)
            raw_products = properties.get('products', [])
            print(f"[DEBUG] JotFormHelper.get_products - Raw products count: {len(raw_products)}")
            clean_products = self.clean_products(raw_products)
            print(f"[DEBUG] JotFormHelper.get_products - Clean products count: {len(clean_products)}")
            self.products_cache[form_id] = clean_products
            return clean_products
        except Exception as e:
            print(f"[ERROR] JotFormHelper.get_products - Error fetching products: {e}")
            import traceback
            traceback.print_exc()
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

def generate_answer_with_products(user_question, form_title, products):
    """
    Uses ChatGPT to generate a natural conversational answer to the user's question
    based on the available products.
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

    prompt = f"""You are Bohemia's Steward, a helpful assistant for a Group Buy community. A user has asked a question about products in a Group Buy form.

Form: {form_title}

Available Products:
{products_text}

User's Question: "{user_question}"

Please provide a helpful, conversational answer to the user's question based on the products listed above.

Guidelines:
- Be friendly and conversational
- Answer their specific question directly
- If they ask about specific products, provide details about those products
- If they ask general questions like "what's available", give an overview
- If they ask about prices, include pricing information
- If they ask about MOQ (Minimum Order Quantity), stock, or quantity information, provide that data if available
- If MOQ information is in the description field, extract and present it clearly
- If they ask about something not in the product list, politely let them know it's not available in this form
- Keep your response concise but informative
- Use a natural, helpful tone like you're talking to a friend"""

    print(f"[DEBUG] generate_answer_with_products - Generating answer for: '{user_question}'")
    print(f"[DEBUG] generate_answer_with_products - Using {len(products)} products")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )

    answer = response.choices[0].message.content.strip()
    print(f"[DEBUG] generate_answer_with_products - Generated answer length: {len(answer)} chars")

    return answer

def find_form_by_product_names(message_text, available_forms):
    """
    Search through products in all forms to find which form contains
    products mentioned in the user's message.
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

            matches = 0
            matched_products = []

            # Check if any product names appear in the user's message
            for product in products:
                product_name = product.get('name', '').lower()
                if not product_name or product_name == 'n/a':
                    continue

                # Check for exact or partial matches
                # Split product name into words and check if they appear in the message
                product_words = product_name.split()

                # Check if the full product name is in the message
                if product_name in message_lower:
                    matches += 2  # Full match is worth more
                    matched_products.append(product.get('name'))
                    print(f"[DEBUG] find_form_by_product_names - Full match: '{product.get('name')}' in form {form_id}")
                # Check if significant words from product name appear
                elif len(product_words) >= 2:
                    # For multi-word products, check if at least 2 words match
                    word_matches = sum(1 for word in product_words if len(word) > 3 and word in message_lower)
                    if word_matches >= 2:
                        matches += 1
                        matched_products.append(product.get('name'))
                        print(f"[DEBUG] find_form_by_product_names - Partial match: '{product.get('name')}' in form {form_id}")

            if matches > 0:
                form_matches[form_id] = {
                    'score': matches,
                    'products': matched_products,
                    'title': form_data.get('title')
                }
                print(f"[DEBUG] find_form_by_product_names - Form {form_id} ({form_data.get('title')}) has {matches} matches")

        except Exception as e:
            print(f"[DEBUG] find_form_by_product_names - Error checking form {form_id}: {e}")
            continue

    # Return the form with the most matches
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

    # Sort forms by creation date to identify the most recent
    sorted_forms = sorted(
        available_forms.items(),
        key=lambda x: x[1].get('created', ''),
        reverse=True
    )

    forms_list = "\n".join([
        f"- {form_data['title']} (ID: {form_id}, Created: {form_data.get('created', 'Unknown')})"
        for form_id, form_data in sorted_forms
    ])

    prompt = f"""You are helping identify which Group Buy (GB) form a user is asking about.

Available forms (sorted by most recent first):
{forms_list}

User message: "{message_text}"

Analyze the user's message and determine which form they're asking about:
1. If they mention a specific month name (January, February, November, December, etc.), look for that month in the form title
2. If they ask about "current", "latest", or "newest" GB, choose the FIRST form in the list (most recent)
3. If they mention a date, match it to the closest form by creation date
4. If they mention a vendor name, try to match it to a form title
5. If the message is completely unclear or ambiguous, respond with "UNCLEAR"

IMPORTANT: Respond with ONLY the form ID number (e.g., "253411113426040") or the word "UNCLEAR".
Do not include any other text, explanation, or formatting."""

    print(f"\n[DEBUG] User message: {message_text}")
    print(f"[DEBUG] Available forms: {len(available_forms)}")
    print(f"[DEBUG] Forms list sent to ChatGPT:\n{forms_list}\n")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
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

# Start/Welcome Message
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello there! I'm Bohemia's Steward. I'm alive!")
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available commands:\n/start - Say hello\n/help - Show this message")
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    text_lower = text.lower()

    # Handle timeline questions
    if 'how long' in text_lower or 'timeline' in text_lower or 'timeframe' in text_lower:
        await update.message.reply_text(
            "On average, GBs take around 4-8 weeks to be completed. This timeframe does not include vendor production on custom-made batches, custom delays, or seizures. International shipping from the GBO to members can take longer.\n"
            "Use /help for more commands!"
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
                # Get form title
                form_title = available_forms.get(form_id, {}).get('title', 'Group Buy')

                print(f"[DEBUG] handle_message - Generating conversational answer with ChatGPT")

                # Use ChatGPT to generate a natural answer to the user's question
                answer = generate_answer_with_products(text, form_title, products)

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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()