import hashlib, hmac, secrets

# PBKDF2 keeps the starter app dependency-light and portable.
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, expected = stored.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False
