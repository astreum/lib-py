import os
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

def encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """
    Encrypt data using the ChaCha20-Poly1305 AEAD construction.

    This function provides both confidentiality and integrity. The returned
    value contains the ciphertext with the Poly1305 authentication tag
    appended. The same key, nonce, and associated authenticated data (AAD)
    must be provided during decryption.

    Parameters
    ----------
    key : bytes
        A 32-byte (256-bit) secret key.
    nonce : bytes
        A 12-byte nonce. Must be unique per key. Nonce reuse with the same key
        breaks security.
    plaintext : bytes
        The data to be encrypted.
    aad : bytes, optional
        Associated authenticated data that is not encrypted but is included
        in authentication (e.g. headers or metadata). Defaults to empty.

    Returns
    -------
    bytes
        The encrypted output consisting of ciphertext followed by the
        authentication tag.

    Raises
    ------
    ValueError
        If the key or nonce length is invalid.
    """
    aead = ChaCha20Poly1305(key)
    return aead.encrypt(nonce, plaintext, aad)


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
    """
    Decrypt data encrypted with ChaCha20-Poly1305 and verify its authenticity.

    This function verifies the Poly1305 authentication tag before returning
    the plaintext. If the ciphertext, nonce, key, or associated authenticated
    data (AAD) has been altered, decryption fails.

    Parameters
    ----------
    key : bytes
        The same 32-byte (256-bit) secret key used for encryption.
    nonce : bytes
        The same 12-byte nonce used for encryption.
    ciphertext : bytes
        The encrypted data including the appended authentication tag.
    aad : bytes, optional
        The associated authenticated data used during encryption. Must match
        exactly. Defaults to empty.

    Returns
    -------
    bytes
        The original decrypted plaintext.

    Raises
    ------
    cryptography.exceptions.InvalidTag
        If authentication fails due to tampering or incorrect inputs.
    ValueError
        If the key or nonce length is invalid.
    """
    aead = ChaCha20Poly1305(key)
    return aead.decrypt(nonce, ciphertext, aad)
