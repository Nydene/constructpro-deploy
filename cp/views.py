import io
import re
import pandas as pd
import numpy as np
from datetime import datetime, date
from django.shortcuts import render, redirect
from django.db.models import Max
from django.contrib import messages
from django.http import HttpResponse
from django.utils import timezone
from passlib.hash import bcrypt as passlib_bcrypt
from django.views.decorators.http import require_POST

from .models import (
    User, Company, Project, Invoice, Expense,
    InvoiceStatus, Recipient,
    ExpenseType, ExpenseSubject, ExpenseStatus
)

from .clean_headers import clean_and_detect
from .populate_ids import populate_ids, populate_invoice_ids, populate_expense_ids
from .clean import clean_invoice_data, clean_expense_data, clean_date_columns, safe_numeric

# =========================
# Utility Functions
# =========================
def safe_import_dataframe(csv_str):
    df = pd.read_csv(io.StringIO(csv_str))
    df = df.where(pd.notnull(df), None)

    # Rename is_archived -> is_archive
    if 'is_archived' in df.columns:
        df.rename(columns={'is_archived': 'is_archive'}, inplace=True)

    # Convert decimal fields to float
    for col in ['amount', 'received_amount', 'liquidated_amount', 'estimated_expenses']:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: float(str(x).replace(',', '')) if x not in [None, '', 'nan'] else 0.0)

    return df


def map_project_ids(df):
    # Normalize project_number or project name from CSV
    if 'project_number' in df.columns:
        df['project_number'] = df['project_number'].astype(str).str.strip().str.upper()
    elif 'project' in df.columns:
        df['project'] = df['project'].astype(str).str.strip().str.upper()

    # Normalize values from DB
    project_map_num = {k.upper(): v for k, v in Project.objects.values_list('project_number', 'id')}
    project_map_name = {k.upper(): v for k, v in Project.objects.values_list('name', 'id')}

    # Try mapping by project_number, else project name
    if 'project_number' in df.columns:
        df['project_id'] = df['project_number'].map(project_map_num)
    elif 'project' in df.columns:
        df['project_id'] = df['project'].map(project_map_name)

    # Debugging printout for skipped mappings
    skipped = df[df['project_id'].isnull()]
    if not skipped.empty:
        print("=== DEBUG PROJECT MAPPING FAILURES ===")
        print("Unmatched rows:", skipped[['project_number']] if 'project_number' in skipped.columns else skipped[['project']])
        print("DB project numbers:", list(project_map_num.keys())[:10])
        print("DB project names:", list(project_map_name.keys())[:10])
        messages.warning(None, f"⚠️ Skipped {len(skipped)} rows due to unmatched project.")

    return df[df['project_id'].notnull()]

def fill_missing_invoice_fields(df):
    df = df.copy()
    df['received_amount'] = df.get('received_amount', 0).fillna(0)
    df['invoice_status_id'] = df.get('invoice_status_id', 1).fillna(1)
    df['due_date'] = df.get('due_date', df.get('invoice_date'))
    df['date_paid'] = df.get('date_paid', None)
    df['invoice_attachment'] = df.get('invoice_attachment', None)
    df['payment_attachment'] = df.get('payment_attachment', None)
    return df


def fill_missing_expense_fields(df):
    df = df.copy()
    df['amount'] = df.get('amount', 0).fillna(0)
    df['expense_status_id'] = df.get('expense_status_id', 1).fillna(1)
    df['liquidated_amount'] = df.get('liquidated_amount', 0).fillna(0)
    df['expense_type'] = df.get('expense_type', 'General').fillna('General')
    df['expense_subject'] = df.get('expense_subject', 'General').fillna('General')
    df['recipient'] = df.get('recipient', 'Unknown').fillna('Unknown')
    df['payable_date'] = df.get('payable_date', None)
    df['liquidated_date'] = df.get('liquidated_date', None)
    df['return_date'] = df.get('return_date', None)
    df['description'] = df.get('description', 'General').fillna('General')
    return df

# --------------------------
# 1) LOGIN & MODE SELECTION
# --------------------------
def login_view(request):
    if request.method == 'POST':
        uname = request.POST.get('username')
        pwd = request.POST.get('password')
        try:
            user = User.objects.get(username=uname)
        except User.DoesNotExist:
            messages.error(request, "Invalid username or password")
            return render(request, 'login.html')

        if passlib_bcrypt.verify(pwd, user.password):
            request.session.flush()
            request.session['user_id'] = user.id
            return redirect('choose_mode')

        messages.error(request, "Invalid username or password")

    return render(request, 'login.html')


def choose_mode(request):
    if request.method == 'POST':
        mode = request.POST.get('mode')
        request.session['next_mode'] = mode
        return redirect('select_company')
    return render(request, 'choose_mode.html')


