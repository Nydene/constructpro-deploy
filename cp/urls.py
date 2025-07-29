# cp/urls.py
from django.urls import path
from .views import (
    login_view,
    select_company,
    upload_view,
    download_clean_csv,
    import_clean_csv,
)

urlpatterns = [
    path('login/',            login_view,         name='login'),
    path('select-company/',   select_company,     name='select_company'),
    path('upload/',           upload_view,        name='upload'),
    path('download-cleaned/', download_clean_csv, name='download_clean_csv'),
    path('import-cleaned/',   import_clean_csv,   name='import_clean_csv'),
]
