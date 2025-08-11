import re
import numpy as np
import pandas as pd
from numpy.linalg import norm
from rapidfuzz import fuzz
from spellchecker import SpellChecker

from .models import Expense, Invoice
from .embedding_loader import get_glove, get_fasttext

# --- Abbreviations & Synonyms ---
spell = SpellChecker()
ABBR = {
    'inv': 'invoice', 'exp': 'expense', 'amt': 'amount', 'num': 'number', 'id': 'id',
    'dt': 'date', 'expdt': 'expense_date', 'exp_dt': 'expense_date',
    'paydt': 'payable_date', 'pay_dt': 'payable_date', 'paid': 'date_paid',
    'desc': 'description', 'subj': 'subject', 'typ': 'type', 'arch': 'is_archive',
    'att': 'attachment', 'file': 'attachment', 'liqamt': 'liquidated_amount',
    'liqdt': 'liquidated_date', 'paidon': 'date_paid', 'received': 'received_amount',
    'retdt': 'return_date', 'ret': 'return_date', 'retd': 'return_date',
    'retdt.': 'return_date', 'retdt ': 'return_date', 'retdate': 'return_date',
    'ret_dt': 'return_date', 'ret dt': 'return_date'
}
SYNONYMS = {'cost': 'amount', 'total': 'amount'}

RAW_REMAP = {
    'paid on': 'date_paid',
    'ret date': 'return_date',
    'ret dt': 'return_date',
    'retdt': 'return_date',
    'retdt.': 'return_date',
    'retdt ': 'return_date',
    'retd.': 'return_date',
    'retd': 'return_date',
    'ret_dt': 'return_date',
    'retdate': 'return_date',
    'received': 'received_amount'
}

SPECIAL_REMAP = {
    'paid on': 'date_paid',
    'ret dt': 'return_date',
    'returndate': 'return_date',
    'ret date': 'return_date',
    'retdt.': 'return_date',
    'retd': 'return_date',
    'retdt ': 'return_date',
    'retd.': 'return_date',
    'retdate': 'return_date',
    'ret_dt': 'return_date'
}

def preprocess(header: str) -> str:
    raw = str(header).strip().lower()
    if raw in RAW_REMAP:
        return RAW_REMAP[raw]

    h = re.sub(r"[^0-9a-z]+", " ", raw)
    for abbr, full in sorted(ABBR.items(), key=lambda kv: -len(kv[0])):
        h = re.sub(rf"\b{re.escape(abbr)}\b", full, h)
    tokens = [tok for tok in h.split() if tok]
    processed = []
    for tok in tokens:
        corr = spell.correction(tok) or tok
        processed.append(SYNONYMS.get(corr, corr))
    joined = ' '.join(processed)
    if joined in SPECIAL_REMAP:
        return SPECIAL_REMAP[joined]
    return joined

# --- Schema for Expense & Invoice ---
SCHEMA = {
    m._meta.db_table: [
        f.name for f in m._meta.get_fields() if hasattr(f, 'column') and f.column
    ] for m in (Expense, Invoice)
}

FIELD_EMBS = {}
COLUMN_SIGS = {}
_embeddings_built = False