# --------------------------
# 2) CONSOLIDATED FULL (Projects + Invoices + Expenses)
# --------------------------
def consolidated_full(request):
    preview_projects = None
    preview_invoices = None
    preview_expenses = None
    warnings_invoice = []
    warnings_expense = []

    company_id = request.session.get('company_id')
    user_id = request.session.get('user_id')
    if not company_id:
        messages.error(request, "Please select a company first.")
        return redirect('select_company')

    if request.method == 'POST' and request.FILES.get('raw_file'):
        raw_file = request.FILES['raw_file']
        data = raw_file.read()
        buf = io.BytesIO(data)
        ext = raw_file.name.rsplit('.', 1)[-1].lower()

        try:
            if ext in ('xls', 'xlsx'):
                df = pd.read_excel(buf, engine='openpyxl')
            else:
                df = pd.read_csv(io.BytesIO(data))
        except Exception as e:
            messages.error(request, f"Error reading file: {e}")
            return render(request, 'consolidated_full.html')

        # Normalize headers and drop dup cols
        df.columns = df.columns.astype(str).str.strip().str.title()
        df = df.loc[:, ~df.columns.duplicated(keep='first')]

        expected_cols = {'Date', 'Amount', 'Type', 'Project'}
        if not expected_cols.issubset(df.columns):
            messages.error(request, f"File must have columns: {', '.join(sorted(expected_cols))}")
            return render(request, 'consolidated_full.html')

        # Normalize Date
        df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
        df['Date'] = df['Date'].fillna(pd.Timestamp(datetime.today().date()))

        # Split by type
        invoice_df = df[df['Type'].astype(str).str.lower() == 'invoice'].copy()
        expense_df = df[df['Type'].astype(str).str.lower() != 'invoice'].copy()

        # Unique project names in this file
        projects_unique = pd.Series(df['Project'].dropna().astype(str).str.strip()).unique()
        projects_unique_set = set(projects_unique)

        # Which of those already exist?
        existing_projects = set(
            Project.objects.filter(
                name__in=projects_unique_set,
                company_id=company_id
            ).values_list('name', flat=True)
        )
        all_exist = (len(existing_projects) == len(projects_unique_set))
        request.session['all_projects_existing'] = all_exist

        combined_invoice = []
        combined_expense = []
        project_records = []

        now = datetime.now()

        # Project number prefix for this month
        proj_prefix = f"P{now.year % 100:02d}-{now.month:02d}-"
        latest_proj = (
            Project.objects.filter(project_number__startswith=proj_prefix)
            .aggregate(max_val=Max('project_number'))['max_val']
        )
        last_seq = int(latest_proj.rsplit('-', 1)[-1]) if latest_proj else 0

        def _safe_numeric(val):
            if pd.isna(val):
                return 0
            try:
                return float(str(val).replace(',', '').strip())
            except Exception:
                return 0

        # Numbering prefixes for current month
        ym = now.strftime("%y-%m")
        inv_prefix = f"INV{ym}-"
        exp_prefix = f"EXP{ym}-"

        latest_inv = Invoice.objects.filter(invoice_number__startswith=inv_prefix).aggregate(Max('invoice_number'))['invoice_number__max']
        latest_exp = Expense.objects.filter(expense_number__startswith=exp_prefix).aggregate(Max('expense_number'))['expense_number__max']
        inv_seq = int(latest_inv.rsplit('-', 1)[-1]) if latest_inv else 0
        exp_seq = int(latest_exp.rsplit('-', 1)[-1]) if latest_exp else 0

        created_any_project = False

        # Build previews + CSVs
        for project in projects_unique:
            project_rows = df[df['Project'] == project]
            project_amount = _safe_numeric(project_rows['Amount'].iloc[0]) if 'Amount' in project_rows else 0
            estimated_exp = _safe_numeric(project_rows['Estimated Expenses'].iloc[0]) if 'Estimated Expenses' in project_rows.columns else 0

            existing_proj = Project.objects.filter(name=project, company_id=company_id).first()
            if existing_proj:
                proj_id = existing_proj.id
                proj_number = existing_proj.project_number
            else:
                # Create the project (so invoices/expenses can be imported)
                last_seq += 1
                proj_number = f"{proj_prefix}{last_seq:04d}"
                new_proj = Project.objects.create(
                    name=project,
                    project_number=proj_number,
                    company_id=company_id,
                    client_information_id=151,
                    project_status_id=1,
                    amount=project_amount,
                    estimated_expenses=estimated_exp,
                    created_by_id=user_id
                )
                proj_id = new_proj.id
                created_any_project = True

            project_records.append({
                'Project Name': project,
                'Project Number': proj_number,
                'Project ID': proj_id,
                'Amount': project_amount,
                'Estimated Expenses': estimated_exp
            })

            # Invoices for this project
            proj_invoices = invoice_df[invoice_df['Project'] == project].copy()
            if not proj_invoices.empty:
                proj_invoices = proj_invoices.assign(
                    invoice_date=proj_invoices['Date'],
                    received_amount=0,
                    due_date=proj_invoices['Date'],
                    date_paid=None,
                    invoice_attachment=None,
                    payment_attachment=None,
                    invoice_status='Pending',
                    project_number=proj_number
                )
                invoice_numbers = [f"{inv_prefix}{inv_seq + i + 1:04d}" for i in range(len(proj_invoices))]
                inv_seq += len(invoice_numbers)
                proj_invoices['invoice_number'] = invoice_numbers
                combined_invoice.append(proj_invoices)

            # Expenses for this project
            proj_expenses = expense_df[expense_df['Project'] == project].copy()
            if not proj_expenses.empty:
                proj_expenses = proj_expenses.assign(
                    expense_date=proj_expenses['Date'],
                    payable_date=None,
                    liquidated_amount=0,
                    liquidated_date=None,
                    return_date=None,
                    description=proj_expenses['Type'],
                    expense_status='Pending',
                    expense_type='General',
                    expense_subject='General',
                    recipient='Unknown',
                    project_number=proj_number
                )
                expense_numbers = [f"{exp_prefix}{exp_seq + i + 1:04d}" for i in range(len(proj_expenses))]
                exp_seq += len(expense_numbers)
                proj_expenses['expense_number'] = expense_numbers
                combined_expense.append(proj_expenses)

        # Projects preview + CSV
        proj_df = pd.DataFrame(project_records)
        request.session['full_projects_csv'] = proj_df.to_csv(index=False)
        preview_projects = proj_df.to_html(classes="table table-striped", index=False)

        # Invoices preview + CSV (+ warnings)
        if combined_invoice:
            all_inv = pd.concat(combined_invoice, ignore_index=True)
            all_inv = all_inv.loc[:, ~all_inv.columns.duplicated(keep='first')]
            if 'Amount' in all_inv.columns:
                all_inv['Amount'] = pd.to_numeric(all_inv['Amount'], errors='coerce').fillna(0)

            existing_inv_nums = set(Invoice.objects.values_list('invoice_number', flat=True))
            all_inv = all_inv[~all_inv['invoice_number'].isin(existing_inv_nums)]

            try:
                _ = all_inv.rename(columns=str.lower).copy()
                _, warnings_invoice = clean_invoice_data(_)
            except Exception:
                warnings_invoice = []

            request.session['warnings_invoice'] = warnings_invoice
            request.session['full_invoices_csv'] = all_inv.to_csv(index=False)
            preview_invoices = all_inv.head(50).to_html(classes="table table-striped", index=False)

        # Expenses preview + CSV (+ warnings)
        if combined_expense:
            all_exp = pd.concat(combined_expense, ignore_index=True)
            all_exp = all_exp.loc[:, ~all_exp.columns.duplicated(keep='first')]
            if 'Amount' in all_exp.columns:
                all_exp['Amount'] = pd.to_numeric(all_exp['Amount'], errors='coerce').fillna(0)

            try:
                _ = all_exp.rename(columns=str.lower).copy()
                _, warnings_expense = clean_expense_data(_)
            except Exception:
                warnings_expense = []

            request.session['warnings_expense'] = warnings_expense
            request.session['full_expenses_csv'] = all_exp.to_csv(index=False)
            preview_expenses = all_exp.head(50).to_html(classes="table table-striped", index=False)

        # Button state:
        # True if all projects already existed OR we created any this run
        projects_imported = bool(all_exist or created_any_project)
        request.session['projects_imported'] = projects_imported

        # Info banner when nothing to import for projects
        if all_exist and not created_any_project:
            messages.info(request, "ℹ️ All projects in this file already exist in the database.")

        # Save previews for GET refresh
        request.session['preview_projects_html'] = preview_projects
        request.session['preview_invoices_html'] = preview_invoices
        request.session['preview_expenses_html'] = preview_expenses

        messages.success(request, "✅ Full consolidated file processed successfully (Preview Only).")

    else:
        # GET: restore previews; if only CSVs exist, reconstruct HTML
        preview_projects = request.session.get('preview_projects_html')
        preview_invoices = request.session.get('preview_invoices_html')
        preview_expenses = request.session.get('preview_expenses_html')

        if not preview_projects:
            proj_csv = request.session.get('full_projects_csv')
            if proj_csv:
                try:
                    dfp = pd.read_csv(io.StringIO(proj_csv))
                    preview_projects = dfp.head(50).to_html(classes="table table-striped", index=False)
                    request.session['preview_projects_html'] = preview_projects
                except Exception:
                    pass

        if not preview_invoices:
            inv_csv = request.session.get('full_invoices_csv')
            if inv_csv:
                try:
                    dfi = pd.read_csv(io.StringIO(inv_csv))
                    preview_invoices = dfi.head(50).to_html(classes="table table-striped", index=False)
                    request.session['preview_invoices_html'] = preview_invoices
                except Exception:
                    pass

        if not preview_expenses:
            exp_csv = request.session.get('full_expenses_csv')
            if exp_csv:
                try:
                    dfe = pd.read_csv(io.StringIO(exp_csv))
                    preview_expenses = dfe.head(50).to_html(classes="table table-striped", index=False)
                    request.session['preview_expenses_html'] = preview_expenses
                except Exception:
                    pass

        # restore warnings saved during POST
        warnings_invoice = request.session.get('warnings_invoice', [])
        warnings_expense = request.session.get('warnings_expense', [])

    return render(request, 'consolidated_full.html', {
        'preview_projects': preview_projects,
        'preview_invoices': preview_invoices,
        'preview_expenses': preview_expenses,
        'warnings_invoice': warnings_invoice,
        'warnings_expense': warnings_expense,
        'all_projects_existing': request.session.get('all_projects_existing', False),
        'projects_imported': request.session.get('projects_imported', False),
    })



# --------------------------
# 3) CONSOLIDATED IE (Invoices + Expenses)
# --------------------------
def consolidated_ie(request):
    """
    Upload + preview for Invoices & Expenses only.
    Every POST resets the previous IE session state so re-uploads show a fresh preview.
    """
    preview_invoice_html = None
    preview_expense_html = None
    warnings_invoice = []
    warnings_expense = []

    if request.method == 'POST' and request.FILES.get('raw_file'):
        # 1) Clear old state so a new upload makes a brand-new preview
        for k in (
            'ie_invoices_csv', 'ie_expenses_csv', 'consolidated_csv',
            'preview_invoice_html', 'preview_expense_html',
            'full_invoices_preview', 'full_expenses_preview',
            'warnings_invoice', 'warnings_expense',
            'full_invoices_imported', 'full_expenses_imported',
        ):
            request.session.pop(k, None)

        raw = request.FILES['raw_file']
        data = raw.read()
        buf = io.BytesIO(data)
        ext = raw.name.rsplit('.', 1)[-1].lower()

        # 2) Read file
        try:
            if ext in ('xls', 'xlsx'):
                df = pd.read_excel(buf, engine='openpyxl')
            else:
                df = pd.read_csv(io.BytesIO(data))
        except Exception as e:
            messages.error(request, f"Error reading file: {e}")
            return render(request, 'consolidated_ie.html')

        # 3) Validate + split
        if 'Type' not in df.columns:
            messages.error(request, "Missing 'Type' column in file. Cannot proceed.")
            return render(request, 'consolidated_ie.html')

        df['Type'] = df['Type'].astype(str).str.strip().str.lower()
        df_invoice = df[df['Type'] == 'invoice'].copy()
        df_expense = df[df['Type'] == 'expense'].copy()

        combined = []

        # 4) Invoices
        if not df_invoice.empty:
            df_invoice.rename(columns={
                'No': 'invoice_number',
                'Date': 'invoice_date',
                'Item': 'description',
                'Amount': 'amount',
            }, inplace=True)

            df_invoice = populate_ids(
                df_invoice, table='invoice',
                project_id=request.session.get('project_id'),
                user_id=request.session.get('user_id'),
                company_id=request.session.get('company_id'),
            )
            df_invoice, warnings_invoice = clean_invoice_data(df_invoice)

            # Save CSV + NEW preview to session
            request.session['ie_invoices_csv'] = df_invoice.to_csv(index=False)
            preview_invoice_html = df_invoice.head(20).to_html(classes="table table-striped", index=False)
            request.session['preview_invoice_html'] = preview_invoice_html

            combined.append(df_invoice)

        # 5) Expenses
        if not df_expense.empty:
            df_expense.rename(columns={
                'No': 'expense_number',
                'Date': 'expense_date',
                'Item': 'description',
                'Amount': 'amount',
            }, inplace=True)

            df_expense = populate_ids(
                df_expense, table='expense',
                project_id=request.session.get('project_id'),
                user_id=request.session.get('user_id'),
                company_id=request.session.get('company_id'),
            )
            df_expense, warnings_expense = clean_expense_data(df_expense)

            request.session['ie_expenses_csv'] = df_expense.to_csv(index=False)
            preview_expense_html = df_expense.head(20).to_html(classes="table table-striped", index=False)
            request.session['preview_expense_html'] = preview_expense_html

            combined.append(df_expense)

        if not combined:
            messages.error(request, "No valid Invoice or Expense rows found.")
            return render(request, 'consolidated_ie.html')

        # 6) For “Download Combined CSV”
        df_final = pd.concat(combined, ignore_index=True)
        s_buf = io.StringIO()
        df_final.to_csv(s_buf, index=False)
        request.session['consolidated_csv'] = s_buf.getvalue()

        # 7) Persist warnings + RESET imported flags so buttons are active
        request.session['warnings_invoice'] = warnings_invoice
        request.session['warnings_expense'] = warnings_expense
        request.session['full_invoices_imported'] = False
        request.session['full_expenses_imported'] = False

        # 8) Render page with the new previews (no redirect needed)
        return render(request, 'consolidated_ie.html', {
            'preview_invoice_html': preview_invoice_html,
            'preview_expense_html': preview_expense_html,
            'warnings_invoice': warnings_invoice,
            'warnings_expense': warnings_expense,
            'full_invoices_imported': False,
            'full_expenses_imported': False,
        })

    # GET: restore whatever is in session (unless you’ve cleared it)
    return render(request, 'consolidated_ie.html', {
        'preview_invoice_html': request.session.get('preview_invoice_html'),
        'preview_expense_html': request.session.get('preview_expense_html'),
        'warnings_invoice': request.session.get('warnings_invoice', []),
        'warnings_expense': request.session.get('warnings_expense', []),
        'full_invoices_imported': request.session.get('full_invoices_imported', False),
        'full_expenses_imported': request.session.get('full_expenses_imported', False),
    })

