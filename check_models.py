import asyncio, os, httpx
from dotenv import load_dotenv
load_dotenv()

async def main():
    key = os.getenv("LLM_FALLBACK_3_API_KEY", "")
    async with httpx.AsyncClient() as c:
        r = await c.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        data = r.json()
        free = [m for m in data.get("data", []) if ":free" in m.get("id", "")]
        for m in sorted(free, key=lambda x: x["id"]):
            print(m["id"])

asyncio.run(main())
