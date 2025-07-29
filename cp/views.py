# views.py
import io
import re
import pandas as pd
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import HttpResponse
from passlib.hash import bcrypt as passlib_bcrypt

from .models import User, Company, Project, Invoice, Expense
from .clean_headers import clean_and_detect
from .populate_ids import populate_ids
from .clean import clean_invoice_data, clean_expense_data


def login_view(request):
    if request.method == 'POST':
        uname = request.POST.get('username')
        pwd   = request.POST.get('password')
        try:
            user = User.objects.get(username=uname)
        except User.DoesNotExist:
            messages.error(request, "Invalid username or password")
            return render(request, 'login.html')

        if passlib_bcrypt.verify(pwd, user.password):
            request.session.flush()
            request.session['user_id'] = user.id
            return redirect('select_company')

        messages.error(request, "Invalid username or password")
    return render(request, 'login.html')


def select_company(request):
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        request.session.pop('user_id', None)
        return redirect('login')

    companies = Company.objects.filter(id=user.company_id)
    projects  = Project.objects.filter(company_id=user.company_id)

    if request.method == 'POST':
        request.session['company_id'] = int(request.POST['company_id'])
        request.session['project_id'] = int(request.POST['project_id'])
        return redirect('upload')

    return render(request, 'select_company.html', {
        'user':      user,
        'companies': companies,
        'projects':  projects,
    })


def upload_view(request):
    preview_html   = None
    detected_table = None
    mapping_log    = []
    warnings_list  = []

    if request.method == 'POST' and request.FILES.get('raw_file'):
        raw       = request.FILES['raw_file']
        data      = raw.read()
        buf       = io.BytesIO(data)
        ext       = raw.name.rsplit('.', 1)[-1].lower()

        try:
            if ext in ('xls', 'xlsx'):
                df_try = pd.read_excel(buf, engine='openpyxl')
            else:
                df_try = pd.read_csv(io.BytesIO(data))
        except Exception as e:
            messages.error(request, f"Error reading file: {e}")
            return render(request, 'upload.html')

        # --- detect headerless ---
        cols = df_try.columns.tolist()
        def is_data(c):
            s = str(c).strip()
            return bool(
                re.fullmatch(r"\d+(?:\.\d+)?", s) or
                re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", s)
            )
        if sum(is_data(c) for c in cols) > len(cols)/2:
            messages.error(request, "No header row detected. Please upload a file with headers.")
            return render(request, 'upload.html', {
                'preview_html': None,
                'mapping_log':  [],
                'warnings':     []
            })

        # --- clean headers & detect ---
        buf.seek(0)
        detected_table, df_clean, mapping_log = clean_and_detect(df_try.copy())

        # --- check for unrelated columns ---
        expected_expense_cols = {
            'Recipient', 'Type', 'Subject', 'Status', 'Desc', 'Amt', 'ExpDt',
            'PayDt', 'LiqAmt', 'LiqDt', 'RetDt', 'File', 'Arch'
        }
        expected_invoice_cols = {
            'Amt', 'Received', 'Inv Dt', 'Due Dt', 'Paid On',
            'Inv File', 'Pay File', 'Status', 'Arch'
        }
        expected = expected_invoice_cols if detected_table == 'invoice' else expected_expense_cols
        unexpected_cols = [c for c in df_clean.columns if c not in expected]
        if unexpected_cols:
            messages.warning(request, f"⚠️ Unrelated columns detected: {', '.join(unexpected_cols)} — they will be ignored.")
            df_clean.drop(columns=unexpected_cols, inplace=True, errors='ignore')

        # --- rename/drop extras ---
        df_clean.rename(columns={
            'ExpDt': 'expense_date', 'PayDt': 'payable_date',
            'LiqAmt': 'liquidated_amount', 'LiqDt': 'liquidated_date',
            'RetDt': 'return_date', 'File': 'attachment', 'Arch': 'is_archive'
        }, inplace=True)
        df_clean.drop(columns=['description.1','project','project_name'], errors='ignore', inplace=True)

        # --- populate IDs ---
        df_final = populate_ids(
            df_clean,
            detected_table,
            project_id=request.session.get('project_id'),
            user_id=   request.session.get('user_id'),
            company_id=request.session.get('company_id')
        )

        # --- clean data & gather warnings ---
        if detected_table == 'invoice':
            df_final, warnings_list = clean_invoice_data(df_final)
        else:
            df_final, warnings_list = clean_expense_data(df_final)
        for w in warnings_list:
            messages.warning(request, w)

        # --- preview & session store ---
        preview_html = df_final.head(20).to_html(classes="table table-striped", index=False)
        s_buf       = io.StringIO()
        df_final.to_csv(s_buf, index=False)
        request.session['cleaned_csv']    = s_buf.getvalue()
        request.session['detected_table'] = detected_table

    return render(request, 'upload.html', {
        'preview_html':   preview_html,
        'mapping_log':    mapping_log,
        'warnings':       warnings_list,
        'detected_table': detected_table,
    })


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
        return redirect('upload')

    csv_str = request.session.pop('cleaned_csv', None)
    table   = request.session.pop('detected_table', None)
    if not csv_str or not table:
        messages.error(request, "Nothing to import. Please upload & preview first.")
        return redirect('upload')

    try:
        df = pd.read_csv(io.StringIO(csv_str))

        # drop any PK / auto-managed fields
        for col in list(df.columns):
            if col.lower() in ('id', 'created_at', 'updated_at'):
                df.drop(columns=[col], inplace=True)

        # rename archive flag
        if 'is_archived' in df.columns:
            df.rename(columns={'is_archived': 'is_archive'}, inplace=True)

        # nulls → None
        df = df.where(pd.notnull(df), None)

        # parse dates
        if table == 'invoice':
            date_cols = ['invoice_date','due_date','date_paid']
        else:
            date_cols = ['expense_date','payable_date','liquidated_date','return_date']

        for col in date_cols:
            if col in df.columns:
                df[col] = (
                    df[col]
                      .astype(str)
                      .str.replace(r'[“”"‘’]', '', regex=True)
                      .str.replace('/', '-', regex=False)
                      .str.strip()
                )
                df[col] = pd.to_datetime(df[col], dayfirst=True, errors='coerce')
                df[col] = df[col].apply(lambda x: x.to_pydatetime() if pd.notnull(x) else None)

        # build model instances
        Model = Invoice if table == 'invoice' else Expense
        records = df.to_dict(orient='records')
        objs = []
        for rec in records:
            rec.pop('id', None)
            objs.append(Model(**rec))

        Model.objects.bulk_create(objs)
        messages.success(request, f"Imported {len(objs)} rows into {table}.")
    except Exception as e:
        messages.error(request, f"Import failed: {e}")

    return redirect('upload')