# --------------------------
# 4) SELECT COMPANY
# --------------------------
def select_company(request):
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')

    user = User.objects.get(pk=user_id)
    companies = Company.objects.filter(id=user.company_id)
    projects = Project.objects.filter(company_id=user.company_id)

    if request.method == 'POST':
        request.session['company_id'] = int(request.POST['company_id'])
        next_mode = request.session.get('next_mode', 'uncons')
        if next_mode != 'full':
            request.session['project_id'] = int(request.POST['project_id'])

        request.session.pop('next_mode', None)
        if next_mode == 'full':
            return redirect('consolidated_full')
        elif next_mode == 'ie':
            return redirect('consolidated_ie')
        else:
            return redirect('upload')

    next_mode = request.session.get('next_mode', 'uncons')
    return render(request, 'select_company.html', {
        'user': user,
        'companies': companies,
        'projects': projects,
        'next_mode': next_mode,
        'hide_project': next_mode == 'full',
    })

# --------------------------
# 5) STANDARD UPLOAD VIEW (Unconsolidated)
# --------------------------
def upload_view(request):
    preview_html = None
    detected_table = None
    mapping_log = []
    warnings_list = []

    if request.method == 'POST' and request.FILES.get('raw_file'):
        raw = request.FILES['raw_file']
        data = raw.read()
        buf = io.BytesIO(data)
        ext = raw.name.rsplit('.', 1)[-1].lower()

        try:
            if ext in ('xls', 'xlsx'):
                df_try = pd.read_excel(buf, engine='openpyxl')
            else:
                df_try = pd.read_csv(io.BytesIO(data))
        except Exception as e:
            messages.error(request, f"Error reading file: {e}")
            return render(request, 'upload.html')

        # Detect headerless file
        cols = df_try.columns.tolist()
        def is_data(c):
            s = str(c).strip()
            return bool(
                re.fullmatch(r"\d+(?:\.\d+)?", s) or
                re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", s)
            )
        if sum(is_data(c) for c in cols) > len(cols) / 2:
            messages.error(request, "No header row detected. Please upload a file with headers.")
            return render(request, 'upload.html')

        # Clean headers & detect table type
        buf.seek(0)
        detected_table, df_clean, mapping_log = clean_and_detect(df_try.copy())

        # Normalize and fill is_archive
        df_clean.rename(columns={'is_archived': 'is_archive'}, inplace=True)
        if 'is_archive' not in df_clean.columns:
            df_clean['is_archive'] = False

        # Validate allowed fields
        allowed_invoice_fields = {
            'invoice_number', 'amount', 'received_amount', 'invoice_date', 'due_date',
            'date_paid', 'invoice_attachment', 'payment_attachment',
            'invoice_status', 'is_archive', 'created_at'
        }
        allowed_expense_fields = {
            'expense_number', 'description', 'amount', 'expense_date', 'payable_date',
            'liquidated_amount', 'liquidated_date', 'return_date', 'attachment',
            'recipient', 'expense_type', 'expense_subject', 'expense_status',
            'is_archive', 'created_at'
        }
        allowed_fields = allowed_invoice_fields if detected_table == 'invoice' else allowed_expense_fields
        unrelated = set(df_clean.columns) - allowed_fields
        if unrelated:
            messages.warning(request, f"⚠️ Unrelated columns detected: {', '.join(unrelated)}")
            messages.error(request, "Header mismatch. Please re-upload using the correct format.")
            return render(request, 'upload.html', {
                'mapping_log': mapping_log,
                'warnings': []
            })

        # Populate IDs
        try:
            df_final = populate_ids(
                df_clean,
                detected_table,
                project_id=request.session.get('project_id'),
                user_id=request.session.get('user_id'),
                company_id=request.session.get('company_id')
            )
        except Exception as e:
            messages.error(request, str(e))
            return render(request, 'upload.html', {
                'mapping_log': mapping_log,
                'warnings': []
            })

        # Clean data & gather warnings
        if detected_table == 'invoice':
            df_final, warnings_list = clean_invoice_data(df_final)
        else:
            df_final, warnings_list = clean_expense_data(df_final)
        for w in warnings_list:
            messages.warning(request, w)

        # Preview & session store
        preview_html = df_final.head(20).to_html(classes="table table-striped", index=False)
        s_buf = io.StringIO()
        df_final.to_csv(s_buf, index=False)
        request.session['cleaned_csv'] = s_buf.getvalue()
        request.session['detected_table'] = detected_table

    return render(request, 'upload.html', {
        'preview_html': preview_html,
        'mapping_log': mapping_log,
        'warnings': warnings_list,
        'detected_table': detected_table,
    })


# --------------------------
# 6) CLEAN CSV DOWNLOAD & IMPORT (Unconsolidated)
# --------------------------
def download_clean_csv(request):
    csv_data = request.session.get('cleaned_csv')
    if not csv_data:
        messages.error(request, "No cleaned file available to download.")
        return redirect('upload')
    resp = HttpResponse(csv_data, content_type="text/csv")
    resp['Content-Disposition'] = 'attachment; filename="cleaned_upload.csv"'
    return resp


def import_clean_csv(request):
    if request.method != 'POST':
        return redirect('upload_ai')

    # Load sessions
    csv_data = request.session.pop('cleaned_csv', None)
    table = request.session.pop('table_type', None) or request.session.pop('detected_table', None)
    project_id = request.session.get('project_id')
    company_id = request.session.get('company_id')
    user_id = request.session.get('user_id')

    if not csv_data or not table:
        messages.error(request, "Nothing to import. Please upload & preview first.")
        return redirect('upload_ai')

    try:
        df = pd.read_csv(io.StringIO(csv_data))

        # 🧼 Drop duplicate columns
        df = df.loc[:, ~df.columns.duplicated(keep='first')]

        # 🧹 Remove known bad columns
        for col in list(df.columns):
            if col.lower() in ('id', 'created_at', 'updated_at'):
                df.drop(columns=[col], inplace=True)

        # 🧼 Rename is_archived → is_archive (if needed)
        if 'is_archived' in df.columns:
            df.rename(columns={'is_archived': 'is_archive'}, inplace=True)

        # ⏱️ Clean datetime columns
        date_cols = (
            ['invoice_date', 'due_date', 'date_paid']
            if table == 'invoice'
            else ['expense_date', 'payable_date', 'liquidated_date', 'return_date']
        )
        for col in date_cols:
            if col in df.columns:
                df[col] = (
                    df[col].astype(str)
                         .str.replace(r'[“”"‘’]', '', regex=True)
                         .str.replace('/', '-', regex=False)
                         .str.strip()
                )
                df[col] = pd.to_datetime(df[col], dayfirst=True, errors='coerce') \
                            .apply(lambda x: x.to_pydatetime() if pd.notnull(x) else None)

        # 🧠 Populate missing fields if full-consolidation route (has project_id, user_id, company_id)
        if company_id and user_id:
            df = populate_ids(df, table=table, project_id=project_id, user_id=user_id, company_id=company_id)

        # ✅ Final conversion
        df = df.where(pd.notnull(df), None)
        Model = Invoice if table == 'invoice' else Expense
        valid_fields = {f.name for f in Model._meta.fields}
        records = df.to_dict(orient='records')
        objs = [Model(**{k: v for k, v in rec.items() if k in valid_fields}) for rec in records]

        Model.objects.bulk_create(objs, ignore_conflicts=True)
        messages.success(request, f"✅ Imported {len(objs)} rows into {table}.")

    except Exception as e:
        messages.error(request, f"❌ Import failed: {str(e)}")

    return redirect('upload')

