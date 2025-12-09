import sys
import os
import json
import requests
from rich import print as rprint

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from functions.base import SchulportalHessenAPI
from functions.tools.cryptor import Cryptor

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "CLI", "sph_config.json")
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return json.load(f)
    return None

def test_params():
    config = load_config()
    if not config:
        print("No config found")
        return

    client = SchulportalHessenAPI()
    print("Logging in...")
    client.login(config['school_id'], config['username'], config['password'])
    
    print("Initializing encryption...")
    client.cryptor = Cryptor(client.session)
    client.cryptor.authenticate()
    
    # Get a conversation ID first
    print("Fetching headers...")
    response = client.session.post(
        f"{client.BASE_START_URL}/nachrichten.php",
        data={'a': 'headers', 'getType': 'All', 'last': '0'},
        headers={'X-Requested-With': 'XMLHttpRequest'}
    )
    data = response.json()
    decrypted = client.cryptor.decrypt(data['rows'])
    conversations = json.loads(decrypted)
    
    if not conversations:
        print("No conversations found to test with.")
        return

    conv = conversations[0]
    conv_id = conv.get('Id')
    conv_uniquid = conv.get('Uniquid')
    
    print(f"Testing with ID: {conv_id}, Uniquid: {conv_uniquid}")
    
    # Encrypt ID
    encrypted_id = client.cryptor.encrypt(conv_id)
    
    # Test cases
    test_payloads = [
        {'a': 'refresh', 'id': conv_id, 'last': '0'},
        {'a': 'refresh', 'id': encrypted_id, 'last': '0'}, # Encrypted ID
        {'a': 'refresh', 'id': conv_id, 'last': '0', 'getType': 'All'}, # Add getType
    ]
    
    for i, payload in enumerate(test_payloads):
        print(f"Testing payload: {payload}")
        try:
            resp = client.session.post(
                f"{client.BASE_START_URL}/nachrichten.php",
                data=payload,
                headers={'X-Requested-With': 'XMLHttpRequest'}
            )
            print(f"Status: {resp.status_code}")
            print(f"Content start: {resp.text[:50]}")
            
            if resp.text != "-2" and len(resp.text) > 10:
                # Save to file for inspection
                with open(f"temp/response_{i}.html", "w", encoding="utf-8") as f:
                    f.write(resp.text)
                print(f"Saved to temp/response_{i}.html")

                try:
                    decrypted = client.cryptor.decrypt(resp.text)
                    print("Decryption SUCCESS!")
                    print(decrypted[:100])
                except:
                    print("Decryption failed (expected if not encrypted)")
        except Exception as e:
            print(f"Request failed: {e}")
        print("-" * 20)

    # Download read.js
    print("Downloading read.js...")
    try:
        resp = client.session.get(f"{client.BASE_START_URL}/module/nachrichten/js/read.js")
        print(f"Status: {resp.status_code}")
        with open("temp/read.js", "w", encoding="utf-8") as f:
            f.write(resp.text)
        print("Saved to temp/read.js")
    except Exception as e:
        print(f"Download failed: {e}")

if __name__ == "__main__":
    test_params()
