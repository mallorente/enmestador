"""Setup helper: export cookies from your real browser for X.com and LinkedIn.

INSTRUCTIONS:
1. Install the Chrome extension "Get cookies.txt LOCALLY" (or "Export Cookies")
2. Go to x.com (make sure you're logged in)
3. Click the extension → Export cookies for x.com → Save as "x_cookies.txt"
4. Go to linkedin.com (make sure you're logged in)
5. Click the extension → Export cookies for linkedin.com → Save as "li_cookies.txt"
6. Place both files in: ./volumes/user_data/

That's it. The pipeline will load these cookies automatically.

Alternative extensions:
- "Get cookies.txt LOCALLY" (Chrome Web Store)
- "Export Cookies" (Firefox)
- "Cookie-Editor" → Export as Netscape
"""
import os
from pathlib import Path

USER_DATA_DIR = os.getenv("USER_DATA_DIR", "./volumes/user_data")

def check_cookies():
    user_data = Path(USER_DATA_DIR)
    x_cookies = user_data / "x_cookies.txt"
    li_cookies = user_data / "li_cookies.txt"
    
    print("=== Cookie Check ===")
    print(f"Looking in: {user_data.absolute()}")
    print(f"X.com cookies: {'FOUND' if x_cookies.exists() else 'MISSING'}")
    print(f"LinkedIn cookies: {'FOUND' if li_cookies.exists() else 'MISSING'}")
    
    if not x_cookies.exists() or not li_cookies.exists():
        print("\nPlease export cookies from your browser:")
        print("1. Install 'Get cookies.txt LOCALLY' extension")
        print("2. Go to x.com (logged in) → export → save as 'x_cookies.txt'")
        print("3. Go to linkedin.com (logged in) → export → save as 'li_cookies.txt'")
        print(f"4. Place both files in: {user_data.absolute()}")
    else:
        print("\nAll set! You can now run: python main.py")

if __name__ == "__main__":
    check_cookies()
