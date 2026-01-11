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
            forms = self.client.get_forms()
            for form in forms:
                self.forms_cache[form['id']] = {
                    'id': form['id'],
                    'title': form['title'],
                    'created': form.get('created_at','')
                }
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
            return self.products_cache[form_id]
        
        try:
            properties = self.client.get_form_properties(form_id)
            raw_products = properties.get('products', [])
            clean_products = self.clean_products(raw_products)
            self.products_cache[form_id] = clean_products
            return clean_products
        except Exception as e:
            print(f"Error fetching products: {e}")
            return []
        
    def clean_products(self, products):
        clean_products_list = []
        for product in products:
            clean_products_list.append({
                'name': product.get('name', 'N/A'),
                'price': product.get('price', 'N/A'),
                'description': product.get('description', 'N/A')
            })
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

def analyze_message_for_gb(message_text, available_forms):
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    forms_list = "\n".join([f"- {form_data['title']} (ID: {form_id})" for form_id, form_data in available_forms.items()])
    prompt = f"""You are helping identify which Group Buy (GB) form a user is asking about.
Available forms:
{forms_list}

User message: "{message_text}"

Based on the user's message, which form are they asking about?
- If they mention a specific month (like "January", "November"), match it to that form
- If they say "current GB" or "latest", pick the most recent form
- If they mention a specific vendor, pick the most recent form with the vendor listed as a supplier
- If unclear, respond with "UNCLEAR"
Respond with ONLY the form ID (the number) or "UNCLEAR". Nothing else."""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    
    result = response.choices[0].message.content.strip()
    return result if result != "UNCLEAR" else None

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

        # Use ChatGPT to analyze the message and identify the form
        form_id = analyze_message_for_gb(text, available_forms)

        if form_id:
            # Get products for the identified form
            products = jotform_helper.get_products(form_id)

            if products:
                # Get form title
                form_title = available_forms.get(form_id, {}).get('title', 'Group Buy')

                # Format the response
                response = f"📋 *{form_title}*\n\n"
                response += f"Found {len(products)} product(s):\n\n"

                for idx, product in enumerate(products, 1):
                    name = product.get('name', 'N/A')
                    price = product.get('price', 'N/A')
                    description = product.get('description', 'N/A')

                    response += f"{idx}. *{name}*\n"
                    response += f"   💰 Price: ${price}\n"

                    # Truncate long descriptions
                    if len(description) > 100:
                        description = description[:100] + "..."
                    response += f"   📝 {description}\n\n"

                await update.message.reply_text(response, parse_mode='Markdown')
            else:
                await update.message.reply_text(
                    "I found the form, but couldn't retrieve any products. Please try again later."
                )
        else:
            await update.message.reply_text(
                "I'm not sure which Group Buy you're asking about. Could you please be more specific? "
                "Try mentioning a month (e.g., 'January GB') or ask about the 'current' or 'latest' GB."
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