import os
from dotenv import load_dotenv

load_dotenv()

try:
    from xai_sdk import Client
    from xai_sdk.chat import user
    
    print("xai_sdk import successful!")
    
    # We won't actually hit the API without a real key unless we want to, 
    # but let's test if the package initializes
    api_key = os.getenv("XAI_API_KEY", "dummy_key_to_test_instantiation")
    client = Client(api_key=api_key)
    print("Client instantiated successfully!")
    
except ImportError as e:
    print(f"ImportError: {e}")
except Exception as e:
    print(f"Other Error: {e}")
