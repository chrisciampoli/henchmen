"""User registration service.

Currently accepts any input without validation. The agent should add
validation for username length/format and age range.
"""

from __future__ import annotations


def register_user(username: str, age: int) -> dict[str, object]:
    """Register a new user and return their profile dict.

    Currently performs NO validation. The task is to add:
    - username must be 3-20 alphanumeric characters
    - age must be between 0 and 150
    Raise ValueError on invalid input.
    """
    return {
        "username": username,
        "age": age,
        "active": True,
    }
