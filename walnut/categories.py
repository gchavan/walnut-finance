"""Payee clustering and category tags.

SimpleFIN has no categories. Walnut groups similar cash/card payees and lets
you assign a tag. Optional seed rules cover common national merchants. Rules
apply on sync only when category_name is empty/Uncategorized.

Investment / Robinhood activity is never auto-tagged as household spend.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3

from . import db

logger = logging.getLogger("walnut.categories")

DEFAULT_TAGS = (
    "Dining & Drinks",
    "Groceries",
    "Auto & Transport",
    "Shopping",
    "Entertainment & Rec.",
    "Health & Wellness",
    "Personal Care",
    "Bills & Utilities",
    "Travel & Vacation",
    "Family Care",
    "Home & Garden",
    "Medical",
    "Software & Tech",
    "Education",
    "Kids",
    "Charitable Donations",
    "Fees",
    "Income",
    "Transfer (not spend)",
    "Uncategorized",
)

# Substring (normalized) → tag. Applied on first boot if category_rules is empty.
SEED_RULES: tuple[tuple[str, str], ...] = (
    ("DOORDASH", "Dining & Drinks"),
    ("CHIPOTLE", "Dining & Drinks"),
    ("DOMINO", "Dining & Drinks"),
    ("JIMMY JOHN", "Dining & Drinks"),
    ("STARBUCKS", "Dining & Drinks"),
    ("MCDONALD", "Dining & Drinks"),
    ("INSTACART", "Groceries"),
    ("COSTCO", "Groceries"),
    ("WHOLE FOODS", "Groceries"),
    ("WHOLEFD", "Groceries"),
    ("WALMART", "Groceries"),
    ("TRADER JOE", "Groceries"),
    ("UBER *TRIP", "Auto & Transport"),
    ("LYFT", "Auto & Transport"),
    ("AMAZON", "Shopping"),
    ("TARGET", "Shopping"),
    ("APPLE.COM", "Entertainment & Rec."),
    ("NETFLIX", "Entertainment & Rec."),
    ("YOUTUBE", "Entertainment & Rec."),
    ("SPOTIFY", "Entertainment & Rec."),
    ("HOME DEPOT", "Home & Garden"),
    ("GOOGLE ONE", "Software & Tech"),
    ("OPENAI", "Software & Tech"),
    ("CHATGPT", "Software & Tech"),
    ("SPECTRUM", "Bills & Utilities"),
)

_PROCESSOR_PREFIX = re.compile(
    r"^(?:SQ\s*\*|TST\*|PP\*|DD\s*\*|IC\*|FIV\*)\s*",
    re.IGNORECASE,
)

# Longer first so "CEDAR PARK" wins over leftover tokens.
_TRAILING_CITIES = (
    "SAN FRANCISCO", "LOS ANGELES", "SAN ANTONIO", "FORT WORTH",
    "NEW YORK", "SAN JOSE", "AUSTIN", "HOUSTON", "DALLAS",
    "ATLANTA", "MIAMI", "SEATTLE", "DENVER", "PHOENIX", "CHICAGO",
)

_EMPTY_CATS = frozenset(("", "uncategorized", "uncategorised"))


def normalize_payee(payee: str | None) -> str:
    """Uppercase; strip processor prefixes, store numbers, trailing city noise."""
    s = (payee or "").upper().replace("\u00a0", " ")
    s = re.sub(r"UBER\s*\*", "UBER ", s)
    s = _PROCESSOR_PREFIX.sub("", s)
    s = re.sub(r"\.COM\b", " ", s)
    s = re.sub(r"\.ORG\b", " ", s)
    s = re.sub(r"#\d+", " ", s)
    s = re.sub(r"\b\d{3,5}\b", " ", s)
    s = re.sub(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", " ", s)
    s = s.replace("-", "")
    s = s.replace("+", " ").replace("&", " ").replace("*", " ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+[A-Z]{2}$", "", s).strip()
    changed = True
    while changed and s:
        changed = False
        for city in _TRAILING_CITIES:
            if s == city:
                return ""
            suffix = " " + city
            if s.endswith(suffix):
                s = s[: -len(suffix)].strip()
                changed = True
                break
    return s


def _cat_empty(name: str | None) -> bool:
    return (name or "").strip().lower() in _EMPTY_CATS


def _rule_id(pattern: str) -> str:
    return hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:20]


def upsert_rule(
    conn: sqlite3.Connection,
    pattern: str,
    category: str,
    source: str,
) -> str:
    key = normalize_payee(pattern) or (pattern or "").strip().upper()
    if not key:
        raise ValueError("pattern is empty after normalize")
    rid = _rule_id(key)
    conn.execute(
        """
        INSERT INTO category_rules (rule_id, pattern, category, source, updated_at_ms)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(rule_id) DO UPDATE SET
            pattern = excluded.pattern,
            category = excluded.category,
            source = excluded.source,
            updated_at_ms = excluded.updated_at_ms
        """,
        (rid, key, category, source, db.now_ms()),
    )
    return rid


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    return db.rows(conn.execute(
        "SELECT * FROM category_rules ORDER BY length(pattern) DESC, pattern"
    ))


def ensure_seeded(conn: sqlite3.Connection) -> int:
    """Insert seed rules if the table is empty. Returns number inserted."""
    n = int(conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0])
    if n:
        return 0
    added = 0
    for pattern, category in SEED_RULES:
        upsert_rule(conn, pattern, category, "seed")
        added += 1
    logger.info("seeded %s category rules", added)
    return added


def match_category(payee: str | None, rules: list[dict]) -> str | None:
    key = normalize_payee(payee)
    if not key:
        return None
    for rule in rules:
        pat = rule.get("pattern") or ""
        if not pat:
            continue
        if pat == key or pat in key:
            return rule.get("category")
    return None


def apply_to_uncategorized(conn: sqlite3.Connection) -> int:
    """Set category_name from rules when empty/Uncategorized.

    Cash/cards only, including pending. Skip investment / Robinhood accounts.
    Pending rows are not spend, but they still get a tag so they are not
    Uncategorized on Transactions when they post.
    """
    rules = list_rules(conn)
    if not rules:
        return 0
    rows = conn.execute(
        """
        SELECT t.transaction_id, t.payee, t.category_name,
               a.name AS account_name, a.group_key AS group_key
        FROM transactions t
        LEFT JOIN accounts a ON a.account_id = t.account_id
        WHERE t.amount_cents > 0 AND t.is_transfer = 0
          AND (t.category_name IS NULL OR t.category_name = ''
               OR t.category_name = 'Uncategorized')
        """
    ).fetchall()
    n = 0
    for row in rows:
        if db.account_is_investment_spend(row["account_name"], row["group_key"]):
            continue
        cat = match_category(row["payee"], rules)
        if not cat or cat == "Uncategorized":
            continue
        conn.execute(
            "UPDATE transactions SET category_name = ? WHERE transaction_id = ?",
            (cat, row["transaction_id"]),
        )
        n += 1
    if n:
        logger.info("applied category rules to %s transactions", n)
    return n


def apply_category_to_new(payee: str | None, existing: str | None, rules: list[dict]) -> str | None:
    """Keep a user-set category; otherwise the matching rule (or None)."""
    if not _cat_empty(existing):
        return existing
    return match_category(payee, rules)


def _tags_payload(conn: sqlite3.Connection) -> list[str]:
    seen = list(DEFAULT_TAGS)
    have = {t.lower() for t in seen}
    extras: list[str] = []
    for row in conn.execute(
        "SELECT DISTINCT category FROM category_rules WHERE category IS NOT NULL AND category != ''"
    ):
        name = row[0]
        if name.lower() not in have:
            extras.append(name)
            have.add(name.lower())
    for row in conn.execute(
        "SELECT DISTINCT category_name FROM transactions "
        "WHERE category_name IS NOT NULL AND category_name != ''"
    ):
        name = row[0]
        if name.lower() not in have:
            extras.append(name)
            have.add(name.lower())
    extras.sort(key=str.lower)
    return seen + extras


def clusters(conn: sqlite3.Connection, *, uncategorized_only: bool = True) -> list[dict]:
    """Group spending payees by normalized key. Sorted by n then cents."""
    rows = conn.execute(
        """
        SELECT t.payee, t.amount_cents, t.category_name, t.is_spending
        FROM transactions t
        WHERE t.is_spending = 1 AND t.pending = 0 AND t.amount_cents > 0
        """
    ).fetchall()
    rules = list_rules(conn)
    groups: dict[str, dict] = {}
    for row in rows:
        if uncategorized_only and not _cat_empty(row["category_name"]):
            continue
        key = normalize_payee(row["payee"]) or "UNKNOWN"
        g = groups.get(key)
        if g is None:
            g = {
                "key": key,
                "payees": [],
                "payee_set": set(),
                "n": 0,
                "cents": 0,
                "cats": set(),
            }
            groups[key] = g
        original = (row["payee"] or "").strip() or "—"
        if original not in g["payee_set"] and len(g["payees"]) < 8:
            g["payees"].append(original)
            g["payee_set"].add(original)
        g["n"] += 1
        g["cents"] += int(row["amount_cents"] or 0)
        cat = (row["category_name"] or "").strip()
        g["cats"].add(cat if cat else "Uncategorized")
    out = []
    for key, g in groups.items():
        cats = g["cats"]
        current = next(iter(cats)) if len(cats) == 1 else None
        suggested = match_category(key, rules)
        out.append({
            "key": key,
            "payees": g["payees"],
            "n": g["n"],
            "cents": g["cents"],
            "suggested": suggested,
            "current": current,
        })
    out.sort(key=lambda c: (-c["n"], -c["cents"], c["key"]))
    return out


def apply_to_matching(
    conn: sqlite3.Connection,
    *,
    category: str,
    cluster_key: str | None = None,
    pattern: str | None = None,
) -> dict:
    """Write category_name on matching spending txns and upsert a user rule."""
    category = (category or "").strip()
    if not category:
        raise ValueError("category is required")
    if len(category) > 80:
        raise ValueError("category is too long")
    key = (cluster_key or "").strip()
    pat_raw = (pattern or "").strip()
    if not key and not pat_raw:
        raise ValueError("pattern or cluster_key is required")
    match_pat = normalize_payee(key or pat_raw)
    if not match_pat:
        raise ValueError("pattern is empty after normalize")
    exact = bool(key)
    rows = conn.execute(
        """
        SELECT t.transaction_id, t.payee, t.is_spending, a.name AS account_name,
               a.group_key AS group_key
        FROM transactions t
        LEFT JOIN accounts a ON a.account_id = t.account_id
        """
    ).fetchall()
    n = 0
    for row in rows:
        if db.account_is_investment_spend(row["account_name"], row["group_key"]):
            continue
        nk = normalize_payee(row["payee"])
        if exact:
            hit = nk == match_pat
        else:
            hit = bool(nk) and (match_pat == nk or match_pat in nk)
        if not hit:
            continue
        conn.execute(
            "UPDATE transactions SET category_name = ? WHERE transaction_id = ?",
            (category, row["transaction_id"]),
        )
        n += 1
    rid = upsert_rule(conn, match_pat, category, "user")
    extra = apply_to_uncategorized(conn)
    if category.strip().lower() == "transfer (not spend)":
        db.recompute_spending(conn)
    return {"ok": True, "n": n, "n_rules_applied": extra, "rule_id": rid, "category": category, "pattern": match_pat}


def tag_one(conn: sqlite3.Connection, transaction_id: str, category: str) -> dict:
    category = (category or "").strip()
    if not category:
        raise ValueError("category is required")
    if len(category) > 80:
        raise ValueError("category is too long")
    tid = (transaction_id or "").strip()
    if not tid:
        raise ValueError("transaction_id is required")
    row = conn.execute(
        "SELECT transaction_id FROM transactions WHERE transaction_id = ?",
        (tid,),
    ).fetchone()
    if not row:
        raise KeyError("transaction not found")
    conn.execute(
        "UPDATE transactions SET category_name = ? WHERE transaction_id = ?",
        (category, tid),
    )
    if category.lower() == "transfer (not spend)":
        db.recompute_spending(conn)
    return {"ok": True, "transaction_id": tid, "category": category}


def payload(conn: sqlite3.Connection, *, uncategorized_only: bool = True) -> dict:
    return {
        "tags": _tags_payload(conn),
        "clusters": clusters(conn, uncategorized_only=uncategorized_only),
        "rules": list_rules(conn),
        "suggestions": visible_suggestions(conn),
        "uncategorized_only": uncategorized_only,
    }

# ---------------------------------------------------------------------------
# Rule-consolidation suggestions (user must Apply each one)
# ---------------------------------------------------------------------------

_TOO_BROAD = frozenset({"GOOGLE"})
_LEGAL_SUFFIX = frozenset({"LLC", "INC", "CORP", "LTD", "NA", "US", "CO", "LP", "PLC"})
_TRAILING_DIGITS = re.compile(r"^(.*[A-Z])(\d{6,})$")
_MIN_STEM = 8
_MIN_SIBLING_PREFIX = 5


def delete_rule(
    conn: sqlite3.Connection,
    rule_id: str | None = None,
    pattern: str | None = None,
) -> int:
    """Delete a category rule by id or pattern. Returns rows deleted."""
    if rule_id:
        cur = conn.execute("DELETE FROM category_rules WHERE rule_id = ?", (rule_id,))
        return int(cur.rowcount or 0)
    key = normalize_payee(pattern) or (pattern or "").strip().upper()
    if not key:
        raise ValueError("rule_id or pattern is required")
    rid = _rule_id(key)
    cur = conn.execute(
        "DELETE FROM category_rules WHERE rule_id = ? OR pattern = ?",
        (rid, key),
    )
    return int(cur.rowcount or 0)


def update_rule(
    conn: sqlite3.Connection,
    rule_id: str,
    *,
    category: str | None = None,
    pattern: str | None = None,
) -> dict:
    """Change a rule's category and/or pattern in place, then retag matches."""
    rid = (rule_id or "").strip()
    if not rid:
        raise ValueError("rule_id is required")
    row = conn.execute(
        "SELECT rule_id, pattern, category, source FROM category_rules WHERE rule_id = ?",
        (rid,),
    ).fetchone()
    if not row:
        raise KeyError("rule not found")
    new_cat = (category if category is not None else row["category"] or "").strip()
    if not new_cat:
        raise ValueError("category is required")
    if len(new_cat) > 80:
        raise ValueError("category is too long")
    raw = pattern if pattern is not None else row["pattern"]
    new_key = normalize_payee(raw) or (str(raw or "").strip().upper())
    if not new_key:
        raise ValueError("pattern is empty after normalize")
    old_key = row["pattern"] or ""
    if new_key != old_key:
        delete_rule(conn, rule_id=rid)
    return apply_to_matching(conn, category=new_cat, pattern=new_key)


