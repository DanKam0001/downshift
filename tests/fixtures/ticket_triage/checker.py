import re

LABELS = {"billing", "shipping", "technical", "account", "refund"}


def check(output, reference, input):
    text = re.sub(r"[`*\"'.]", " ", str(output)).strip().lower()
    words = re.findall(r"[a-z]+", text)
    found = [w for w in words if w in LABELS]
    if not found:
        return 0.0, "no valid label"
    if len(set(found)) > 1:
        return 0.0, "more than one label"
    return (1.0, "match") if found[0] == str(reference).strip().lower() else (0.0, f"got {found[0]}")
