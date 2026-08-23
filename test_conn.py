import asyncio
import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

async def main():
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
    
    models_to_test = [
        'gemini-2.0-flash-exp',
        'gemini-2.0-flash-live',
        'gemini-2.5-flash-live',
        'gemini-live-2.5-flash-native-audio'
    ]
    
    for model in models_to_test:
        print(f"Testing connection with model '{model}'...")
        try:
            async with client.aio.live.connect(model=model, config={'response_modalities': ['AUDIO']}) as session:
                print(f"SUCCESS: Connected successfully to model '{model}'!")
                return
        except Exception as e:
            print(f"FAILED for model '{model}': {e}\n")

if __name__ == "__main__":
    asyncio.run(main())
