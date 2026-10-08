"""Cryptographic login passwords with fewer visually ambiguous characters."""

import secrets


# Omit lookalike letters/numbers and all punctuation for manual entry. This is
# only for generated login passwords, never encryption keys or access tokens.
PASSWORD_ALPHABET = "ACDEFHJKMNPRTUVWXYacdefhjkmnprtuvwxy347"
PASSWORD_LENGTH = 40


def generate_login_password() -> str:
    """Return a uniform password with upper/lowercase and a digit (~211 bits).

    Reject whole candidates instead of replacing characters or forcing fixed
    positions. Every accepted password remains equally likely, and requiring
    all three categories keeps the result compatible with account policies.
    """
    while True:
        password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))
        if any(character.isupper() for character in password) and any(
            character.islower() for character in password
        ) and any(character.isdigit() for character in password):
            return password
