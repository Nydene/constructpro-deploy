from pathlib import Path

_glove_model = {}
_fasttext_model = {}

# Define the necessary words for header mapping
def get_filtered_words():
    return {
        # Core fields from invoice & expense
        'invoice', 'expense', 'project', 'company', 'recipient',
        'type', 'subject', 'status', 'user',

        # Identifiers
        'invoice_number', 'expense_number', 'project_number', 'number', 'id',

        # Amounts
        'amount', 'received_amount', 'liquidated_amount',

        # Dates
        'date', 'invoice_date', 'due_date', 'date_paid',
        'expense_date', 'payable_date', 'liquidated_date',
        'return_date', 'created_at', 'updated_at', 'created', 'updated', 'due',

        # Descriptions & attachments
        'description', 'attachment', 'invoice_attachment', 'payment_attachment',
        'file', 'archived', 'is_archive', 'is_archived', 'arch', 'desc', 'subj',

        # Abbreviated or user-upload variants
        'amt', 'expdt', 'paydt', 'liqamt', 'liqdt', 'returndt',
        'inv', 'exp', 'paid', 'typ', 'subj', 'invdt', 'duedt', 'paydt', 'returndate',
        'inv_file', 'pay_file', 'return', 'attachment_file',

        # Common mislabels
        'total', 'cost', 'file_upload', 'remarks', 'details', 'ref', 'reference'
    }

# Load only allowed words from the .vec file
def load_filtered_embeddings(vec_path, allowed_words):
    model = {}
    with open(vec_path, 'r', encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 10:  # skip header or bad lines
                continue
            word = parts[0]
            if word in allowed_words:
                try:
                    model[word] = [float(x) for x in parts[1:]]
                except:
                    continue  # skip corrupted line
    return model

# Lazy-load GloVe when requested
def get_glove():
    global _glove_model
    if not _glove_model:
        print("[INFO] Loading filtered GloVe embeddings...")
        path = Path(__file__).resolve().parent / 'embeddings' / 'glove.6B.200d.word2vec.txt'
        _glove_model = load_filtered_embeddings(str(path), get_filtered_words())
        print("[INFO] GloVe loaded.")
    return _glove_model

# Lazy-load FastText when requested
def get_fasttext():
    global _fasttext_model
    if not _fasttext_model:
        print("[INFO] Loading filtered FastText embeddings...")
        path = Path(__file__).resolve().parent / 'embeddings' / 'wiki-news-300d-1M-subword.vec'
        _fasttext_model = load_filtered_embeddings(str(path), get_filtered_words())
        print("[INFO] FastText loaded.")
    return _fasttext_model
