import pandas as pd
from datetime import datetime
from django.apps import apps
from django.db.models import Max
from difflib import get_close_matches

# Load models dynamically (avoids circular import issues)
Invoice        = apps.get_model('cp', 'Invoice')
Expense        = apps.get_model('cp', 'Expense')
InvoiceStatus  = apps.get_model('cp', 'InvoiceStatus')
ExpenseStatus  = apps.get_model('cp', 'ExpenseStatus')
ExpenseSubject = apps.get_model('cp', 'ExpenseSubject')
ExpenseType    = apps.get_model('cp', 'ExpenseType')
Recipient      = apps.get_model('cp', 'Recipient')

# Cache lookup maps (keys normalized to lowercase)
INVOICE_STATUS_MAP  = {o.name.strip().lower(): o.id for o in InvoiceStatus.objects.all()}
EXPENSE_STATUS_MAP  = {o.name.strip().lower(): o.id for o in ExpenseStatus.objects.all()}
EXPENSE_SUBJECT_MAP = {o.name.strip().lower(): o.id for o in ExpenseSubject.objects.all()}
EXPENSE_TYPE_MAP    = {o.name.strip().lower(): o.id for o in ExpenseType.objects.all()}
RECIPIENT_MAP       = {o.name.strip().lower(): o.id for o in Recipient.objects.all()}


def normalize_status(val, lookup, errors, default_id=1):
    if pd.isna(val) or str(val).strip() == '':
        return default_id
    val = str(val).strip().lower()
    if val in lookup:
        return lookup[val]

    # Try fuzzy match
    closest = get_close_matches(val, lookup.keys(), n=1, cutoff=0.85)
    if closest:
        print(f"⚠️ Autocorrected '{val}' → '{closest[0]}'")
        return lookup[closest[0]]

    # Unknown: return default and log
    errors.append(val)
    return default_id