# --------------------------
# 7) FULL CONSOLIDATED IMPORTS
# --------------------------
@require_POST
def import_full_consolidated(request):
    """
    FULL import that inserts Invoices and Expenses inline (no nested redirects).
    Uses the CSVs produced by consolidated_full POST:
      - request.session['full_invoices_csv']
      - request.session['full_expenses_csv']
    """
    company_id = request.session.get('company_id')
    user_id    = request.session.get('user_id')
    inv_csv    = request.session.get('full_invoices_csv')
    exp_csv    = request.session.get('full_expenses_csv')

    if not company_id or not user_id:
        messages.error(request, "Missing session context. Please re-upload.")
        return redirect('consolidated_full')

    if not inv_csv and not exp_csv:
        messages.error(request, "❌ Nothing to import. Please process a full file first.")
        return redirect('consolidated_full')

    imported_invoices = 0
    imported_expenses = 0

    # -----------------------------
    # Import Invoices (inline)
    # -----------------------------
    if inv_csv:
        try:
            df_inv = pd.read_csv(io.StringIO(inv_csv))

            # If 'Date' exists but 'invoice_date' missing, map it once then drop 'Date'
            if 'invoice_date' not in df_inv.columns and 'Date' in df_inv.columns:
                df_inv['invoice_date'] = pd.to_datetime(df_inv['Date'], errors='coerce')
            if 'Date' in df_inv.columns:
                df_inv.drop(columns=['Date'], inplace=True)

            # Normalize a few headers (we DO NOT rely on 'No')
            rename_map = {
                'Amount': 'amount',
                'Received_Amount': 'received_amount',
                'Received Amount': 'received_amount',
                'Invoice_Date': 'invoice_date',
                'Due_Date': 'due_date',
                'Date_Paid': 'date_paid',
                'Project_Number': 'project_number',
                'Project': 'project_name',
            }
            df_inv.rename(columns={k: v for k, v in rename_map.items() if k in df_inv.columns}, inplace=True)

            # Unify duplicate column names (keep first non-null to the left)
            if df_inv.columns.duplicated().any():
                for col in pd.unique(df_inv.columns[df_inv.columns.duplicated(keep=False)]):
                    same = df_inv.loc[:, df_inv.columns == col]
                    df_inv[col] = same.bfill(axis=1).iloc[:, 0]
                df_inv = df_inv.loc[:, ~df_inv.columns.duplicated(keep='first')]

            # Map project → project_id (prefer number; else name)
            if 'project_number' in df_inv.columns:
                df_inv['project_number'] = df_inv['project_number'].astype(str).str.strip().str.upper()
                project_map = {
                    number.upper(): pid
                    for number, pid in Project.objects.filter(company_id=company_id).values_list('project_number', 'id')
                }
                df_inv['project_id'] = df_inv['project_number'].map(project_map)
            else:
                if 'project_name' not in df_inv.columns:
                    messages.error(request, "Invoice import: missing project reference (number or name).")
                    return redirect('consolidated_full')
                df_inv['project_name'] = df_inv['project_name'].astype(str).str.strip().str.upper()
                proj_name_map = {
                    name.strip().upper(): pid
                    for name, pid in Project.objects.filter(company_id=company_id).values_list('name', 'id')
                }
                df_inv['project_id'] = df_inv['project_name'].map(proj_name_map)

            # Keep only rows that have a project
            df_inv = df_inv[df_inv['project_id'].notna()].copy()
            if df_inv.empty:
                messages.warning(request, "No invoice rows matched a project (skipped).")
            else:
                df_inv['project_id'] = df_inv['project_id'].astype(int)

                # Numerics
                if 'amount' in df_inv.columns:
                    df_inv['amount'] = pd.to_numeric(df_inv['amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
                else:
                    df_inv['amount'] = 0
                if 'received_amount' in df_inv.columns:
                    df_inv['received_amount'] = pd.to_numeric(df_inv['received_amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

                # Dates
                def _fix_dates(df, cols):
                    for c in cols:
                        if c in df.columns:
                            df[c] = pd.to_datetime(df[c], errors='coerce')
                            df[c] = df[c].apply(lambda x: x.date() if pd.notnull(x) else None)
                    return df
                df_inv = _fix_dates(df_inv, ['invoice_date', 'due_date', 'date_paid'])

                # Status FK id (avoid assigning 'Pending' string to FK)
                status_obj = InvoiceStatus.objects.filter(name__iexact='Pending').first()
                if not status_obj:
                    messages.error(request, "Missing default invoice status.")
                    return redirect('consolidated_full')
                df_inv['invoice_status_id'] = status_obj.id
                df_inv.drop(columns=['invoice_status'], errors='ignore', inplace=True)

                # Invoice number: preserve existing; generate only if missing
                prefix = datetime.now().strftime('INV%y-%m-')
                latest = Invoice.objects.filter(invoice_number__startswith=prefix)\
                                        .aggregate(Max('invoice_number'))['invoice_number__max']
                db_max = int(str(latest).rsplit('-', 1)[-1]) if latest else 0

                if 'invoice_number' not in df_inv.columns:
                    df_inv['invoice_number'] = ''
                df_inv['invoice_number'] = df_inv['invoice_number'].fillna('').astype(str).str.strip()

                csv_existing = df_inv.loc[df_inv['invoice_number'].str.startswith(prefix), 'invoice_number']
                if not csv_existing.empty:
                    try:
                        csv_max = pd.to_numeric(
                            csv_existing.str.rsplit('-', n=1, expand=True)[1],
                            errors='coerce'
                        ).dropna().astype(int).max()
                    except Exception:
                        csv_max = 0
                else:
                    csv_max = 0

                seq = max(db_max, csv_max)
                missing_mask = df_inv['invoice_number'] == ''
                for idx in df_inv.index[missing_mask]:
                    seq += 1
                    df_inv.at[idx, 'invoice_number'] = f"{prefix}{seq:04d}"

                # Audit
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if 'created_at' not in df_inv.columns:
                    df_inv['created_at'] = now
                if 'updated_at' not in df_inv.columns:
                    df_inv['updated_at'] = now
                df_inv['is_archived'] = 0
                df_inv['created_by_id'] = user_id
                df_inv['updated_by_id'] = user_id

                # Build objects (whitelist)
                valid_fields = {f.name for f in Invoice._meta.fields}
                existing_numbers = set(
                    Invoice.objects.filter(project__company_id=company_id)
                                   .values_list('invoice_number', flat=True)
                )
                records = []
                for row in df_inv.to_dict(orient='records'):
                    if row.get('invoice_number') in existing_numbers:
                        continue
                    try:
                        row['project'] = Project.objects.get(pk=row['project_id'])
                        row['created_by'] = User.objects.get(pk=user_id)
                        row['updated_by'] = User.objects.get(pk=user_id)
                        # keep invoice_status_id as int FK; drop temp ids
                        for k in ('project_id', 'created_by_id', 'updated_by_id'):
                            row.pop(k, None)
                        row = {k: v for k, v in row.items() if k in (valid_fields | {'invoice_status_id'})}
                        records.append(Invoice(**row))
                    except Exception as e:
                        print(f"⚠️ Skipped invoice row due to error: {e}")

                if records:
                    Invoice.objects.bulk_create(records, ignore_conflicts=True)
                imported_invoices = len(records)
                request.session['full_invoices_imported'] = True

        except Exception as e:
            messages.error(request, f"❌ Invoice import failed: {e}")

    # -----------------------------
    # Import Expenses (inline)
    # -----------------------------
    if exp_csv:
        try:
            df = pd.read_csv(io.StringIO(exp_csv))

            # Normalize headers (no 'No' dependency)
            rename_map = {
                'Amount': 'amount',
                'Expense_Date': 'expense_date',
                'Payable_Date': 'payable_date',
                'Liquidated_Amount': 'liquidated_amount',
                'Liquidated_Date': 'liquidated_date',
                'Return_Date': 'return_date',
                'Expense_Number': 'expense_number',
                'Project_Number': 'project_number',
                'Description': 'description',
            }
            df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)

            # Numerics
            if 'amount' in df.columns:
                df['amount'] = pd.to_numeric(df['amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
            else:
                df['amount'] = 0
            if 'liquidated_amount' in df.columns:
                df['liquidated_amount'] = pd.to_numeric(df['liquidated_amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

            # Map project_number -> project_id
            df['project_number'] = df['project_number'].astype(str).str.strip().str.upper()
            project_map = {
                number.upper(): pid
                for number, pid in Project.objects.filter(company_id=company_id).values_list('project_number', 'id')
            }
            df['project_id'] = df['project_number'].map(project_map)
            df = df[df['project_id'].notna()].copy()
            if df.empty:
                messages.warning(request, "No expense rows matched a project (skipped).")
            else:
                df['project_id'] = df['project_id'].astype(int)

                # Dates
                def _fix_dates(df, cols):
                    for c in cols:
                        if c in df.columns:
                            df[c] = pd.to_datetime(df[c], errors='coerce')
                            df[c] = df[c].apply(lambda x: x.date() if pd.notnull(x) else None)
                    return df
                df = _fix_dates(df, ['expense_date', 'payable_date', 'liquidated_date', 'return_date'])

                # Defaults / audit
                if 'description' not in df.columns:
                    df['description'] = 'General'
                df['company_id'] = company_id
                df['created_by_id'] = user_id
                df['updated_by_id'] = user_id
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if 'created_at' not in df.columns:
                    df['created_at'] = now
                if 'updated_at' not in df.columns:
                    df['updated_at'] = now
                df['is_archived'] = 0

                # Expense number: preserve existing; generate if missing
                prefix = datetime.now().strftime('EXP%y-%m-')
                latest = Expense.objects.filter(expense_number__startswith=prefix) \
                                        .aggregate(Max('expense_number'))['expense_number__max']
                db_max = int(str(latest).rsplit('-', 1)[-1]) if latest else 0

                if 'expense_number' not in df.columns:
                    df['expense_number'] = ''
                df['expense_number'] = df['expense_number'].fillna('').astype(str).str.strip()

                csv_existing = df.loc[df['expense_number'].str.startswith(prefix), 'expense_number']
                if not csv_existing.empty:
                    try:
                        csv_max = pd.to_numeric(
                            csv_existing.str.rsplit('-', n=1, expand=True)[1],
                            errors='coerce'
                        ).dropna().astype(int).max()
                    except Exception:
                        csv_max = 0
                else:
                    csv_max = 0

                seq = max(db_max, csv_max)
                missing_mask = df['expense_number'] == ''
                for idx in df.index[missing_mask]:
                    seq += 1
                    df.at[idx, 'expense_number'] = f"{prefix}{seq:04d}"

                # Build objects
                valid_fields = {f.name for f in Expense._meta.fields}
                existing_numbers = set(
                    Expense.objects.filter(company_id=company_id).values_list('expense_number', flat=True)
                )
                records = []
                for row in df.to_dict(orient='records'):
                    if row.get('expense_number') in existing_numbers:
                        continue
                    try:
                        row['company'] = Company.objects.get(pk=company_id)
                        row['project'] = Project.objects.get(pk=row['project_id'])
                        row['created_by'] = User.objects.get(pk=user_id)
                        row['updated_by'] = User.objects.get(pk=user_id)

                        # Resolve FKs by id if present; otherwise fallback to id=1
                        row['recipient'] = Recipient.objects.get(pk=row.get('recipient_id') or 1)
                        row['expense_type'] = ExpenseType.objects.get(pk=row.get('expense_type_id') or 1)
                        row['expense_subject'] = ExpenseSubject.objects.get(pk=row.get('expense_subject_id') or 1)
                        row['expense_status'] = ExpenseStatus.objects.get(pk=row.get('expense_status_id') or 1)

                        # Drop *_id passthroughs
                        for key in [
                            'project_id', 'created_by_id', 'updated_by_id',
                            'recipient_id', 'expense_type_id', 'expense_subject_id', 'expense_status_id',
                            'company_id'
                        ]:
                            row.pop(key, None)

                        row = {k: v for k, v in row.items() if k in valid_fields}
                        records.append(Expense(**row))
                    except Exception as e:
                        print(f"⚠️ Skipped expense row due to error: {e}")

                if records:
                    Expense.objects.bulk_create(records)
                imported_expenses = len(records)
                request.session['full_expenses_imported'] = True

        except Exception as e:
            messages.error(request, f"❌ Expense import failed: {e}")

    # Summary messages
    if imported_invoices:
        messages.success(request, f"✅ Imported {imported_invoices} invoices successfully.")
    if imported_expenses:
        messages.success(request, f"✅ Imported {imported_expenses} expenses successfully.")
    if not imported_invoices and not imported_expenses:
        messages.error(request, "❌ No data was imported. Please check your file.")

    return redirect('consolidated_full')



# --------------------------
# 8) INDIVIDUAL CSV DOWNLOADS (Full Consolidation)
# --------------------------
def download_projects_csv(request):
    csv_data = request.session.get('full_projects_csv')
    if not csv_data:
        messages.error(request, "No project data available to download.")
        return redirect('consolidated_full')
    resp = HttpResponse(csv_data, content_type="text/csv")
    resp['Content-Disposition'] = 'attachment; filename="projects.csv"'
    return resp

def download_invoices_csv(request):
    csv_data = request.session.get('full_invoices_csv')
    if not csv_data:
        messages.error(request, "No invoice data available to download.")
        return redirect('consolidated_full')
    resp = HttpResponse(csv_data, content_type="text/csv")
    resp['Content-Disposition'] = 'attachment; filename="invoices.csv"'
    return resp

def download_expenses_csv(request):
    csv_data = request.session.get('full_expenses_csv')
    if not csv_data:
        messages.error(request, "No expense data available to download.")
        return redirect('consolidated_full')
    resp = HttpResponse(csv_data, content_type="text/csv")
    resp['Content-Disposition'] = 'attachment; filename="expenses.csv"'
    return resp

# Utility: clean dates for Django ORM
def clean_date_columns(df, columns):
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')
            df[col] = df[col].apply(lambda x: x.strftime('%Y-%m-%d') if pd.notnull(x) else None)
    return df

# Utility: safe numeric columns
def safe_numeric(df, columns):
    for col in columns:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    return df

# Utility: clean IDs (replace NaN with None)
def clean_ids(df):
    if 'id' in df.columns:
        df['id'] = df['id'].apply(lambda x: int(x) if pd.notna(x) else None)
    return df

# --------------------------
# 9) INDIVIDUAL IMPORTS (Full Consolidation)
# --------------------------
# --------------------------
# Import Projects Only
# --------------------------
def import_projects_only(request):
    csv_data = request.session.get('full_projects_csv')
    invoices_csv = request.session.get('full_invoices_csv')  # ✅ Restore invoice preview
    expenses_csv = request.session.get('full_expenses_csv')  # ✅ Restore expense preview

    if not csv_data:
        messages.error(request, "No project data to import.")
        return redirect('consolidated_full')

    df_proj = pd.read_csv(io.StringIO(csv_data))
    safe_numeric(df_proj, ['Amount', 'Estimated Expenses'])
    clean_ids(df_proj)

    imported = 0
    all_existing = True

    for _, row in df_proj.iterrows():
        proj_obj, created = Project.objects.get_or_create(
            name=row['Project Name'],
            company_id=request.session.get('company_id'),
            defaults={
                'project_number': row['Project Number'],
                'client_information_id': 151,
                'project_status_id': 1,
                'amount': row.get('Amount', 0),
                'estimated_expenses': row.get('Estimated Expenses', 0),
                'created_by_id': request.session.get('user_id'),
            }
        )
        if created:
            imported += 1
            all_existing = False

    request.session['projects_imported'] = imported > 0
    request.session['all_projects_existing'] = all_existing
    messages.success(request, f"Imported {imported} new projects.")

    # ✅ Preserve previews
    request.session['full_projects_preview'] = df_proj.head(20).to_html(classes="table table-striped", index=False)
    request.session['full_projects_imported'] = True
    if invoices_csv:
        df_inv = pd.read_csv(io.StringIO(invoices_csv))
        request.session['full_invoices_preview'] = df_inv.head(20).to_html(classes="table table-striped", index=False)
    if expenses_csv:
        df_exp = pd.read_csv(io.StringIO(expenses_csv))
        request.session['full_expenses_preview'] = df_exp.head(20).to_html(classes="table table-striped", index=False)

    return render(request, 'consolidated_full.html', {
        'preview_project_html': request.session.get('full_projects_preview', ''),
        'preview_invoice_html': request.session.get('full_invoices_preview', ''),
        'preview_expense_html': request.session.get('full_expenses_preview', ''),
        'full_projects_imported': True,
        'full_invoices_imported': request.session.get('full_invoices_imported', False),
        'full_expenses_imported': request.session.get('full_expenses_imported', False),
    })

# --------------------------
# Import Invoices Only
# --------------------------
def import_invoices_only(request):
    inv_csv      = request.session.get('full_invoices_csv')
    projects_csv = request.session.get('full_projects_csv')   # ✅ for restoring project preview
    expenses_csv = request.session.get('full_expenses_csv')   # ✅ for restoring expense preview
    company_id   = request.session.get('company_id')
    user_id      = request.session.get('user_id')

    if not inv_csv or not company_id or not user_id:
        messages.error(request, "Missing invoice file or session context.")
        return redirect('consolidated_full')

    try:
        # 1) Load preview CSV
        df_inv = pd.read_csv(io.StringIO(inv_csv))

        # 2) Route shared Date -> invoice_date, then drop Date (avoid dup keys)
        if 'invoice_date' not in df_inv.columns and 'Date' in df_inv.columns:
            df_inv['invoice_date'] = pd.to_datetime(df_inv['Date'], errors='coerce')
        if 'Date' in df_inv.columns:
            df_inv.drop(columns=['Date'], inplace=True)

        # 3) Normalize headers (do NOT map Date again)
        rename_map = {
            'No': 'invoice_number',
            'Amount': 'amount',
            'Received_Amount': 'received_amount',
            'Received Amount': 'received_amount',
            'Invoice_Date': 'invoice_date',
            'Due_Date': 'due_date',
            'Date_Paid': 'date_paid',
            'Project_Number': 'project_number',
            'Project': 'project_name',
        }
        df_inv.rename(columns={k: v for k, v in rename_map.items() if k in df_inv.columns}, inplace=True)

        # 4) Merge & drop duplicate column names (keep first)
        if df_inv.columns.duplicated().any():
            for col in pd.unique(df_inv.columns[df_inv.columns.duplicated(keep=False)]):
                same = df_inv.loc[:, df_inv.columns == col]
                df_inv[col] = same.bfill(axis=1).iloc[:, 0]
            df_inv = df_inv.loc[:, ~df_inv.columns.duplicated(keep='first')]

        # 5) Project mapping (prefer number; else name)
        if 'project_number' in df_inv.columns:
            df_inv['project_number'] = df_inv['project_number'].astype(str).str.strip().str.upper()
            project_map = {
                number.upper(): pid
                for number, pid in Project.objects.filter(company_id=company_id).values_list('project_number', 'id')
            }
            df_inv['project_id'] = df_inv['project_number'].map(project_map)
        else:
            if 'project_name' not in df_inv.columns:
                messages.error(request, "Missing project reference (project number or name).")
                return redirect('consolidated_full')
            df_inv['project_name'] = df_inv['project_name'].astype(str).str.strip().str.upper()
            proj_name_map = {
                name.strip().upper(): pid
                for name, pid in Project.objects.filter(company_id=company_id).values_list('name', 'id')
            }
            df_inv['project_id'] = df_inv['project_name'].map(proj_name_map)

        before = len(df_inv)
        df_inv = df_inv[df_inv['project_id'].notna()].copy()
        after = len(df_inv)
        print(f"📊 Rows after project mapping: {after} / {before}")
        if df_inv.empty:
            messages.error(request, "No invoices matched a project. Check project numbers/names.")
            return redirect('consolidated_full')
        df_inv['project_id'] = df_inv['project_id'].astype(int)

        # 6) Amounts (strip commas)
        if 'amount' in df_inv.columns:
            df_inv['amount'] = pd.to_numeric(df_inv['amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
        else:
            df_inv['amount'] = 0
        if 'received_amount' in df_inv.columns:
            df_inv['received_amount'] = pd.to_numeric(df_inv['received_amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

        # 7) Dates
        df_inv = clean_date_columns(df_inv, ['invoice_date', 'due_date', 'date_paid'])

        # 8) Status (set FK id; drop text column to avoid assigning 'Pending' to FK)
        status_obj = InvoiceStatus.objects.filter(name__iexact='Pending').first()
        if not status_obj:
            messages.error(request, "Missing default invoice status.")
            return redirect('consolidated_full')
        df_inv['invoice_status_id'] = status_obj.id
        df_inv.drop(columns=['invoice_status'], errors='ignore', inplace=True)

        # 9) Preserve existing invoice_number; fill missing after max(DB, CSV)
        prefix = datetime.now().strftime('INV%y-%m-')
        latest = Invoice.objects.filter(invoice_number__startswith=prefix) \
                                .aggregate(Max('invoice_number'))['invoice_number__max']
        db_max = int(str(latest).rsplit('-', 1)[-1]) if latest else 0

        if 'invoice_number' not in df_inv.columns:
            df_inv['invoice_number'] = ''
        df_inv['invoice_number'] = df_inv['invoice_number'].fillna('').astype(str).str.strip()

        csv_existing = df_inv.loc[df_inv['invoice_number'].str.startswith(prefix), 'invoice_number']
        if not csv_existing.empty:
            csv_suffix = (
                csv_existing.str.rsplit('-', n=1, expand=True)[1]
                .apply(lambda s: pd.to_numeric(s, errors='coerce'))
                .dropna()
                .astype(int)
            )
            csv_max = int(csv_suffix.max()) if not csv_suffix.empty else 0
        else:
            csv_max = 0

        seq = max(db_max, csv_max)
        missing_mask = df_inv['invoice_number'] == ''
        for idx in df_inv.index[missing_mask]:
            seq += 1
            df_inv.at[idx, 'invoice_number'] = f"{prefix}{seq:04d}"

        # 10) Audit/context
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if 'created_at' not in df_inv.columns:
            df_inv['created_at'] = now
        if 'updated_at' not in df_inv.columns:
            df_inv['updated_at'] = now
        df_inv['is_archived'] = 0
        df_inv['created_by_id'] = user_id
        df_inv['updated_by_id'] = user_id

        # 11) Whitelist fields + temp FK ids (include *_id extras)
        valid_fields = {f.name for f in Invoice._meta.fields}
        extra_ok = {'project_id', 'created_by_id', 'updated_by_id', 'invoice_status_id'}
        df_inv = df_inv[[c for c in df_inv.columns if c in valid_fields | extra_ok]]

        # 12) Build model objects (NO company on Invoice; filter dupes via project__company)
        existing_numbers = set(
            Invoice.objects.filter(project__company_id=company_id)
                           .values_list('invoice_number', flat=True)
        )
        records = []
        for row in df_inv.to_dict(orient='records'):
            if row.get('invoice_number') in existing_numbers:
                continue
            try:
                row['project'] = Project.objects.get(pk=row['project_id'])
                row['created_by'] = User.objects.get(pk=user_id)
                row['updated_by'] = User.objects.get(pk=user_id)
                for k in ('project_id', 'created_by_id', 'updated_by_id'):
                    row.pop(k, None)
                row = {k: v for k, v in row.items() if k in (valid_fields | {'invoice_status_id'})}
                records.append(Invoice(**row))
            except Exception as e:
                print(f"⚠️ Skipped row due to error: {e}")

        print(f"📦 About to insert: {len(records)} records")
        if not records:
            messages.error(request, "No invoice rows to insert (all duplicates or invalid).")

        else:
            Invoice.objects.bulk_create(records, ignore_conflicts=True)
            messages.success(request, f"✅ Imported {len(records)} Invoices successfully.")

        # ✅ Preserve previews in session so they survive redirect
        try:
            df_inv_preview = pd.read_csv(io.StringIO(inv_csv))
            request.session['full_invoices_preview'] = df_inv_preview.head(20).to_html(classes="table table-striped", index=False)
            request.session['full_invoices_imported'] = True
        except Exception:
            pass

        if projects_csv:
            try:
                df_proj = pd.read_csv(io.StringIO(projects_csv))
                request.session['full_projects_preview'] = df_proj.head(20).to_html(classes="table table-striped", index=False)
            except Exception:
                pass

        if expenses_csv:
            try:
                df_exp = pd.read_csv(io.StringIO(expenses_csv))
                request.session['full_expenses_preview'] = df_exp.head(20).to_html(classes="table table-striped", index=False)
            except Exception:
                pass

    except Exception as e:
        messages.error(request, f"❌ Invoice import failed: {str(e)}")

    return redirect('consolidated_full')



# --------------------------
# Import Expenses Only
# --------------------------
def import_expenses_only(request):
    exp_csv      = request.session.get('full_expenses_csv')
    projects_csv = request.session.get('full_projects_csv')  # restore project preview
    invoices_csv = request.session.get('full_invoices_csv')  # restore invoice preview
    company_id   = request.session.get('company_id')
    user_id      = request.session.get('user_id')

    if not exp_csv or not company_id or not user_id:
        messages.error(request, "Missing expense file or session context.")
        return redirect('consolidated_full')

    try:
        # 1) Load CSV from preview
        df = pd.read_csv(io.StringIO(exp_csv))

        # 2) Normalize headers from preview (fix casing/mismatches)
        rename_map = {
            'Amount': 'amount',
            'Expense_Date': 'expense_date',
            'Payable_Date': 'payable_date',
            'Liquidated_Amount': 'liquidated_amount',
            'Liquidated_Date': 'liquidated_date',
            'Return_Date': 'return_date',
            'Expense_Number': 'expense_number',
            'Project_Number': 'project_number',
            'Description': 'description',
        }
        df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)

        # 3) Numbers & strings cleanup
        if 'amount' in df.columns:
            df['amount'] = pd.to_numeric(df['amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
        else:
            df['amount'] = 0
        if 'liquidated_amount' in df.columns:
            df['liquidated_amount'] = pd.to_numeric(df['liquidated_amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

        # 4) Map project_number -> project_id
        df['project_number'] = df['project_number'].astype(str).str.strip().str.upper()
        project_map = {
            number.upper(): pid
            for number, pid in Project.objects.filter(company_id=company_id).values_list('project_number', 'id')
        }
        df['project_id'] = df['project_number'].map(project_map)
        df = df[df['project_id'].notna()].copy()
        df['project_id'] = df['project_id'].astype(int)

        # 5) Clean dates
        df = clean_date_columns(df, ['expense_date', 'payable_date', 'liquidated_date', 'return_date'])

        # 6) Defaults / context
        if 'description' not in df.columns:
            df['description'] = 'General'
        df['company_id'] = company_id
        df['created_by_id'] = user_id
        df['updated_by_id'] = user_id
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if 'created_at' not in df.columns:
            df['created_at'] = now
        if 'updated_at' not in df.columns:
            df['updated_at'] = now
        df['is_archived'] = 0

        # 7) Preserve existing expense_number; only fill missing after DB/CSV max
        prefix = datetime.now().strftime('EXP%y-%m-')
        latest = Expense.objects.filter(expense_number__startswith=prefix) \
                                .aggregate(Max('expense_number'))['expense_number__max']
        db_max = int(str(latest).rsplit('-', 1)[-1]) if latest else 0

        if 'expense_number' not in df.columns:
            df['expense_number'] = ''
        df['expense_number'] = df['expense_number'].fillna('').astype(str).str.strip()

        csv_existing = df.loc[df['expense_number'].str.startswith(prefix), 'expense_number']
        if not csv_existing.empty:
            try:
                csv_max = pd.to_numeric(
                    csv_existing.str.rsplit('-', n=1, expand=True)[1],
                    errors='coerce'
                ).dropna().astype(int).max()
            except Exception:
                csv_max = 0
        else:
            csv_max = 0

        seq = max(db_max, csv_max)
        missing_mask = df['expense_number'] == ''
        for idx in df.index[missing_mask]:
            seq += 1
            df.at[idx, 'expense_number'] = f"{prefix}{seq:04d}"

        # 8) Final FK object assignments + dedupe
        valid_fields = {f.name for f in Expense._meta.fields}
        existing_numbers = set(
            Expense.objects.filter(company_id=company_id).values_list('expense_number', flat=True)
        )
        records = []
        for row in df.to_dict(orient='records'):
            if row.get('expense_number') in existing_numbers:
                print(f"⚠️ Skipped duplicate: {row['expense_number']}")
                continue
            try:
                row['company'] = Company.objects.get(pk=company_id)
                row['project'] = Project.objects.get(pk=row['project_id'])
                row['created_by'] = User.objects.get(pk=user_id)
                row['updated_by'] = User.objects.get(pk=user_id)

                row['recipient'] = Recipient.objects.get(pk=row.get('recipient_id') or 1)
                row['expense_type'] = ExpenseType.objects.get(pk=row.get('expense_type_id') or 1)
                row['expense_subject'] = ExpenseSubject.objects.get(pk=row.get('expense_subject_id') or 1)
                row['expense_status'] = ExpenseStatus.objects.get(pk=row.get('expense_status_id') or 1)

                # Drop *_id keys; keep FK objects
                for key in [
                    'project_id', 'created_by_id', 'updated_by_id',
                    'recipient_id', 'expense_type_id', 'expense_subject_id', 'expense_status_id',
                    'company_id'
                ]:
                    row.pop(key, None)

                row = {k: v for k, v in row.items() if k in valid_fields}
                records.append(Expense(**row))
            except Exception as e:
                print(f"⚠️ Skipped row due to error: {e}")

        if records:
            Expense.objects.bulk_create(records)
        messages.success(request, f"✅ Imported {len(records)} Expenses successfully.")

        # ✅ Preserve previews using the keys consolidated_full expects
        try:
            request.session['preview_expenses_html'] = pd.read_csv(io.StringIO(exp_csv)).head(20).to_html(
                classes="table table-striped", index=False
            )
            request.session['full_expenses_imported'] = True
        except Exception:
            pass

        if projects_csv:
            try:
                request.session['preview_projects_html'] = pd.read_csv(io.StringIO(projects_csv)).head(20).to_html(
                    classes="table table-striped", index=False
                )
            except Exception:
                pass

        if invoices_csv:
            try:
                request.session['preview_invoices_html'] = pd.read_csv(io.StringIO(invoices_csv)).head(20).to_html(
                    classes="table table-striped", index=False
                )
            except Exception:
                pass

    except Exception as e:
        messages.error(request, f"❌ Expense import failed: {str(e)}")

    return redirect('consolidated_full')


# --------------------------
# INVOICE + EXPENSE (IE) DOWNLOAD/IMPORT
# --------------------------
def download_consolidated_csv(request):
    csv_data = request.session.get('consolidated_csv')
    if not csv_data:
        messages.error(request, "No consolidated file available to download.")
        return redirect('consolidated_ie')
    resp = HttpResponse(csv_data, content_type="text/csv")
    resp['Content-Disposition'] = 'attachment; filename="consolidated_upload.csv"'
    return resp

@require_POST
def import_consolidated_csv(request):
    invoice_csv = request.session.get('ie_invoices_csv')
    expense_csv = request.session.get('ie_expenses_csv')

    project_id = request.session.get('project_id')
    company_id = request.session.get('company_id')
    user_id = request.session.get('user_id')

    if not project_id or not company_id or not user_id:
        messages.error(request, "Missing session information. Please re-upload your file.")
        return redirect('consolidated_ie')

    invoice_success = 0
    expense_success = 0

    if invoice_csv:
        try:
            df_inv = pd.read_csv(io.StringIO(invoice_csv))
            for col in ['amount', 'received_amount']:
                if col in df_inv.columns:
                    df_inv[col] = pd.to_numeric(df_inv[col], errors='coerce').replace({np.nan: None})
            for col in ['invoice_date', 'due_date', 'date_paid']:
                if col in df_inv.columns:
                    df_inv[col] = pd.to_datetime(df_inv[col], errors='coerce')
                    df_inv[col] = df_inv[col].dt.date.where(df_inv[col].notna(), None)

            df_inv = populate_ids(
                df_inv,
                table='invoice',
                project_id=project_id,
                company_id=company_id,
                user_id=user_id
            )

            valid_fields = {f.name for f in Invoice._meta.fields}
            cleaned_invoices = []
            existing_invoice_numbers = set(Invoice.objects.values_list('invoice_number', flat=True))

            for row in df_inv.to_dict(orient='records'):
                if row.get('invoice_number') in existing_invoice_numbers:
                    continue
                row['company'] = Company.objects.get(pk=company_id)
                row['project'] = Project.objects.get(pk=project_id)
                row['created_by'] = User.objects.get(pk=user_id)
                row['updated_by'] = User.objects.get(pk=user_id)
                row['invoice_status'] = (
                    InvoiceStatus.objects.get(pk=row['invoice_status_id'])
                    if row.get('invoice_status_id')
                    else InvoiceStatus.objects.get(pk=1)
                )
                record = {k: v for k, v in row.items() if k in valid_fields}
                cleaned_invoices.append(Invoice(**record))

            if cleaned_invoices:
                Invoice.objects.bulk_create(cleaned_invoices)
            invoice_success = len(cleaned_invoices)

            # (Optional) Set preview for immediate render, but we'll clear before redirect anyway
            request.session['full_invoices_preview'] = df_inv.head(20).to_html(classes="table table-striped", index=False)
            request.session['full_invoices_imported'] = True

        except Exception as e:
            messages.error(request, f"❌ Invoice import failed: {e}")

    if expense_csv:
        try:
            df_exp = pd.read_csv(io.StringIO(expense_csv))
            for col in ['amount', 'liquidated_amount']:
                if col in df_exp.columns:
                    df_exp[col] = pd.to_numeric(df_exp[col], errors='coerce').replace({np.nan: None})
            for col in ['expense_date', 'payable_date', 'liquidated_date', 'return_date']:
                if col in df_exp.columns:
                    df_exp[col] = pd.to_datetime(df_exp[col], errors='coerce')
                    df_exp[col] = df_exp[col].dt.date.where(df_exp[col].notna(), None)

            df_exp = populate_ids(
                df_exp,
                table='expense',
                project_id=project_id,
                company_id=company_id,
                user_id=user_id
            )

            valid_fields = {f.name for f in Expense._meta.fields}
            cleaned_expenses = []
            existing_expense_numbers = set(Expense.objects.values_list('expense_number', flat=True))

            for row in df_exp.to_dict(orient='records'):
                if row.get('expense_number') in existing_expense_numbers:
                    continue
                row['company'] = Company.objects.get(pk=company_id)
                row['project'] = Project.objects.get(pk=project_id)
                row['created_by'] = User.objects.get(pk=user_id)
                row['updated_by'] = User.objects.get(pk=user_id)
                row['expense_status'] = (
                    ExpenseStatus.objects.get(pk=row['expense_status_id'])
                    if row.get('expense_status_id')
                    else ExpenseStatus.objects.get(pk=1)
                )
                row['recipient'] = (
                    Recipient.objects.get(pk=row['recipient_id'])
                    if row.get('recipient_id')
                    else Recipient.objects.get(pk=1)
                )
                row['expense_type'] = (
                    ExpenseType.objects.get(pk=row['expense_type_id'])
                    if row.get('expense_type_id')
                    else ExpenseType.objects.get(pk=1)
                )
                row['expense_subject'] = (
                    ExpenseSubject.objects.get(pk=row['expense_subject_id'])
                    if row.get('expense_subject_id')
                    else ExpenseSubject.objects.get(pk=1)
                )
                record = {k: v for k, v in row.items() if k in valid_fields}
                cleaned_expenses.append(Expense(**record))

            if cleaned_expenses:
                Expense.objects.bulk_create(cleaned_expenses)
            expense_success = len(cleaned_expenses)

            # (Optional) Set preview for immediate render, but we'll clear before redirect anyway
            request.session['full_expenses_preview'] = df_exp.head(20).to_html(classes="table table-striped", index=False)
            request.session['full_expenses_imported'] = True

        except Exception as e:
            messages.error(request, f"❌ Expense import failed: {e}")

    if invoice_success:
        messages.success(request, f"✅ Imported {invoice_success} invoices successfully.")
    if expense_success:
        messages.success(request, f"✅ Imported {expense_success} expenses successfully.")
    if not invoice_success and not expense_success:
        messages.error(request, "❌ No data was imported. Please check your file.")

    # 🔁 Clear IE session state and PRG redirect to prevent POST replay on second upload
    for k in (
        'full_invoices_preview', 'full_expenses_preview',
        'preview_invoice_html', 'preview_expense_html',
        'ie_invoices_csv', 'ie_expenses_csv', 'consolidated_csv',
        'warnings_invoice', 'warnings_expense',
        'full_invoices_imported', 'full_expenses_imported',
    ):
        request.session.pop(k, None)

    return redirect('consolidated_ie')


# --- Individual Imports for IE ---
@require_POST
def import_ie_expenses_only(request):
    expense_csv = request.session.get('ie_expenses_csv')
    invoice_csv = request.session.get('ie_invoices_csv')  # ✅ Add this to restore invoice preview

    project_id = request.session.get('project_id')
    company_id = request.session.get('company_id')
    user_id = request.session.get('user_id')

    if not expense_csv:
        messages.error(request, "No expense data to import.")
        return redirect('consolidated_ie')

    if not project_id or not company_id or not user_id:
        messages.error(request, "Missing project, company, or user session info. Please re-upload and try again.")
        return redirect('consolidated_ie')

    df = pd.read_csv(io.StringIO(expense_csv))

    for col in ['amount', 'liquidated_amount']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').replace({np.nan: None})

    for col in ['expense_date', 'payable_date', 'liquidated_date', 'return_date']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')
            df[col] = df[col].dt.date.where(df[col].notna(), None)

    df = populate_ids(df, table='expense', project_id=project_id, company_id=company_id, user_id=user_id)

    valid_fields = {f.name for f in Expense._meta.fields}
    cleaned_records = []
    existing_expense_numbers = set(Expense.objects.values_list('expense_number', flat=True))

    for row in df.to_dict(orient='records'):
        if row.get('expense_number') in existing_expense_numbers:
            print(f"⚠️ Skipped duplicate expense number: {row.get('expense_number')}")
            continue
        try:
            row['company'] = Company.objects.get(pk=company_id)
            row['project'] = Project.objects.get(pk=project_id)
            row['created_by'] = User.objects.get(pk=user_id)
            row['updated_by'] = User.objects.get(pk=user_id)

            row['recipient'] = Recipient.objects.get(pk=row['recipient_id']) if row.get('recipient_id') else Recipient.objects.get(pk=1)
            row['expense_type'] = ExpenseType.objects.get(pk=row['expense_type_id']) if row.get('expense_type_id') else ExpenseType.objects.get(pk=1)
            row['expense_subject'] = ExpenseSubject.objects.get(pk=row['expense_subject_id']) if row.get('expense_subject_id') else ExpenseSubject.objects.get(pk=1)
            row['expense_status'] = ExpenseStatus.objects.get(pk=row['expense_status_id']) if row.get('expense_status_id') else ExpenseStatus.objects.get(pk=1)

            record = {k: v for k, v in row.items() if k in valid_fields}
            cleaned_records.append(Expense(**record))
        except Exception as e:
            print(f"⚠️ Skipped expense row due to error: {e}")

    Expense.objects.bulk_create(cleaned_records)
    messages.success(request, f"✅ Imported {len(cleaned_records)} expenses successfully.")

    # ✅ Re-generate preview tables for BOTH
    request.session['full_expenses_preview'] = df.head(20).to_html(classes="table table-striped", index=False)
    request.session['full_expenses_imported'] = True

    # ✅ Re-generate invoice preview if invoice_csv is still present
    if invoice_csv:
        df_inv = pd.read_csv(io.StringIO(invoice_csv))
        request.session['full_invoices_preview'] = df_inv.head(20).to_html(classes="table table-striped", index=False)

    return render(request, 'consolidated_ie.html', {
        'preview_invoice_html': request.session.get('full_invoices_preview', ''),
        'preview_expense_html': request.session.get('full_expenses_preview', ''),
        'warnings_invoice': [],
        'warnings_expense': [],
        'full_invoices_imported': request.session.get('full_invoices_imported', False),
        'full_expenses_imported': True,
    })


@require_POST
def import_ie_invoices_only(request):
    invoice_csv = request.session.get('ie_invoices_csv')
    expense_csv = request.session.get('ie_expenses_csv')  # NEW

    project_id = request.session.get('project_id')
    company_id = request.session.get('company_id')
    user_id = request.session.get('user_id')

    if not invoice_csv:
        messages.error(request, "No invoice data to import.")
        return redirect('consolidated_ie')

    if not project_id or not company_id or not user_id:
        messages.error(request, "Missing project, company, or user session info. Please re-upload and try again.")
        return redirect('consolidated_ie')

    df = pd.read_csv(io.StringIO(invoice_csv))

    for col in ['amount', 'received_amount']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').replace({np.nan: None})

    for col in ['invoice_date', 'due_date', 'date_paid']:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors='coerce')
            df[col] = df[col].dt.date.where(df[col].notna(), None)

    df = populate_ids(df, table='invoice', project_id=project_id, company_id=company_id, user_id=user_id)

    valid_fields = {f.name for f in Invoice._meta.fields}
    cleaned_records = []
    existing_invoice_numbers = set(Invoice.objects.values_list('invoice_number', flat=True))

    for row in df.to_dict(orient='records'):
        if row.get('invoice_number') in existing_invoice_numbers:
            print(f"⚠️ Skipped duplicate invoice number: {row.get('invoice_number')}")
            continue
        try:
            row['company'] = Company.objects.get(pk=company_id)
            row['project'] = Project.objects.get(pk=project_id)
            row['created_by'] = User.objects.get(pk=user_id)
            row['updated_by'] = User.objects.get(pk=user_id)

            row['invoice_status'] = InvoiceStatus.objects.get(pk=row['invoice_status_id']) if row.get('invoice_status_id') else InvoiceStatus.objects.get(pk=1)

            record = {k: v for k, v in row.items() if k in valid_fields}
            cleaned_records.append(Invoice(**record))
        except Exception as e:
            print(f"⚠️ Skipped invoice row due to error: {e}")

    Invoice.objects.bulk_create(cleaned_records)
    messages.success(request, f"✅ Imported {len(cleaned_records)} invoices successfully.")

    # ✅ Re-generate preview tables for BOTH
    request.session['full_invoices_preview'] = df.head(20).to_html(classes="table table-striped", index=False)
    request.session['full_invoices_imported'] = True

    # ✅ Re-generate preview of expenses IF present
    if expense_csv:
        df_exp = pd.read_csv(io.StringIO(expense_csv))
        request.session['full_expenses_preview'] = df_exp.head(20).to_html(classes="table table-striped", index=False)

    return render(request, 'consolidated_ie.html', {
        'preview_invoice_html': request.session.get('full_invoices_preview', ''),
        'preview_expense_html': request.session.get('full_expenses_preview', ''),
        'warnings_invoice': [],
        'warnings_expense': [],
        'full_invoices_imported': True,
        'full_expenses_imported': request.session.get('full_expenses_imported', False),
    })

def download_ie_invoices_csv(request):
    csv_data = request.session.get('ie_invoices_csv')
    if not csv_data:
        messages.error(request, "No invoice data available to download.")
        return redirect('consolidated_ie')
    resp = HttpResponse(csv_data, content_type="text/csv")
    resp['Content-Disposition'] = 'attachment; filename="ie_invoices.csv"'
    return resp

def download_ie_expenses_csv(request):
    csv_data = request.session.get('ie_expenses_csv')
    if not csv_data:
        messages.error(request, "No expense data available to download.")
        return redirect('consolidated_ie')
    resp = HttpResponse(csv_data, content_type="text/csv")
    resp['Content-Disposition'] = 'attachment; filename="ie_expenses.csv"'
    return resp




