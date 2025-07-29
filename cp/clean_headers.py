# clean_headers.py
import re
from pathlib import Path

import numpy as np
import pandas as pd
from gensim.models import KeyedVectors
from numpy.linalg import norm
from rapidfuzz import fuzz
from spellchecker import SpellChecker

from .models import Expense, Invoice

# --- Abbreviations & Synonyms ---
spell = SpellChecker()
ABBR = {
    'inv': 'invoice',
    'exp': 'expense',
    'amt': 'amount',
    'num': 'number',
    'id': 'id',
    'dt': 'date',
    'expdt': 'expense_date',
    'exp_dt': 'expense_date',
    'expense_date': 'expense_date',
    'paydt': 'payable_date',
    'pay_dt': 'payable_date',
    'payable_date': 'payable_date',
    'paid': 'date_paid',
    'desc': 'description',
    'subj': 'subject',
    'typ': 'type',
    'arch': 'is_archived',
    'att': 'attachment',
    'file': 'attachment'
}
SYNONYMS = {'cost': 'amount', 'total': 'amount'}

def preprocess(header: str) -> str:
    """
    Normalize header: lowercase, remove non-alphanumeric, expand abbreviations, correct spelling, apply synonyms.
    """
    h = str(header).lower().strip()
    h = re.sub(r"[^0-9a-z]+", " ", h)
    for abbr, full in sorted(ABBR.items(), key=lambda kv: -len(kv[0])):
        h = re.sub(rf"\b{re.escape(abbr)}\b", full, h)
    tokens = [tok for tok in h.split() if tok]
    processed = []
    for tok in tokens:
        corr = spell.correction(tok) or tok
        processed.append(SYNONYMS.get(corr, corr))
    return ' '.join(processed)

# --- Schema for Expense & Invoice ---
SCHEMA = {
    m._meta.db_table: [
        f.name for f in m._meta.get_fields() if hasattr(f, 'column') and f.column
    ]
    for m in (Expense, Invoice)
}

# --- Load embeddings once ---
EMB_DIR = Path(__file__).resolve().parent / 'embeddings'
glove = KeyedVectors.load_word2vec_format(
    str(EMB_DIR / 'glove.6B.200d.word2vec.txt'), binary=False, unicode_errors='ignore'
)
fasttext = KeyedVectors.load_word2vec_format(
    str(EMB_DIR / 'wiki-news-300d-1M-subword.vec'), binary=False, unicode_errors='ignore'
)

# --- Build field embeddings & semantic signatures ---
FIELD_EMBS = {}
COLUMN_SIGS = {}
for model in (Expense, Invoice):
    tbl = model._meta.db_table
    FIELD_EMBS[tbl] = {}
    for fld in SCHEMA[tbl]:
        pfld = preprocess(fld)
        words = pfld.split()
        gvecs = [glove[w] for w in words if w in glove]
        fvecs = [fasttext[w] for w in words if w in fasttext]
        FIELD_EMBS[tbl][fld] = {
            'glove': np.mean(gvecs, axis=0) if gvecs else np.zeros(glove.vector_size),
            'fasttext': np.mean(fvecs, axis=0) if fvecs else np.zeros(fasttext.vector_size),
            'pre': pfld
        }
    vals_qs = {
        fld: list(
            model.objects.exclude(**{f"{fld}__isnull": True})
                  .values_list(fld, flat=True)[:20]
        ) for fld in SCHEMA[tbl]
    }
    sigs = {}
    for fld, vals in vals_qs.items():
        samples = [str(v) for v in vals if isinstance(v, str)][:5]
        vecs = [
            np.mean([fasttext[w] for w in preprocess(s).split() if w in fasttext], axis=0)
            for s in samples
        ]
        sigs[fld] = {'sem': np.mean(vecs, axis=0) if vecs else np.zeros(fasttext.vector_size)}
    COLUMN_SIGS[tbl] = sigs

# --- Safe cosine similarity with shape guard ---
def cosine_sim(a, b):
    a_arr = np.asarray(a, dtype=float).ravel()
    b_arr = np.asarray(b, dtype=float).ravel()
    if a_arr.shape != b_arr.shape or a_arr.size == 0:
        return 0.0
    na = norm(a_arr); nb = norm(b_arr)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / (na * nb))

# --- Main mapping function ---
def clean_and_detect(df: pd.DataFrame, threshold: float = 0.5):
    raw_cols = list(df.columns)
    # Table detection
    table_scores = {}
    for tbl, emb_map in FIELD_EMBS.items():
        sims = []
        for col in raw_cols:
            proc = preprocess(col)
            gvec = np.mean([glove[w] for w in proc.split() if w in glove], axis=0)
            fvec = np.mean([fasttext[w] for w in proc.split() if w in fasttext], axis=0)
            for fe in emb_map.values():
                sims.append(cosine_sim(gvec, fe['glove']) + cosine_sim(fvec, fe['fasttext']))
        table_scores[tbl] = float(np.nanmean(sims)) if sims else 0.0
    detected = max(table_scores, key=table_scores.get)

    # Header mapping
    mapping_log = []
    mapping = {}
    for col in raw_cols:
        proc = preprocess(col)
        gvec = np.mean([glove[w] for w in proc.split() if w in glove], axis=0)
        fvec = np.mean([fasttext[w] for w in proc.split() if w in fasttext], axis=0)
        # direct
        direct = next((fld for fld, fe in FIELD_EMBS[detected].items() if proc == fe['pre'] or proc == fld), None)
        if direct:
            mapping[col] = direct
            mapping_log.append((col, direct, 1.0))
            continue
        # scored
        best_score, best_field = -1.0, col
        for fld, fe in FIELD_EMBS[detected].items():
            sem_score = cosine_sim(gvec, fe['glove']) + cosine_sim(fvec, fe['fasttext'])
            fuzz_score = fuzz.token_sort_ratio(proc, fe['pre']) / 100.0
            sig_score = cosine_sim(fvec, COLUMN_SIGS[detected][fld]['sem'])
            score = 0.5*sem_score + 0.3*fuzz_score + 0.2*sig_score
            if score > best_score:
                best_score, best_field = score, fld
        if best_score >= threshold:
            mapping[col] = best_field
            mapping_log.append((col, best_field, round(best_score, 2)))
        else:
            mapping[col] = col
    cleaned_df = df.rename(columns=mapping)
    return detected, cleaned_df, mapping_log