def build_field_embeddings():
    global FIELD_EMBS, COLUMN_SIGS, _embeddings_built
    if _embeddings_built:
        return
    glove = get_glove()
    fasttext = get_fasttext()
    for model in (Expense, Invoice):
        tbl = model._meta.db_table
        FIELD_EMBS[tbl] = {}
        for fld in SCHEMA[tbl]:
            pfld = preprocess(fld)
            words = pfld.split()
            gvecs = [glove.get(w) for w in words if glove.get(w) is not None]
            fvecs = [fasttext.get(w) for w in words if fasttext.get(w) is not None]
            FIELD_EMBS[tbl][fld] = {
                'glove': np.mean(gvecs, axis=0) if gvecs else np.zeros(200),
                'fasttext': np.mean(fvecs, axis=0) if fvecs else np.zeros(300),
                'pre': pfld
            }
        sigs = {}
        for fld in SCHEMA[tbl]:
            vals = list(
                model.objects.exclude(**{f"{fld}__isnull": True})
                     .values_list(fld, flat=True)[:20]
            )
            samples = [str(v) for v in vals if isinstance(v, str)][:5]
            sample_vecs = []
            for s in samples:
                tokens = preprocess(s).split()
                subvecs = [fasttext.get(w) for w in tokens if fasttext.get(w) is not None]
                if subvecs:
                    avg = np.mean(subvecs, axis=0)
                    sample_vecs.append(avg)
            sigs[fld] = {'sem': np.mean(sample_vecs, axis=0) if sample_vecs else np.zeros(300)}
        COLUMN_SIGS[tbl] = sigs
    _embeddings_built = True

def cosine_sim(a, b):
    a_arr = np.asarray(a, dtype=float).ravel()
    b_arr = np.asarray(b, dtype=float).ravel()
    if a_arr.shape != b_arr.shape or a_arr.size == 0:
        return 0.0
    na = norm(a_arr); nb = norm(b_arr)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / (na * nb))

def clean_and_detect(df: pd.DataFrame, threshold: float = 0.5):
    build_field_embeddings()
    glove = get_glove()
    fasttext = get_fasttext()
    raw_cols = list(df.columns)

    table_scores = {}
    for tbl, emb_map in FIELD_EMBS.items():
        sims = []
        for col in raw_cols:
            proc = preprocess(col)
            gvecs = [glove.get(w) for w in proc.split() if glove.get(w) is not None]
            fvecs = [fasttext.get(w) for w in proc.split() if fasttext.get(w) is not None]
            gvec = np.mean(gvecs, axis=0) if gvecs else np.zeros(200)
            fvec = np.mean(fvecs, axis=0) if fvecs else np.zeros(300)
            for fe in emb_map.values():
                sims.append(cosine_sim(gvec, fe['glove']) + cosine_sim(fvec, fe['fasttext']))
        table_scores[tbl] = float(np.nanmean(sims)) if sims else 0.0

    detected = max(table_scores, key=table_scores.get)
    print("Detected Table:", detected)
    mapping_log = []
    mapping = {}

    for col in raw_cols:
        proc = preprocess(col)
        gvecs = [glove.get(w) for w in proc.split() if glove.get(w) is not None]
        fvecs = [fasttext.get(w) for w in proc.split() if fasttext.get(w) is not None]
        gvec = np.mean(gvecs, axis=0) if gvecs else np.zeros(200)
        fvec = np.mean(fvecs, axis=0) if fvecs else np.zeros(300)

        direct = next((fld for fld, fe in FIELD_EMBS[detected].items() if proc == fe['pre'] or proc == fld), None)
        if direct:
            mapping[col] = direct
            mapping_log.append((col, direct, 1.0))
            continue

        best_score, best_field = -1.0, col
        for fld, fe in FIELD_EMBS[detected].items():
            sem_score = cosine_sim(gvec, fe['glove']) + cosine_sim(fvec, fe['fasttext'])
            fuzz_score = fuzz.token_sort_ratio(proc, fe['pre']) / 100.0
            sig_score = cosine_sim(fvec, COLUMN_SIGS[detected][fld]['sem'])
            score = 0.5 * sem_score + 0.3 * fuzz_score + 0.2 * sig_score
            if score > best_score:
                best_score, best_field = score, fld

        if best_score >= threshold:
            mapping[col] = best_field
            mapping_log.append((col, best_field, round(best_score, 2)))
        else:
            mapping[col] = col

    print("\nHeader Mapping Log:")
    for old, new, score in mapping_log:
        print(f" - {old} → {new} ({score})")

    cleaned_df = df.rename(columns=mapping)
    return detected, cleaned_df, mapping_log