def populate_ids(
    df: pd.DataFrame,
    table: str,
    project_id=None,
    user_id=None,
    company_id=None,
) -> pd.DataFrame:
    df = df.copy()
    now_dt = datetime.now()
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    errors = []

    # ---------------------
    # 1) Foreign Keys
    # ---------------------
    if table == 'expense' and company_id is not None:
        df['company_id'] = company_id
    if project_id is not None:
        df['project_id'] = project_id

    # ---------------------
    # 2) Audit Fields
    # ---------------------
    if user_id is not None:
        df['created_by_id'] = user_id
        df['updated_by_id'] = user_id

    # ---------------------
    # 3) Archive Flag
    # ---------------------
    if 'is_archived' in df.columns:
        df['is_archived'] = (
            df['is_archived']
            .astype(str)
            .str.strip()
            .str.lower()
            .map({'yes': 1, 'no': 0, '1': 1, '0': 0, 'true': 1, 'false': 0})
            .fillna(0)
            .astype('Int64')
        )
    else:
        df['is_archived'] = 0

    # ---------------------
    # 4) Status & FK Mapping
    # ---------------------
    if table == 'invoice':
        first_status = InvoiceStatus.objects.first()
        default_id = first_status.id if first_status else 1

        if 'invoice_status' in df.columns:
            df['invoice_status_id'] = df['invoice_status'].apply(
                lambda x: normalize_status(x, INVOICE_STATUS_MAP, errors, default_id)
            ).astype('Int64')
        else:
            df['invoice_status_id'] = default_id

    else:
        first_exp_status = ExpenseStatus.objects.first()
        first_subj = ExpenseSubject.objects.first()
        first_type = ExpenseType.objects.first()
        first_recipient = Recipient.objects.first()

        mapping_cols = [
            ('expense_status', EXPENSE_STATUS_MAP, first_exp_status.id if first_exp_status else 1),
            ('expense_subject', EXPENSE_SUBJECT_MAP, first_subj.id if first_subj else 1),
            ('expense_type', EXPENSE_TYPE_MAP, first_type.id if first_type else 1),
            ('recipient', RECIPIENT_MAP, first_recipient.id if first_recipient else 1),
        ]

        for col, mapping, default_id in mapping_cols:
            if col in df.columns:
                df[f'{col}_id'] = df[col].apply(
                    lambda x: normalize_status(x, mapping, errors, default_id)
                ).astype('Int64')
            else:
                df[f'{col}_id'] = default_id

    # ---------------------
    # 5) Business Key Generation
    # ---------------------
    yy = now_dt.year % 100
    mm = now_dt.month

    if table == 'invoice':
        prefix = f"INV{yy:02d}-{mm:02d}-"
        num_col = 'invoice_number'

        latest = (
            Invoice.objects.filter(invoice_number__startswith=prefix)
                   .aggregate(max_val=Max('invoice_number'))['max_val']
        )
        # Avoid keeping duplicates: collect existing numbers
        existing_numbers = set(Invoice.objects.values_list('invoice_number', flat=True))
    else:
        prefix = f"EXP{yy:02d}-{mm:02d}-"
        num_col = 'expense_number'

        latest = (
            Expense.objects.filter(expense_number__startswith=prefix)
                   .aggregate(max_val=Max('expense_number'))['max_val']
        )
        # Avoid keeping duplicates: collect existing numbers
        existing_numbers = set(Expense.objects.values_list('expense_number', flat=True))

    last_seq = int(str(latest).rsplit('-', 1)[-1]) if latest else 0

    # Normalize provided numbers and blank anything we shouldn't keep
    if num_col in df.columns:
        df[num_col] = df[num_col].astype(str).str.strip()

        # Keep only values that start with this month's prefix AND are not in DB already.
        # Everything else is blanked so we generate a fresh unique number.
        df[num_col] = df[num_col].where(
            df[num_col].str.startswith(prefix) & (~df[num_col].isin(existing_numbers)),
            ''
        )
    else:
        df[num_col] = ''

    # Assign fresh numbers starting after the DB max for this month
    seq = last_seq
    for idx in df.index[df[num_col] == '']:
        seq += 1
        df.at[idx, num_col] = f"{prefix}{seq:04d}"

    # ---------------------
    # 6) Timestamp Columns
    # ---------------------
    for ts in ('created_at', 'updated_at'):
        df[ts] = df.get(ts, now_str)

    # ---------------------
    # 7) Drop raw FK columns
    # ---------------------
    for col in ['invoice_status', 'expense_status', 'expense_subject', 'expense_type', 'recipient']:
        if col in df.columns:
            df.drop(columns=[col], inplace=True)

    # ---------------------
    # 7.5) Ensure NaT-safe datetime/date fields
    # ---------------------
    datetime_cols = [
        'invoice_date', 'due_date', 'date_paid',
        'expense_date', 'payable_date', 'liquidated_date', 'return_date'
    ]

    for col in datetime_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')
            df[col] = df[col].apply(lambda x: x.to_pydatetime() if pd.notnull(x) else None)

    # ---------------------
    # 8) Final Column Ordering
    # ---------------------
    invoice_cols = [
        'project_id', 'invoice_number', 'amount', 'received_amount',
        'invoice_date', 'due_date', 'date_paid', 'invoice_attachment',
        'payment_attachment', 'invoice_status_id', 'is_archived',
        'created_by_id', 'updated_by_id', 'created_at', 'updated_at'
    ]
    expense_cols = [
        'company_id', 'project_id', 'recipient_id',
        'expense_type_id', 'expense_subject_id', 'expense_status_id',
        'expense_number', 'description', 'amount', 'expense_date',
        'payable_date', 'liquidated_amount', 'liquidated_date',
        'return_date', 'attachment', 'is_archived', 'created_by_id',
        'updated_by_id', 'created_at', 'updated_at'
    ]

    desired = invoice_cols if table == 'invoice' else expense_cols
    final = [c for c in desired if c in df.columns]

    return df[final]


