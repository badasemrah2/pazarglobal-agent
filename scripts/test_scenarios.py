import json
import os
import time
import uuid
import urllib.request

API_BASE = os.getenv("AGENT_API_BASE", "http://localhost:8000/api/v1")
MEDIA_URL = os.getenv(
    "TEST_MEDIA_URL",
    "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3f/Fronalpstock_big.jpg/640px-Fronalpstock_big.jpg",
)
USER_ID = os.getenv("TEST_USER_ID", str(uuid.uuid4()))
CHANNEL = os.getenv("TEST_CHANNEL", "webchat")


def _post_message(message: str, media_urls=None, metadata=None):
    payload = {
        "user_id": USER_ID,
        "message": message,
        "media_urls": media_urls,
        "channel": CHANNEL,
        "metadata": metadata or {},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/message",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def run_scenario(name, steps):
    print("\n" + "=" * 80)
    print(f"SCENARIO: {name}")
    print("=" * 80)
    # Clear session before each scenario
    _post_message("iptal")
    time.sleep(0.3)
    
    for idx, step in enumerate(steps, start=1):
        msg = step.get("message", "")
        media = step.get("media_urls")
        metadata = step.get("metadata")
        print(f"\n[{idx}] -> message={msg!r} media={bool(media)}")
        res = _post_message(msg, media_urls=media, metadata=metadata)
        print("<-", res.get("text", ""))
        if res.get("metadata"):
            print("metadata:", json.dumps(res["metadata"], ensure_ascii=False))
        time.sleep(step.get("sleep", 0.5))


def main():
    scenarios = [
        {
            "name": "Resim -> ilan başlat",
            "steps": [
                {"message": "", "media_urls": [MEDIA_URL]},
                {"message": "ilan vermek istiyorum"},
            ],
        },
        {
            "name": "İlan başlat -> resim",
            "steps": [
                {"message": "ilan vermek istiyorum"},
                {"message": "", "media_urls": [MEDIA_URL]},
            ],
        },
        {
            "name": "İlan sırasında fiyat öner",
            "steps": [
                {"message": "ilan vermek istiyorum"},
                {"message": "SkyHawk Hard Disk"},
                {"message": "fiyat öner"},
            ],
        },
        {
            "name": "Fiyat araştır -> ilana devam et",
            "steps": [
                {"message": "fiyat öner"},
                {"message": "Nike koşu ayakkabısı 2. el"},
                {"message": "ilana devam et"},
            ],
        },
        {
            "name": "Arama -> ilan verme",
            "steps": [
                {"message": "telefon var mı"},
                {"message": "ilan vermek istiyorum"},
            ],
        },
        {
            "name": "İlan -> arama",
            "steps": [
                {"message": "ilan vermek istiyorum"},
                {"message": "telefon var mı"},
            ],
        },
        {
            "name": "Kategori bilmiyorum",
            "steps": [
                {"message": "ilan vermek istiyorum"},
                {"message": "Nike koşu ayakkabısı"},
                {"message": "2500"},
                {"message": "2. el"},
                {"message": "İstanbul"},
                {"message": "bilmiyorum"},
            ],
        },
    ]

    print(f"API_BASE: {API_BASE}")
    print(f"USER_ID: {USER_ID}")
    for sc in scenarios:
        run_scenario(sc["name"], sc["steps"])


if __name__ == "__main__":
    main()
