import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def _password_parts(stored: str):
    parts = (stored or "").split("$")
    if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
        try:
            iterations = int(parts[1])
        except ValueError:
            return None
        return iterations, parts[2], parts[3]
    if len(parts) == 3 and parts[0] == "pbkdf2_sha256":
        return LEGACY_PBKDF2_ITERATIONS, parts[1], parts[2]
    return None


def verify_password(password: str, stored: str) -> bool:
    try:
        parsed = _password_parts(stored)
        if not parsed:
            return False
        iterations, salt, expected = parsed
        if iterations < 1 or iterations > 5_000_000:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
        ).hex()
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def password_needs_rehash(stored: str) -> bool:
    parsed = _password_parts(stored)
    return not parsed or parsed[0] < PBKDF2_ITERATIONS


def password_policy_error(password: str) -> str:
    value = password or ""
    if len(value) < 12:
        return "La contraseña debe tener al menos 12 caracteres."
    if len(value) > 256:
        return "La contraseña no puede exceder 256 caracteres."
    return ""
