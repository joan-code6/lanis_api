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
    conv_uniquid = conv.get('Uniquid')
    
    print(f"Testing with Uniquid: {conv_uniquid}")
    
    # Encrypt Uniquid
    encrypted_uniquid = client.cryptor.encrypt(conv_uniquid)
    
    # Test cases
    test_payloads = [
        {'a': 'read', 'uniqid': encrypted_uniquid}, # The hypothesis from read.js
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
                try:
                    # The response in read.js is JSON: data = JSON.parse(data);
                    # And data.message is encrypted: var msg = JSON.parse($.decrypt(data.message));
                    data = resp.json()
                    print("Response is JSON")
                    if 'message' in data:
                        decrypted = client.cryptor.decrypt(data['message'])
                        print("Decryption SUCCESS!")
                        print(decrypted[:200])
                    else:
                        print("No 'message' field in JSON")
                        print(data)
                except Exception as e:
                    print(f"Processing failed: {e}")
                    print(resp.text[:200])
        except Exception as e:
            print(f"Request failed: {e}")
        print("-" * 20)

if __name__ == "__main__":
    test_params()
