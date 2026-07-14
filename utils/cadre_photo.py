def normalize_mid(mid: str | None) -> str:
    if not mid:
        return ""
    return str(mid).strip().replace("#", "")


def is_valid_membership_id(mid: str | None) -> bool:
    """Membership IDs are numeric (e.g. 15067518). Rejects Swagger placeholders like 'string'."""
    cleaned = normalize_mid(mid)
    return bool(cleaned) and cleaned.isdigit() and len(cleaned) >= 6


def filter_valid_mids(mids: list[str] | None) -> list[str]:
    if not mids:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for mid in mids:
        cleaned = normalize_mid(mid)
        if is_valid_membership_id(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def build_cadre_photo_url(image_path: str | None, base_url: str) -> str | None:
    if not image_path:
        return None
    path = str(image_path).strip().lstrip("/")
    if not path:
        return None
    base = base_url.rstrip("/")
    return f"{base}/{path}"


def build_document_list(documents: str | None, base_url: str) -> list[dict]:
    """Split a '$'-joined documents column into [{name, url}] entries.

    e.g. 'July-2024/33709231.pdf$October-2024/20870063.pdf' ->
        [{"name": "July-2024/33709231.pdf", "url": ".../nominated_post_documents/July-2024/33709231.pdf"}, ...]
    """
    if not documents:
        return []
    out: list[dict] = []
    for piece in str(documents).split("$"):
        name = piece.strip()
        if not name:
            continue
        out.append({"name": name, "url": build_cadre_photo_url(name, base_url)})
    return out
