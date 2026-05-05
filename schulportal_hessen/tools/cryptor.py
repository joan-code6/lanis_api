# Made by spezian https://github.com/lanis-mobile/LanisAPI/blob/deprecated-0.4.1/src%2Flanisapi%2Fhelpers%2Fcryptor.py
# Adapted for standalone use
"""This script has the Cryptor class for decrypting the messages."""

import base64
import re
import time
from hashlib import md5
from json.decoder import JSONDecodeError
from random import randint, random, seed
from typing import Optional

import requests
from Crypto import Random
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA


class Cryptor:
    """Encryption handler for Schulportal Hessen secure communications.

    This class provides encryption and decryption capabilities required
    for communicating with Schulportal Hessen. It handles the hybrid
    RSA/AES encryption scheme used by SPH to protect message content.

    The encryption works as follows:
    1. On authentication, retrieve the RSA public key from SPH
    2. Generate a random AES-256 session key
    3. Encrypt the AES key with RSA and store it server-side
    4. Use AES-CBC for all subsequent message encryption/decryption

    Parameters
    ----------
    session : requests.Session
        The authenticated requests session to use for API calls.

    Attributes
    ----------
    session : requests.Session
        The active HTTP session.
    secret : str, optional
        The decrypted AES session key (after authentication).
    authenticated : bool
        Whether encryption has been successfully initialized.

    Raises
    ------
    Exception
        If RSA key retrieval or encryption setup fails.

    Notes
    -----
    Original implementation based on work by spezian from the LanisAPI project.
    Key derivation algorithm adapted from:
    - https://stackoverflow.com/questions/36762098/how-to-decrypt-password-from-javascript-cryptojs-aes-encryptpassword-passphras
    - https://github.com/koenidv/sph-planner/.../Cryption.kt
    """

    BASE_URL = "https://start.schulportal.hessen.de"

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self.secret: Optional[str] = None
        self.authenticated = False

    def _bytes_to_key(self, data: bytes, salt: bytes, output: int = 48) -> bytes:
        """Derive cryptographic key from data and salt using MD5 iteration.

        Implements PKCS#7 style key derivation using MD5 hash chains.
        This is used to derive AES encryption keys from password/salt pairs.

        Parameters
        ----------
        data : bytes
            Input data to hash.
        salt : bytes
            Salt value (must be exactly 8 bytes).
        output : int, optional
            Desired key length in bytes (default: 48).

        Returns
        -------
        bytes
            Derived key of specified length.

        Raises
        ------
        AssertionError
            If salt length is not exactly 8 bytes.

        See Also
        --------
        https://gist.github.com/gsakkis/4546068
        """
        # extended from https://gist.github.com/gsakkis/4546068
        assert len(salt) == 8, len(salt)
        data += salt
        key = md5(data).digest()
        final_key = key
        while len(final_key) < output:
            key = md5(key + data).digest()
            final_key += key
        return final_key[:output]

    def _pad(self, data: bytes) -> bytes:
        """Pad plain data.

        Parameters
        ----------
        data : bytes
            The plain data.

        Returns
        -------
        bytes
            The padded plain data.

        Note
        ----
        I don't know what this does but it does work.
        """
        length = 16 - (len(data) % 16)  # Block size = 16
        return data + (chr(length) * length).encode()

    def _unpad(self, data: bytes) -> str:
        """Unpad decrypted data.

        Parameters
        ----------
        data : bytes
            Decrypted data.

        Returns
        -------
        str
            The unpadded decrypted data.

        Note
        ----
        I don't know what this does but it does work.
        """
        return data[
            : -(data[-1] if isinstance(data[-1], int) else ord(data[-1]))
        ].decode()

    def _random_letter(self, letter: str) -> str:
        """Return a pseudo-random letter.

        Returns
        -------
        str
            The letter.
        """
        timestamp = time.time()

        seed(timestamp)
        random_value = round((timestamp + random() * 16) % 16)

        return f"{random_value:x}"

    def _generate_key(self) -> str:
        """Generate a pseudorandom AES key for encrypting and decrypting.

        Returns
        -------
        str
            The key.

        Note
        ----
        UUIDs aren't meant to be random.
        Lanis pls fix.
        """
        pattern = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx-xxxxxx3xx"

        key = re.sub(pattern=r"[xy]", string=pattern, repl=self._random_letter)

        print(
            f"Cryptor - Generate key: Generated key {key[:8]}-....-4...-....-............-......3..."
        )

        return self.encrypt(key, key)

    def _handshake(self, encrypted_key: str) -> str:
        """Tell Lanis how to encrypt data.

        Parameters
        ----------
        encrypted_key : str
            The encrypted key by `_encrypt_key`.

        Returns
        -------
        str
            Encrypted secret with our secret.
            It's used to check if both parties are encrypting equally.
        """
        response = self.session.post(
            f"{self.BASE_URL}/ajax.php",
            params={"f": "rsaHandshake", "s": str(randint(0, 2000))},
            data={"key": encrypted_key},
        )

        try:
            challenge = str(response.json()["challenge"])
        except JSONDecodeError as error:
            # Occurs if challenge is not in JSON, often that means its just blank.
            print(
                f"Cryptor - Handshake: Error decoding json {response.content} - {error}"
            )
            raise

        return challenge

    def _challenge(self, challenge: str) -> bool:
        """Check if Lanis and LanisAPI are encrypting equally.

        Parameters
        ----------
        challenge : str
            The encrypted secret from Lanis.

        Returns
        -------
        bool
            If `False` it failed, if `True` it isn't `False`.
        """
        _challenge = self.decrypt(challenge) == self.secret

        print(f"Cryptor - Challenge: Result is {_challenge}")

        return _challenge

    def _get_public_key(self) -> str:
        """Get Schulportal Hessens public rsa key.

        Returns
        -------
        str
            The rsa key.
        """
        try:
            response = self.session.get(
                f"{self.BASE_URL}/ajax.php", params={"f": "rsaPublicKey"}
            )
            response.raise_for_status()
        except requests.RequestException as error:
            print(f"Cryptor - Public key: Error getting public key - {error}")
            raise

        try:
            public_key = response.json()["publickey"]
        except JSONDecodeError as error:
            # Occurs if public_key is not in JSON, often that means its just blank.
            print(
                f"Cryptor - Public key: Error decoding json {response.content} - {error}"
            )
            raise

        return public_key

    def _encrypt_key(self, public_key: str) -> str:
        """Encrypts the secret with the public key.

        Parameters
        ----------
        public_key : str
            The public key from Schulportal Hessen.

        Returns
        -------
        str
            The encrypted key.
        """
        rsa = PKCS1_v1_5.new(RSA.import_key(public_key))

        encrypted = base64.b64encode(rsa.encrypt(self.secret.encode())).decode()

        print(f"Cryptor - Encrypt key: Encrypted key {encrypted[:8]}......")

        return encrypted

    def encrypt(self, plain: str, secret: str = None) -> Optional[str]:
        """Encrypts a given text with AES in CBC mode.

        Parameters
        ----------
        plain : str
            The text to encrypt.
        secret : str, optional
            If given use this secret not the generated key, by default None

        Returns
        -------
        str
            The encrypted text.

        Note
        ----
        CBC encryption isn't the best there are better solutions.
        """
        if secret is None and self.authenticated is False:
            print("Cryptor - encrypt: Not authenticated.")
            return None

        plain = plain.encode()
        salt = Random.new().read(8)
        secret = secret.encode() if secret else self.secret.encode()
        key_iv = self._bytes_to_key(secret, salt, 32 + 16)
        key = key_iv[:32]
        iv = key_iv[32:]
        aes = AES.new(key, AES.MODE_CBC, iv)
        encrypted = base64.b64encode(
            b"Salted__" + salt + aes.encrypt(self._pad(plain))
        ).decode()

        print(f"Cryptor - Encrypt: Encrypted text {encrypted[:8]}....")

        return encrypted

    def decrypt(self, encrypted: str) -> str:
        """Decrypts a given encrypted data.

        Parameters
        ----------
        encrypted : str
            The encrypted data.

        Returns
        -------
        str
            The decrypted data.
        """
        if not self.authenticated:
            raise ValueError("Cryptor not authenticated. Call authenticate() first.")

        print(f"[Cryptor] decrypt: encrypted length = {len(encrypted)}")

        try:
            encrypted_bytes = base64.b64decode(encrypted.encode())
            print(f"[Cryptor] decrypt: decoded length = {len(encrypted_bytes)}")
        except Exception as e:
            print(f"[Cryptor] decrypt: base64 decode error: {e}")
            raise

        assert encrypted_bytes[0:8] == b"Salted__"
        salt = encrypted_bytes[8:16]

        try:
            key_iv = self._bytes_to_key(self.secret.encode(), salt, 32 + 16)
            key = key_iv[:32]
            iv = key_iv[32:]
            aes = AES.new(key, AES.MODE_CBC, iv)
            decrypted = self._unpad(aes.decrypt(encrypted_bytes[16:]))
            print("Cryptor - Decrypt: Decrypted data.")
            return decrypted
        except Exception as e:
            print(f"[Cryptor] decrypt: decryption error: {e}")
            raise

        return decrypted

    def authenticate(self) -> bool:
        """Authenticate with a generated key so Lanis knows how to encrypt data.

        Returns
        -------
        bool
            If False the handshake failed, if True it isn't False.
        """
        print("[Cryptor] Starting authentication...")
        try:
            self.secret = self._generate_key()
            print("[Cryptor] Generated key")
        except Exception as e:
            print(f"[Cryptor] Error generating key: {e}")
            raise

        try:
            encrypted_key = self._encrypt_key(self._get_public_key())
            print("[Cryptor] Encrypted key")
        except Exception as e:
            print(f"[Cryptor] Error encrypting key: {e}")
            raise

        try:
            challenge = self._handshake(encrypted_key)
            print("[Cryptor] Got challenge")
        except Exception as e:
            print(f"[Cryptor] Error in handshake: {e}")
            raise

        self.authenticated = True

        try:
            if self._challenge(challenge):
                print("Cryptor - Authenticate: Successfully authenticated.")
                return True
        except Exception as e:
            print(f"[Cryptor] Error in challenge: {e}")
            self.authenticated = False
            raise

        self.authenticated = False

        print("Cryptor - Authenticate: Couldn't authenticate.")

        return False
