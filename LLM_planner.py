from dotenv import load_dotenv
import os
from groq import Groq

# Load .env file
load_dotenv()

# Get API key
api_key = os.getenv("groq_llm_apikey1") or os.getenv("GROQ_API_KEY")

if not api_key:
    raise RuntimeError(
        "Missing Groq API key. Put `groq_llm_apikey1=...` in .env (same folder as this script) "
        "or set `GROQ_API_KEY` env var."
    )

# Create client
client = Groq(api_key=api_key)

# Make test request (route to fallback model if the first fails)
primary_model = "openai/gpt-oss-20b"
fallback_model = "openai/gpt-oss-7b"

messages = [{"role": "user", "content": "Say 'API is working' in one line."}]

try:
    response = client.chat.completions.create(model=primary_model, messages=messages)
except Exception as e:
    # If primary doesn't respond/works, try fallback
    print(f"Primary model failed ({primary_model}): {e}")
    response = client.chat.completions.create(model=fallback_model, messages=messages)

print(response.choices[0].message.content)
