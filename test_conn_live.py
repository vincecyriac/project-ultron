import asyncio
import os
from google import genai
from dotenv import load_dotenv

load_dotenv('project_ultron/.env')

async def main():
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
    
    models_to_test = [
        'gemini-3.1-flash-live-preview',
        'gemini-2.5-flash-native-audio-latest',
        'gemini-2.0-flash'
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
