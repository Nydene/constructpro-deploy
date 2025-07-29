from django.db import models

class Company(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        db_table = 'company'

    def __str__(self):
        return self.name


class Project(models.Model):
    name = models.CharField(max_length=200)
    project_number = models.CharField(max_length=100)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        db_column='company_id'
    )

    class Meta:
        db_table = 'project'

    def __str__(self):
        return self.name


class User(models.Model):
    username = models.CharField(max_length=150, unique=True)
    password = models.CharField(max_length=128)
    name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        db_column='company_id'
    )

    class Meta:
        db_table = 'users'

    def __str__(self):
        return self.username


class Recipient(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        db_table = 'recipient'

    def __str__(self):
        return self.name


class ExpenseType(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        db_table = 'expense_type'

    def __str__(self):
        return self.name


class ExpenseSubject(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        db_table = 'expense_subject'

    def __str__(self):
        return self.name


class ExpenseStatus(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        db_table = 'expense_status'

    def __str__(self):
        return self.name


class InvoiceStatus(models.Model):
    name = models.CharField(max_length=200)

    class Meta:
        db_table = 'invoice_status'

    def __str__(self):
        return self.name


class Invoice(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        db_column='project_id'
    )
    invoice_number = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    received_amount = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    invoice_date = models.DateField()
    due_date = models.DateField()
    date_paid = models.DateField(blank=True, null=True)
    invoice_attachment = models.CharField(max_length=255, blank=True, null=True)
    payment_attachment = models.CharField(max_length=255, blank=True, null=True)
    invoice_status = models.ForeignKey(
        InvoiceStatus,
        on_delete=models.PROTECT,
        db_column='invoice_status_id'
    )
    is_archive = models.BooleanField(default=False, db_column='is_archive')
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='invoices_created',
        db_column='created_by_id'
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='invoices_updated',
        db_column='updated_by_id',
        blank=True,
        null=True
    )
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'invoice'

    def __str__(self):
        return self.invoice_number


class Expense(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        db_column='company_id'
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        db_column='project_id',
        blank=True,
        null=True
    )
    recipient = models.ForeignKey(
        Recipient,
        on_delete=models.PROTECT,
        db_column='recipient_id'
    )
    expense_type = models.ForeignKey(
        ExpenseType,
        on_delete=models.PROTECT,
        db_column='expense_type_id'
    )
    expense_subject = models.ForeignKey(
        ExpenseSubject,
        on_delete=models.PROTECT,
        db_column='expense_subject_id'
    )
    expense_status = models.ForeignKey(
        ExpenseStatus,
        on_delete=models.PROTECT,
        db_column='expense_status_id'
    )
    expense_number = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True, null=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    expense_date = models.DateField(blank=True, null=True)
    payable_date = models.DateField(blank=True, null=True)
    liquidated_amount = models.DecimalField(max_digits=15, decimal_places=2, blank=True, null=True)
    liquidated_date = models.DateField(blank=True, null=True)
    return_date = models.DateField(blank=True, null=True)
    attachment = models.CharField(max_length=100, blank=True, null=True)
    is_archive = models.BooleanField(default=False, db_column='is_archive')
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='expenses_created',
        db_column='created_by_id'
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='expenses_updated',
        db_column='updated_by_id',
        blank=True,
        null=True
    )
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'expense'

    def __str__(self):
        return self.expense_number
