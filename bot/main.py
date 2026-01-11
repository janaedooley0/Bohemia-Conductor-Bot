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

# Start/Welcome Message
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello there! I'm Bohemia's Steward. I'm alive!")
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available commands:\n/start - Say hello\n/help - Show this message")
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if 'how long' in text or 'timeline' in text or 'timeframe' in text:
        await update.message.reply_text(
            "On average, GBs take around 4-8 weeks to be completed. This timeframe does not include vendor production on custom-made batches, custom delays, or seizures. International shipping from the GBO to members can take longer.\n"
            "Use /help for more commands!"
        )
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    form_id = '253411113426040'
    jotform_helper = JotFormHelper()
    jotform_helper.get_products(form_id)
    print(jotform_helper.products_cache[form_id])
    main()