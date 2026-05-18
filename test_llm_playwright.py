"""Test LLM connectivity before, during, and after Playwright."""
import asyncio
import os
from dotenv import load_dotenv
from openai import AsyncOpenAI
from playwright.async_api import async_playwright

load_dotenv()


async def test_llm(label: str) -> None:
    client = AsyncOpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
        timeout=60.0,
        max_retries=0,
    )
    try:
        r = await client.chat.completions.create(
            model="kimi-k2.6",
            messages=[{"role": "user", "content": "Reply OK"}],
            temperature=0.1,
            max_tokens=10,
        )
        content = r.choices[0].message.content or ""
        print(f"  {label}: SUCCESS - {content[:50]}")
    except Exception as e:
        print(f"  {label}: FAILED - {type(e).__name__}: {str(e)[:120]}")


async def main() -> None:
    print("1. Testing LLM BEFORE Playwright...")
    await test_llm("BEFORE")

    print("\n2. Starting Playwright...")
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir="volumes/user_data",
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--window-position=-32000,-32000",
        ],
    )
    print("   Playwright started.")

    print("\n3. Testing LLM DURING Playwright...")
    await test_llm("DURING")

    print("\n4. Closing Playwright...")
    await ctx.close()
    await pw.stop()
    print("   Playwright stopped.")

    print("\n5. Testing LLM AFTER Playwright...")
    await test_llm("AFTER")


if __name__ == "__main__":
    asyncio.run(main())