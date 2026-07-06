import os
from google import genai

key = "AQ.Ab8RN6IPZGjUmO6tDHUGjiNL_2HDOianNzJAaNDchGwyovYKhg"
try:
    print(f"Initializing genai client with key starting with {key[:4]}")
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents='Say hello.'
    )
    print("Response text:", response.text)
except Exception as e:
    print("Exception occurred:", repr(e))
