from django.urls import path
from .views import (
    # 1) Authentication & mode selection
    login_view,
    choose_mode,

    # 2) Consolidated flows
    consolidated_full,
    consolidated_ie,
    import_full_consolidated,
    import_projects_only,
    import_invoices_only,
    import_expenses_only,
    download_projects_csv,
    download_invoices_csv,
    download_expenses_csv,

    # 3) Consolidated CSV/XLSX (Invoice + Expense)
    download_consolidated_csv,
    import_consolidated_csv,
    download_ie_invoices_csv,
    download_ie_expenses_csv,
    import_ie_invoices_only,
    import_ie_expenses_only,

    # 4) Unconsolidated / standard flow
    select_company,
    upload_view,
    download_clean_csv,
    import_clean_csv,
)

urlpatterns = [
    # 1) Authentication
    path('',                login_view,          name='login'),
    path('login/',          login_view,          name='login_alt'),

    # 2) Mode selection page
    path('choose-mode/',    choose_mode,         name='choose_mode'),

    # 3) Consolidation flows
    path('consolidated-full/', consolidated_full, name='consolidated_full'),
    path('consolidated-ie/',   consolidated_ie,   name='consolidated_ie'),

    # 3a) Full consolidated imports
    path('import-full-consolidated/', import_full_consolidated, name='import_full_consolidated'),

    # 3b) Individual imports (Full Consolidation)
    path('import-projects/', import_projects_only, name='import_projects_only'),
    path('import-invoices/', import_invoices_only, name='import_invoices_only'),
    path('import-expenses/', import_expenses_only, name='import_expenses_only'),

    # 3c) Individual CSV downloads (Full Consolidation)
    path('download-projects/', download_projects_csv, name='download_projects_csv'),
    path('download-invoices/', download_invoices_csv, name='download_invoices_csv'),
    path('download-expenses/', download_expenses_csv, name='download_expenses_csv'),

    # 3d) Consolidated CSV/XLSX (Invoice + Expense) - Combined
    path('download-consolidated/', download_consolidated_csv, name='download_consolidated_csv'),
    path('import-consolidated/', import_consolidated_csv, name='import_consolidated_csv'),

    # 3e) Invoice + Expense (IE) - Individual CSV download & import
    path('download-ie-invoices/', download_ie_invoices_csv, name='download_ie_invoices_csv'),
    path('download-ie-expenses/', download_ie_expenses_csv, name='download_ie_expenses_csv'),
    path('import-ie-invoices/', import_ie_invoices_only, name='import_ie_invoices_only'),
    path('import-ie-expenses/', import_ie_expenses_only, name='import_ie_expenses_only'),

    # 4) Unconsolidated / standard flow
    path('select-company/', select_company,      name='select_company'),
    path('upload/',         upload_view,         name='upload'),

    # 5) Download & Import cleaned files (Unconsolidated)
    path('download-cleaned/', download_clean_csv, name='download_clean_csv'),
    path('import-cleaned/',   import_clean_csv,   name='import_clean_csv'),
]
