import httpx
import json
import sys

API_KEY = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    r = httpx.get(
        'https://openrouter.ai/api/v1/models',
        headers={'Authorization': f'Bearer {API_KEY}'},
        timeout=30
    )
    print(f"Status: {r.status_code}")
    
    if r.status_code == 200:
        data = r.json()
        models = [m['id'] for m in data.get('data', []) if ':free' in m['id']]
        print(f"\nFree models ({len(models)}):")
        for m in sorted(models):
            print(f"  {m}")
    else:
        print(f"Response: {r.text[:1000]}")
except Exception as e:
    print(f"Error: {e}")
