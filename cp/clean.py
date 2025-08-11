import pandas as pd
import numpy as np

def clean_invoice_data(df: pd.DataFrame):
    warnings = []

    # --- Amount check ---
    if 'amount' not in df.columns:
        df['amount'] = None
        warnings.append("Missing 'amount' column – left blank.")
    elif df['amount'].isnull().any():
        df['amount'] = df['amount'].where(pd.notnull(df['amount']), None)
        warnings.append(f"Some 'amount' values were null – left blank ({df['amount'].isnull().sum()} rows).")
    elif df['amount'].sum() == 0:
        warnings.append("All invoice amounts are 0. Is this intentional?")

    # --- Critical column null checks ---
    critical_invoice_cols = ['invoice_number', 'description']
    for col in critical_invoice_cols:
        if col not in df.columns:
            df[col] = None
            warnings.append(f"Missing '{col}' column – left blank.")
        elif df[col].isnull().any():
            warnings.append(f"Some invoice '{col}' values are missing – left blank ({df[col].isnull().sum()} rows).")

    # --- Invoice status & dates ---
    if 'invoice_status_id' not in df.columns:
        df['invoice_status_id'] = 1
        warnings.append("Missing 'invoice_status_id' – defaulted to 1 (Pending).")

    if 'invoice_date' not in df.columns or df['invoice_date'].isnull().all():
        df['invoice_date'] = pd.Timestamp.now()
        warnings.append("Missing 'invoice_date' – defaulted to today.")

    if 'due_date' not in df.columns:
        df['due_date'] = df['invoice_date']
        warnings.append("Missing 'due_date' – defaulted to invoice_date.")
    elif df['due_date'].isnull().any():
        df['due_date'].fillna(df['invoice_date'], inplace=True)
        warnings.append(f"Some 'due_date' values missing – filled from 'invoice_date' ({df['due_date'].isnull().sum()} rows).")

    # --- Received and paid ---
    if 'received_amount' not in df.columns:
        df['received_amount'] = None
        warnings.append("Missing 'received_amount' column – left blank.")
    if 'date_paid' not in df.columns:
        df['date_paid'] = None
        warnings.append("Missing 'date_paid' column – left blank.")

    # --- Handle is_archived ---
    if 'is_archived' in df.columns:
        df['is_archived'] = df['is_archived'].apply(lambda x: bool(x) if pd.notnull(x) else False)
    else:
        df['is_archived'] = False
        warnings.append("Missing 'is_archived' column – defaulted to False.")

    # --- Timestamps ---
    if 'created_at' not in df.columns:
        df['created_at'] = pd.Timestamp.now()
        warnings.append("Missing 'created_at' – defaulted to now.")
    if 'updated_at' not in df.columns:
        df['updated_at'] = pd.Timestamp.now()
        warnings.append("Missing 'updated_at' – defaulted to now.")

    # --- Warn if default IDs are all 1 ---
    default_id_cols = ['invoice_status_id']
    for col in default_id_cols:
        if col in df.columns and (df[col] == 1).all():
            warnings.append(f"All {col} values are defaulted to 1 – verify if this is intended.")

    # --- Backup zero-sum check ---
    if df['amount'].sum() == 0:
        warnings.append("All invoice amounts are 0. Is this intentional? (backup check)")

    return df, warnings


def clean_expense_data(df: pd.DataFrame):
    warnings = []

    # --- Amount check ---
    if 'amount' not in df.columns:
        df['amount'] = None
        warnings.append("Missing 'amount' column – left blank.")
    elif df['amount'].isnull().any():
        df['amount'] = df['amount'].fillna(value=None)
        warnings.append(f"Some 'amount' values were null – left blank ({df['amount'].isnull().sum()} rows).")
    elif df['amount'].sum() == 0:
        warnings.append("All expense amounts are 0. Is this intentional?")

    # --- Critical column null checks ---
    critical_expense_cols = ['expense_number', 'description']
    for col in critical_expense_cols:
        if col not in df.columns:
            df[col] = None
            warnings.append(f"Missing '{col}' column – left blank.")
        elif df[col].isnull().any():
            warnings.append(f"Some expense '{col}' values are missing – left blank ({df[col].isnull().sum()} rows).")

    # --- Default IDs with warnings ---
    default_id_cols = ['expense_status_id', 'expense_type_id', 'expense_subject_id', 'recipient_id']
    for col in default_id_cols:
        if col not in df.columns:
            df[col] = 1
            warnings.append(f"Missing '{col}' – defaulted to 1.")
        elif (df[col] == 1).all():
            warnings.append(f"All {col} values are defaulted to 1 – verify if this is intended.")

    # --- Date handling ---
    if 'expense_date' not in df.columns or df['expense_date'].isnull().all():
        df['expense_date'] = pd.Timestamp.now()
        warnings.append("Missing 'expense_date' – defaulted to today.")

    if 'payable_date' not in df.columns:
        df['payable_date'] = None
        warnings.append("Missing 'payable_date' – left blank.")
    if 'liquidated_amount' not in df.columns:
        df['liquidated_amount'] = None
        warnings.append("Missing 'liquidated_amount' – left blank.")
    if 'liquidated_date' not in df.columns:
        df['liquidated_date'] = None
        warnings.append("Missing 'liquidated_date' – left blank.")

    if 'return_date' not in df.columns:
        df['return_date'] = None
        warnings.append("Missing 'return_date' column – left blank.")
    elif df['return_date'].isnull().any():
        df.loc[df['return_date'].isnull(), 'return_date'] = None
        warnings.append(f"Some 'return_date' values were null – left blank ({df['return_date'].isnull().sum()} rows).")

    # --- Attachment handling ---
    if 'attachment' not in df.columns:
        df['attachment'] = None
        warnings.append("Missing 'attachment' – left blank.")

    # --- Handle is_archived ---
    if 'is_archived' in df.columns:
        df['is_archived'] = df['is_archived'].apply(lambda x: bool(x) if pd.notnull(x) else False)
    else:
        df['is_archived'] = False
        warnings.append("Missing 'is_archived' column – defaulted to False.")

    # --- Timestamps ---
    if 'created_at' not in df.columns:
        df['created_at'] = pd.Timestamp.now()
        warnings.append("Missing 'created_at' – defaulted to now.")
    if 'updated_at' not in df.columns:
        df['updated_at'] = pd.Timestamp.now()
        warnings.append("Missing 'updated_at' – defaulted to now.")

    # --- Backup zero-sum check ---
    if df['amount'].sum() == 0:
        warnings.append("All expense amounts are 0. Is this intentional? (backup check)")

    return df, warnings

def clean_date_columns(df, date_cols):
    import numpy as np
    import pandas as pd

    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')
            df[col] = df[col].where(pd.notnull(df[col]), None)
        else:
            df[col] = None
    return df

def safe_numeric(df, cols):
    import numpy as np
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        else:
            df[col] = 0
    return df