def _cash_card_txns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT t.payee, t.category_name, a.name AS account_name,
               a.group_key AS group_key
        FROM transactions t
        LEFT JOIN accounts a ON a.account_id = t.account_id
        """
    ).fetchall()
    out = []
    for row in rows:
        if db.account_is_investment_spend(row["account_name"], row["group_key"]):
            continue
        out.append(row)
    return out


def _rule_hits(pattern: str, txns: list) -> list:
    return [t for t in txns if match_category(t["payee"], [{"pattern": pattern, "category": "_"}])]


def _cat_of(name: str | None) -> str:
    s = (name or "").strip()
    return "" if _cat_empty(s) else s


def _too_broad(pattern: str, category: str, txns: list) -> bool:
    key = (pattern or "").strip().upper()
    if not key or key in _TOO_BROAD:
        return True
    if " " not in key and len(key) < 6:
        return True
    foreign = set()
    for t in _rule_hits(key, txns):
        cat = _cat_of(t["category_name"])
        if cat and cat.lower() != (category or "").strip().lower():
            foreign.add(cat)
    return bool(foreign)


_IGNORED_SUG_META = "ignored_rule_suggestions"


def ignored_suggestion_ids(conn: sqlite3.Connection) -> set[str]:
    raw = db.get_meta(conn, _IGNORED_SUG_META) or "[]"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x) for x in data if x}


def ignore_suggestion(conn: sqlite3.Connection, suggestion_id: str) -> dict:
    sid = (suggestion_id or "").strip()
    if not sid:
        raise ValueError("id is required")
    ids = ignored_suggestion_ids(conn)
    ids.add(sid)
    db.set_meta(conn, _IGNORED_SUG_META, json.dumps(sorted(ids)))
    return {"ok": True, "id": sid}


def visible_suggestions(conn: sqlite3.Connection) -> list[dict]:
    ignored = ignored_suggestion_ids(conn)
    return [s for s in suggest_rules(conn) if s.get("id") not in ignored]


def _sug_id(fields: dict) -> str:
    blob = repr((
        fields.get("action"),
        fields.get("pattern"),
        fields.get("new_pattern"),
        fields.get("category"),
        tuple(fields.get("drop_patterns") or ()),
    )).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _num_word(n: int) -> str:
    words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}
    return words.get(n, str(n))


def _common_first_token(a: str, b: str) -> str:
    ta = (a or "").split()
    tb = (b or "").split()
    if not ta or not tb:
        return ""
    return ta[0] if ta[0] == tb[0] else ""


def _broaden_candidate(pattern: str) -> str | None:
    """Shorter pattern if the current one ends in a one-off code or store letter."""
    toks = (pattern or "").split()
    if not toks:
        return None
    last = toks[-1]
    m = _TRAILING_DIGITS.match(last)
    if m:
        head = toks[:-1] + [m.group(1)]
        cand = " ".join(t for t in head if t).strip()
        return cand or None
    if (
        len(toks) >= 2
        and last not in _LEGAL_SUFFIX
        and 4 <= len(last) <= 10
        and re.search(r"\d", last)
        and re.search(r"[A-Z]", last)
    ):
        return " ".join(toks[:-1]).strip() or None
    if len(toks) == 2 and len(last) == 1 and last.isalpha() and len(toks[0]) >= 6:
        return toks[0]
    return None


def _reason_merge(drop_patterns: list[str], new_pattern: str) -> str:
    joined = " ".join(drop_patterns).upper()
    if "COOP" in joined or "ELECT" in joined:
        return "Same co-op, two spellings."
    return f"Same merchant, two spellings — keep {new_pattern}."


def _reason_drop(pattern: str, kept: str | None, txns: list) -> str:
    samples: list[str] = []
    if kept:
        for t in _rule_hits(kept, txns):
            raw = (t["payee"] or "").upper()
            tok = re.split(r"[\s#*]+", raw)[0]
            if tok and tok not in samples:
                samples.append(tok)
            if len(samples) >= 2:
                break
    hint = samples[0] if samples else kept
    if hint:
        return f"Feed payees are {hint}…, none match {pattern}."
    return f"No cash/card payees match {pattern}."


def _reason_broaden(pattern: str, new_pattern: str, extras: list, covers: list) -> str:
    last = (pattern or "").split()[-1] if pattern else ""
    if last and _TRAILING_DIGITS.match(last) and extras:
        n = len(extras)
        noun = "charge" if n == 1 else "charges"
        return f"{_num_word(n)} later {noun} are still untagged."
    extra_refs: list[str] = []
    for t in extras:
        raw = (t["payee"] or "").replace("*", " ")
        tok = (raw.split() or [""])[-1]
        if tok and tok not in extra_refs:
            extra_refs.append(tok)
        if len(extra_refs) >= 3:
            break
    if extra_refs:
        label = (new_pattern.split() or ["merchant"])[-1]
        pretty = label.title() if label.isalpha() else label
        return f"Other {pretty} refs ({', '.join(extra_refs)}) would match."
    n = len(extras)
    if n:
        noun = "charge" if n == 1 else "charges"
        return f"{_num_word(n)} later {noun} are still untagged."
    names: list[str] = []
    for t in covers:
        p = (t["payee"] or "").strip()
        if p and p not in names:
            names.append(p)
        if len(names) >= 2:
            break
    if len(names) >= 2:
        return f"Covers {names[0]} and {names[1]}."
    if names:
        return f"Covers {names[0]}."
    return f"Shorter pattern {new_pattern} covers store-number payees."


def _pack(action: str, *, pattern: str, new_pattern: str | None, category: str,
          drop_patterns: list[str], reason: str, n_extra: int) -> dict:
    fields = {
        "action": action,
        "pattern": pattern,
        "new_pattern": new_pattern,
        "category": category,
        "drop_patterns": drop_patterns,
        "reason": reason,
        "n_extra": n_extra,
    }
    fields["id"] = _sug_id(fields)
    return fields


def suggest_rules(conn: sqlite3.Connection) -> list[dict]:
    """Conservative consolidations. Never merges different categories."""
    rules = list_rules(conn)
    txns = _cash_card_txns(conn)
    used: set[str] = set()
    out: list[dict] = []

    by_cat: dict[str, list[dict]] = {}
    for r in rules:
        by_cat.setdefault(r["category"], []).append(r)

    # 1. Same category, 2+ rules share a long first token → one shorter pattern.
    for cat, group in by_cat.items():
        buckets: dict[str, list[str]] = {}
        for r in group:
            tok = (r["pattern"] or "").split()[:1]
            if not tok:
                continue
            stem = tok[0]
            if len(stem) < _MIN_STEM:
                continue
            buckets.setdefault(stem, []).append(r["pattern"])
        for stem, pats in buckets.items():
            uniq = sorted(set(pats), key=lambda p: (-len(p), p))
            if len(uniq) < 2:
                continue
            if stem in _TOO_BROAD or _too_broad(stem, cat, txns):
                continue
            if any(p in used for p in uniq):
                continue
            extras = [
                t for t in _rule_hits(stem, txns)
                if not _cat_of(t["category_name"])
                and not any(match_category(t["payee"], [{"pattern": p, "category": cat}]) for p in uniq)
            ]
            drop = [p for p in uniq if p != stem]
            for p in uniq:
                used.add(p)
            used.add(stem)
            out.append(_pack(
                "merge",
                pattern=stem,
                new_pattern=stem,
                category=cat,
                drop_patterns=drop,
                reason=_reason_merge(drop, stem),
                n_extra=len(extras),
            ))

    # 2. Same category, one pattern is a substring of another → drop the longer.
    for cat, group in by_cat.items():
        pats = [r["pattern"] for r in group]
        for longer in sorted(pats, key=len, reverse=True):
            if longer in used:
                continue
            shorter = None
            for other in pats:
                if other == longer or len(other) >= len(longer):
                    continue
                if other in longer:
                    shorter = other
                    break
            if not shorter or shorter in used:
                continue
            used.add(longer)
            extras = [
                t for t in _rule_hits(shorter, txns)
                if not _cat_of(t["category_name"])
                and not match_category(t["payee"], [{"pattern": longer, "category": cat}])
            ]
            out.append(_pack(
                "drop",
                pattern=longer,
                new_pattern=shorter,
                category=cat,
                drop_patterns=[longer],
                reason=f"{shorter} already covers {longer}.",
                n_extra=len(extras),
            ))

    # 3. Strip a trailing ticket / account / store-letter suffix.
    for r in rules:
        pat = r["pattern"]
        if pat in used:
            continue
        cand = _broaden_candidate(pat)
        if not cand or cand == pat:
            continue
        if cand in _TOO_BROAD or _too_broad(cand, r["category"], txns):
            continue
        other = next((x for x in rules if x["pattern"] == cand), None)
        if other and other["category"] != r["category"]:
            continue
        covers = _rule_hits(cand, txns)
        extras = [
            t for t in covers
            if not _cat_of(t["category_name"])
            and not match_category(t["payee"], [{"pattern": pat, "category": r["category"]}])
        ]
        used.add(pat)
        used.add(cand)
        drop = [pat]
        if other and other["category"] == r["category"] and cand != pat:
            # keep existing shorter, just drop this one
            action = "drop"
            reason = f"{cand} already covers {pat}."
        else:
            action = "broaden"
            reason = _reason_broaden(pat, cand, extras, covers)
        out.append(_pack(
            action,
            pattern=pat,
            new_pattern=cand,
            category=r["category"],
            drop_patterns=drop,
            reason=reason,
            n_extra=len(extras),
        ))

    # 4. Unused rule with a same-category sibling that actually matches.
    match_n: dict[str, int] = {}
    for r in rules:
        match_n[r["pattern"]] = len(_rule_hits(r["pattern"], txns))
    for r in rules:
        pat = r["pattern"]
        if pat in used or match_n.get(pat, 0) > 0:
            continue
        sibling = None
        for other in by_cat.get(r["category"], []):
            op = other["pattern"]
            if op == pat or match_n.get(op, 0) <= 0:
                continue
            if pat[:_MIN_SIBLING_PREFIX] == op[:_MIN_SIBLING_PREFIX] and len(pat) >= _MIN_SIBLING_PREFIX:
                sibling = op
                break
        if not sibling:
            continue
        used.add(pat)
        out.append(_pack(
            "drop",
            pattern=pat,
            new_pattern=sibling,
            category=r["category"],
            drop_patterns=[pat],
            reason=_reason_drop(pat, sibling, txns),
            n_extra=0,
        ))

    return out


def apply_suggestion(
    conn: sqlite3.Connection,
    *,
    suggestion_id: str | None = None,
    action: str | None = None,
    pattern: str | None = None,
    new_pattern: str | None = None,
    category: str | None = None,
    drop_patterns: list | None = None,
) -> dict:
    """Apply one suggestion: upsert kept pattern, delete dropped rows, retag."""
    suggestions = suggest_rules(conn)
    sug = None
    if suggestion_id:
        sug = next((s for s in suggestions if s["id"] == suggestion_id), None)
    if sug is None:
        drops = drop_patterns or []
        if isinstance(drops, str):
            drops = [drops]
        for s in suggestions:
            if action and s["action"] != action:
                continue
            if new_pattern and s.get("new_pattern") != new_pattern:
                continue
            if pattern and s.get("pattern") != pattern and pattern not in (s.get("drop_patterns") or []):
                continue
            if drops and set(drops) != set(s.get("drop_patterns") or []):
                continue
            if category and s.get("category") != category:
                continue
            sug = s
            break
    if sug is None:
        raise KeyError("suggestion not found")

    kept = (sug.get("new_pattern") or "").strip()
    cat = (sug.get("category") or category or "").strip()
    if kept and cat:
        if kept in _TOO_BROAD:
            raise ValueError("pattern is too broad")
        upsert_rule(conn, kept, cat, "user")
    n_deleted = 0
    for pat in sug.get("drop_patterns") or []:
        if kept and normalize_payee(pat) == normalize_payee(kept):
            continue
        n_deleted += delete_rule(conn, pattern=pat)
    n_updated = apply_to_uncategorized(conn)
    return {
        "ok": True,
        "n_updated": n_updated,
        "n_deleted": n_deleted,
        "suggestion": sug,
    }
