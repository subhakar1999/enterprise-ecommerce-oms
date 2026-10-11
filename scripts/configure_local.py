"""Prepare local configuration without replacing existing credentials."""
from pathlib import Path
import secrets


def main():
    root = Path(__file__).resolve().parent.parent
    target = root / ".env"
    text = target.read_text(encoding="utf-8") if target.exists() else (root / ".env.example").read_text(encoding="utf-8")
    lines = text.splitlines()
    replacements = {"ADMIN_PASSWORD": secrets.token_urlsafe(18), "ADMIN_SESSION_SECRET": secrets.token_urlsafe(48),
                    "PAYMENT_WEBHOOK_SECRET": secrets.token_urlsafe(32)}
    seen = set()
    for index, line in enumerate(lines):
        key, separator, value = line.partition("=")
        if separator and key in replacements:
            seen.add(key)
            if not value.strip() or value.startswith("replace-with-"):
                lines[index] = key + "=" + replacements[key]
    for key in replacements.keys() - seen:
        lines.append(key + "=" + replacements[key])
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Local .env is ready. Admin credentials are stored in .env; existing values were preserved.")
    print("Razorpay credentials are left for your own merchant account. Docker was not started.")


if __name__ == "__main__":
    main()
