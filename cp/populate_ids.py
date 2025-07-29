import pandas as pd
from datetime import datetime
from django.apps import apps
from django.db.models import Max

Invoice        = apps.get_model('cp', 'Invoice')
Expense        = apps.get_model('cp', 'Expense')
InvoiceStatus  = apps.get_model('cp', 'InvoiceStatus')
ExpenseStatus  = apps.get_model('cp', 'ExpenseStatus')
ExpenseSubject = apps.get_model('cp', 'ExpenseSubject')
ExpenseType    = apps.get_model('cp', 'ExpenseType')
Recipient      = apps.get_model('cp', 'Recipient')

# Cache lookup maps
INVOICE_STATUS_MAP  = {o.name: o.id for o in InvoiceStatus.objects.all()}
EXPENSE_STATUS_MAP  = {o.name: o.id for o in ExpenseStatus.objects.all()}
EXPENSE_SUBJECT_MAP = {o.name: o.id for o in ExpenseSubject.objects.all()}
EXPENSE_TYPE_MAP    = {o.name: o.id for o in ExpenseType.objects.all()}
RECIPIENT_MAP       = {o.name: o.id for o in Recipient.objects.all()}

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

    # Foreign keys
    if table == 'expense' and company_id is not None:
        df['company_id'] = company_id
    if project_id is not None:
        df['project_id'] = project_id

    # Audit fields
    if user_id is not None:
        df['created_by_id'] = user_id
        df['updated_by_id'] = user_id

    # Archive flag (nullable support)
    if 'is_archived' in df.columns:
        df['is_archived'] = (
            df['is_archived']
            .map({'Yes': 1, 'No': 0})
            .astype('Int64')
        )
    else:
        df['is_archived'] = pd.NA

    # Status mapping
    if table == 'invoice':
        if 'invoice_status' in df.columns:
            df['invoice_status_id'] = (
                df['invoice_status']
                .map(INVOICE_STATUS_MAP)
                .astype('Int64')
                .fillna(1)
            )
    else:
        mapping_cols = [
            ('expense_status', EXPENSE_STATUS_MAP),
            ('expense_subject', EXPENSE_SUBJECT_MAP),
            ('expense_type', EXPENSE_TYPE_MAP),
            ('recipient', RECIPIENT_MAP),
        ]
        for col, mapping in mapping_cols:
            if col in df.columns:
                df[f'{col}_id'] = (
                    df[col]
                    .map(mapping)
                    .astype('Int64')
                    .fillna(1)
                )

    # Business key generation
    yy = now_dt.year % 100
    mm = now_dt.month
    if table == 'invoice':
        prefix = f"INV{yy:02d}-{mm:02d}-"
        num_col = 'invoice_number'
        latest = (
            Invoice.objects.filter(invoice_number__startswith=prefix)
                   .aggregate(max_val=Max('invoice_number'))['max_val']
        )
    else:
        prefix = f"EXP{yy:02d}-{mm:02d}-"
        num_col = 'expense_number'
        latest = (
            Expense.objects.filter(expense_number__startswith=prefix)
                   .aggregate(max_val=Max('expense_number'))['max_val']
        )
    last_seq = int(latest.rsplit('-', 1)[-1]) if latest else 0

    if num_col in df.columns:
        df[num_col] = df[num_col].fillna('')
    else:
        df[num_col] = ''

    seq = last_seq + 1
    for idx in df.index[df[num_col] == '']:
        df.at[idx, num_col] = f"{prefix}{seq:04d}"
        seq += 1

    # Timestamps
    for ts in ('created_at', 'updated_at'):
        df[ts] = df.get(ts, now_str)

    # Drop raw text columns
    for col in ['invoice_status', 'expense_status', 'expense_subject', 'expense_type', 'recipient']:
        if col in df.columns:
            df.drop(columns=[col], inplace=True)

    # Final column ordering
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
