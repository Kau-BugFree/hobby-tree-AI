"""Conservative title relevance independent of onboarding and budget."""

import re

from .catalog_domain import query_key


def material_matches(material_name: str, title: str) -> bool:
    """Require all name tokens in the title, without inventing semantic matches."""
    tokens = re.findall(r"[a-z0-9가-힣]+", query_key(material_name))
    compact_title = "".join(re.findall(r"[a-z0-9가-힣]+", query_key(title)))
    compact_material = "".join(tokens)
    # Search often returns an accessory for the requested item. Reject obvious
    # accessory titles unless that accessory itself is the DB material. This
    # intentionally sacrifices some bundles; it is not a compatibility model.
    accessories = (
        "가방",
        "보관함",
        "케이스",
        "커버",
        "거치대",
        "홀더",
        "파우치",
        "스트랩",
        "보호필름",
        "피크",
        "악보",
        "받침대",
        "세척액",
        "교체용",
        "리필용",
        "case",
        "cover",
        "holder",
        "strap",
    )
    if any(word in compact_title and word not in compact_material for word in accessories):
        return False
    return bool(tokens) and all(token in compact_title for token in tokens)
