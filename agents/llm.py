import os

from dotenv import load_dotenv

load_dotenv()


def generate_text(provider: str, prompt: str, *, system_prompt: str = "") -> str:
    if provider == "gemini":
        from google import genai

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("Chưa tìm thấy GEMINI_API_KEY trong file .env")

        client = genai.Client(api_key=api_key)

        from google.genai import types

        config = types.GenerateContentConfig(system_instruction=system_prompt) if system_prompt else None
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=prompt,
            config=config,
        )

        return response.text.strip()

    if provider == "openai":
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Chưa tìm thấy OPENAI_API_KEY trong file .env")

        client = OpenAI(api_key=api_key)

        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            instructions=system_prompt or None,
            input=prompt,
        )

        return response.output_text.strip()

    raise ValueError("Provider phải là gemini hoặc openai")
