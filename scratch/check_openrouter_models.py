import asyncio
import aiohttp
import json

async def run():
    print("Fetching OpenRouter models...")
    async with aiohttp.ClientSession() as session:
        async with session.get("https://openrouter.ai/api/v1/models") as resp:
            data = await resp.json()
            
            models = data.get("data", [])
            free_models = []
            
            for m in models:
                pricing = m.get("pricing", {})
                prompt_price = pricing.get("prompt", "0")
                completion_price = pricing.get("completion", "0")
                
                # Check if it's a free model
                if prompt_price == "0" and completion_price == "0":
                    # Filter out purely embedding or non-chat models if any, but usually they are chat
                    free_models.append(m)
            
            # Sort by name or context length
            free_models.sort(key=lambda x: x.get("context_length", 0), reverse=True)
            
            print(f"Found {len(free_models)} free models. Here are some of the most notable ones:")
            
            notable_keywords = ["llama", "gemini", "mistral", "qwen", "phi", "gemma"]
            
            count = 0
            for m in free_models:
                name = m.get("id", "")
                if any(kw in name.lower() for kw in notable_keywords) or count < 10:
                    ctx = m.get("context_length", 0)
                    limits = m.get("top_provider", {}).get("max_completion_tokens", "N/A")
                    print(f"- ID: {name}")
                    print(f"  Name: {m.get('name')}")
                    print(f"  Context: {ctx} tokens")
                    print()
                    count += 1
                if count >= 20:
                    break

if __name__ == "__main__":
    asyncio.run(run())
