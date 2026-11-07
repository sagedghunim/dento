"""Small validators for request bodies (design 6.1)."""

from core.errors import ApiError


def text(data, name, max_len, required=True, strip=True):
    value = data.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError("invalid", f"{name} must be text", fields={name: "text"})
    if strip:
        value = " ".join(value.split()) if "\n" not in value else value.strip()
    if required and not value:
        raise ApiError("invalid", f"{name} is required", fields={name: "required"})
    if len(value) > max_len:
        raise ApiError("invalid", f"{name} is too long", fields={name: "too_long"})
    return value


def choice(data, name, options):
    value = data.get(name)
    if value not in options:
        raise ApiError("invalid", f"{name} must be one of: {', '.join(options)}", fields={name: "choice"})
    return value


def boolean(data, name, default=False):
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise ApiError("invalid", f"{name} must be true or false", fields={name: "boolean"})
    return value