def populate_invoice_ids(df, company_id):
    from django.db.models import Max
    from datetime import datetime
    import pandas as pd

    df = df.copy()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # flags & timestamps
    if 'is_archived' not in df.columns:
        df['is_archived'] = 0
    if 'created_at' in df.columns:
        df['created_at'] = df['created_at'].fillna(now)
    else:
        df['created_at'] = pd.Series([now] * len(df), index=df.index)
    if 'updated_at' in df.columns:
        df['updated_at'] = df['updated_at'].fillna(now)
    else:
        df['updated_at'] = pd.Series([now] * len(df), index=df.index)

    # monthly prefix
    prefix = datetime.now().strftime('INV%y-%m-')

    # ensure column + normalize
    if 'invoice_number' not in df.columns:
        df['invoice_number'] = ''
    df['invoice_number'] = df['invoice_number'].fillna('').astype(str).str.strip()

    # DB max for this month
    latest = Invoice.objects.filter(invoice_number__startswith=prefix) \
                            .aggregate(Max('invoice_number'))['invoice_number__max']
    db_max = int(str(latest).rsplit('-', 1)[-1]) if latest else 0

    # CSV max (only same prefix)
    csv_existing = df.loc[df['invoice_number'].str.startswith(prefix), 'invoice_number']
    if not csv_existing.empty:
        try:
            csv_max = pd.to_numeric(
                csv_existing.str.rsplit('-', n=1).str[-1],
                errors='coerce'
            ).dropna().astype(int).max()
        except Exception:
            csv_max = 0
    else:
        csv_max = 0

    # assign to missing starting after max(DB, CSV)
    seq = max(db_max, csv_max)
    missing = df['invoice_number'] == ''
    for idx in df.index[missing]:
        seq += 1
        df.at[idx, 'invoice_number'] = f"{prefix}{seq:04d}"

    # ensure uniqueness within this batch (avoid duplicates inside df)
    seen = set()
    for i, num in enumerate(df['invoice_number'].astype(str)):
        if num in seen or num == '':
            seq += 1
            df.at[df.index[i], 'invoice_number'] = f"{prefix}{seq:04d}"
        seen.add(df.at[df.index[i], 'invoice_number'])

    return df


def populate_expense_ids(df, company_id):
    from django.db.models import Max
    from datetime import datetime
    import pandas as pd

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    df = df.copy()

    df['is_archived'] = 0
    df['created_at'] = pd.Series([now] * len(df), index=df.index)
    df['updated_at'] = pd.Series([now] * len(df), index=df.index)
    df['company_id'] = pd.Series([company_id] * len(df), index=df.index)

    # Monthly prefix like EXP25-08-
    prefix = datetime.today().strftime('EXP%y-%m-')

    # Find current max in DB
    latest = Expense.objects.filter(expense_number__startswith=prefix) \
                            .aggregate(Max('expense_number'))['expense_number__max']
    if latest:
        try:
            start_seq = int(str(latest).rsplit('-', 1)[-1])
        except Exception:
            start_seq = 0
    else:
        start_seq = 0

    # If CSV already has some numbers with this prefix, consider those too
    if 'expense_number' in df.columns:
        existing_nums = df['expense_number'].astype(str)
        existing_nums = existing_nums[existing_nums.str.startswith(prefix)]
        if not existing_nums.empty:
            try:
                csv_max = existing_nums.str.rsplit('-', 1).str[-1].astype(int).max()
                start_seq = max(start_seq, csv_max)
            except Exception:
                pass
    else:
        df['expense_number'] = ''

    # Fill numbers starting after the max
    expense_numbers = []
    seq = start_seq
    for _ in range(len(df)):
        seq += 1
        expense_numbers.append(f"{prefix}{seq:04d}")

    df['expense_number'] = expense_numbers
    return df
