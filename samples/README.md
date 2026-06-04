# Sample documents

A dozen **synthetic** PDFs for quick end-to-end testing of the anonymiser. Every
name, date, address, IBAN, ID and phone number here is **invented** — these files
contain **no real personal data**, which is why they're safe to commit.

They span a deliberately diverse corpus (account-opening forms, invoices, payslips,
utility bills, insurance and employment letters, a bank statement, a tax form, a
rental agreement, a loan application, a medical letter and a KYC form) in EN / DE /
FR — the kind of mix the PII-type planner and the scanner have to cope with.

| File | Type | Language | PII it carries |
|------|------|----------|----------------|
| `01-account-opening-de.pdf` | Account opening | DE | name, DOB, address, IBAN, ID, phone, email, signature |
| `02-invoice-en.pdf` | Invoice | EN | name, address, account no., email, phone |
| `03-payslip-fr.pdf` | Payslip | FR | name, DOB, address, SSN, IBAN |
| `04-utility-bill-de.pdf` | Utility bill | DE | name, address, customer/meter no., email |
| `05-insurance-letter-en.pdf` | Insurance letter | EN | name, DOB, address, policy no., email, phone, signature |
| `06-employment-contract-de.pdf` | Employment contract | DE | name, DOB, address, tax ID, IBAN, email, signature |
| `07-bank-statement-en.pdf` | Bank statement | EN | name, address, IBAN, BIC, email |
| `08-tax-form-fr.pdf` | Tax return | FR | name, DOB, address, tax no., email, phone, signature |
| `09-rental-agreement-de.pdf` | Rental agreement | DE | name, DOB, address, ID, IBAN, phone, signature |
| `10-loan-application-en.pdf` | Loan application | EN | name, DOB, address, NI number, email, phone, signature |
| `11-medical-letter-de.pdf` | Medical letter | DE | name, DOB, address, insurance no., phone, signature |
| `12-kyc-onboarding-fr.pdf` | KYC onboarding | FR | name, DOB, address, ID, IBAN, email, signature |

## Regenerate

```bash
python scripts/generate_samples.py            # writes ./samples/*.pdf
```

Edit `scripts/generate_samples.py` to add or change documents.

## Use them

Upload to a bucket and point a job at it (see the top-level README), or render one
locally to eyeball it:

```python
from pdf_anonymiser.gemini_client import render_pdf
render_pdf(open("samples/01-account-opening-de.pdf", "rb").read(), 150)[0].show()
```
