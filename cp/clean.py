import pandas as pd


def clean_invoice_data(df):
    warnings = []

    # Amount checks
    if 'amount' not in df.columns:
        df['amount'] = None
        warnings.append("Missing 'amount' column – left blank.")
    elif df['amount'].isnull().any():
        df['amount'] = df['amount'].where(pd.notnull(df['amount']), None)
        warnings.append("Some 'amount' values were null – left blank.")
    elif df['amount'].sum() == 0:
        warnings.append("All invoice amounts are 0. Is this intentional?")

    # Invoice status and dates
    if 'invoice_status_id' not in df.columns:
        df['invoice_status_id'] = 1
    if 'invoice_date' not in df.columns or df['invoice_date'].isnull().all():
        df['invoice_date'] = pd.Timestamp.now()
        warnings.append("Missing 'invoice_date' – defaulted to today.")
    if 'due_date' not in df.columns:
        df['due_date'] = df['invoice_date']
    elif df['due_date'].isnull().any():
        df['due_date'].fillna(df['invoice_date'], inplace=True)
        warnings.append("Some 'due_date' values missing – filled from 'invoice_date'.")

    # Received and paid
    if 'received_amount' not in df.columns:
        df['received_amount'] = None
    if 'date_paid' not in df.columns:
        df['date_paid'] = None

    # Handle is_archived
    if 'is_archived' in df.columns:
        df['is_archived'] = df['is_archived'].apply(lambda x: bool(x) if pd.notnull(x) else False)
    else:
        df['is_archived'] = False

    # Timestamps
    if 'created_at' not in df.columns:
        df['created_at'] = pd.Timestamp.now()
    if 'updated_at' not in df.columns:
        df['updated_at'] = pd.Timestamp.now()

    # Zero-sum check
    if df['amount'].sum() == 0:
        warnings.append("All invoice amounts are 0. Is this intentional? (backup check)")

    return df, warnings


def clean_expense_data(df):
    warnings = []

    if 'amount' not in df.columns:
        df['amount'] = None
        warnings.append("Missing 'amount' column – left blank.")
    elif df['amount'].isnull().any():
        df['amount'] = df['amount'].fillna(value=None)
        warnings.append("Some 'amount' values were null – left blank.")
    elif df['amount'].sum() == 0:
        warnings.append("All expense amounts are 0. Is this intentional?")

    if 'expense_status_id' not in df.columns:
        df['expense_status_id'] = 1
    if 'expense_type_id' not in df.columns:
        df['expense_type_id'] = 1
    if 'expense_subject_id' not in df.columns:
        df['expense_subject_id'] = 1

    if 'expense_date' not in df.columns or df['expense_date'].isnull().all():
        df['expense_date'] = pd.Timestamp.now()
        warnings.append("Missing 'expense_date' – defaulted to today.")

    if 'payable_date' not in df.columns:
        df['payable_date'] = None
    if 'liquidated_amount' not in df.columns:
        df['liquidated_amount'] = None
    if 'liquidated_date' not in df.columns:
        df['liquidated_date'] = None

    if 'return_date' not in df.columns:
        df['return_date'] = None
        warnings.append("Missing 'return_date' column – left blank.")
    elif df['return_date'].isnull().any():
        df.loc[df['return_date'].isnull(), 'return_date'] = None
        warnings.append("Some 'return_date' values were null – left blank.")

    if 'attachment' not in df.columns:
        df['attachment'] = None

    # Handle is_archived
    if 'is_archived' in df.columns:
        df['is_archived'] = df['is_archived'].apply(lambda x: bool(x) if pd.notnull(x) else False)
    else:
        df['is_archived'] = False

    if 'created_at' not in df.columns:
        df['created_at'] = pd.Timestamp.now()
    if 'updated_at' not in df.columns:
        df['updated_at'] = pd.Timestamp.now()

    if df['amount'].sum() == 0:
        warnings.append("All expense amounts are 0. Is this intentional? (backup check)")

    return df, warnings
