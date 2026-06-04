#!/usr/bin/env python3
"""Generate a dozen synthetic, PII-laden sample PDFs for quick testing.

Every value here is INVENTED — fake names, fake IBANs, fake addresses. The point is
to exercise the anonymiser end-to-end without touching any real personal data, so
these files are safe to commit. Run:

    python scripts/generate_samples.py            # writes ./samples/*.pdf
    python scripts/generate_samples.py --out /tmp/docs

Output: 12 single-or-two-page A4 documents spanning account-opening forms, invoices,
payslips, utility bills, insurance and employment letters, a bank statement, a tax
form, a rental agreement and a loan application, in EN / DE / FR — a deliberately
diverse corpus (the kind the type-planner and the scanner have to cope with).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

A4 = (1240, 1754)  # ~150 dpi
MARGIN = 90
INK = (24, 26, 31)
MUTED = (110, 116, 124)
RULE = (210, 214, 220)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


# Each document: title, a subtitle/issuer line, sections of (label, value) rows where
# the value carries the synthetic PII, and an optional signature name.
_DOCS: list[dict] = [
    {
        "id": "01-account-opening-de", "title": "Kontoeröffnungsantrag",
        "issuer": "Musterbank AG · Filiale Frankfurt am Main",
        "rows": [
            ("Name, Vorname", "Brandt, Annelie"),
            ("Geburtsdatum", "14.03.1987"),
            ("Anschrift", "Lindenstraße 42, 60311 Frankfurt am Main"),
            ("IBAN", "DE89 3704 0044 0532 0130 00"),
            ("Ausweisnummer", "L01X00T47"),
            ("Telefon", "+49 69 1234 5678"),
            ("E-Mail", "annelie.brandt@example.de"),
        ],
        "sign": "Annelie Brandt",
    },
    {
        "id": "02-invoice-en", "title": "INVOICE  #INV-2026-0481",
        "issuer": "Northwind Trading Ltd · 14 Carlisle St, London W1D 3BP",
        "rows": [
            ("Bill to", "Mr. Oliver Whitfield"),
            ("Address", "27 Maple Avenue, Bristol BS1 4ND"),
            ("Account no.", "GB29 NWBK 6016 1331 9268 19"),
            ("Customer ID", "NW-558102"),
            ("Email", "o.whitfield@example.co.uk"),
            ("Phone", "+44 117 496 0233"),
            ("Amount due", "£ 2,480.00"),
        ],
        "sign": None,
    },
    {
        "id": "03-payslip-fr", "title": "Bulletin de paie",
        "issuer": "Société Lumière SARL · 12 Rue de Rivoli, 75004 Paris",
        "rows": [
            ("Salarié", "Camille Rousseau"),
            ("Date de naissance", "02/09/1991"),
            ("Adresse", "8 Rue des Lilas, 69003 Lyon"),
            ("N° sécurité sociale", "2 91 09 69 123 456 78"),
            ("IBAN", "FR14 2004 1010 0505 0001 3M02 606"),
            ("Net à payer", "2 310,57 €"),
        ],
        "sign": None,
    },
    {
        "id": "04-utility-bill-de", "title": "Stromrechnung",
        "issuer": "StadtEnergie GmbH · Kundenservice",
        "rows": [
            ("Kunde", "Markus Henning"),
            ("Kundennummer", "SE-2210-77431"),
            ("Lieferadresse", "Goethestraße 9, 80336 München"),
            ("Zählernummer", "1ESY1160889012"),
            ("E-Mail", "m.henning@example.de"),
            ("Betrag", "184,20 €"),
        ],
        "sign": None,
    },
    {
        "id": "05-insurance-letter-en", "title": "Policy Confirmation",
        "issuer": "Albion Insurance plc",
        "rows": [
            ("Policyholder", "Mrs. Eleanor Hayes"),
            ("Date of birth", "21 June 1979"),
            ("Address", "5 Beechwood Close, Leeds LS8 2QR"),
            ("Policy number", "ALB-MOT-9920841"),
            ("Email", "eleanor.hayes@example.com"),
            ("Phone", "+44 113 802 5566"),
        ],
        "sign": "E. Hayes",
    },
    {
        "id": "06-employment-contract-de", "title": "Arbeitsvertrag",
        "issuer": "TechWerk Solutions GmbH",
        "rows": [
            ("Arbeitnehmer", "Sophie Adlerová"),
            ("Geburtsdatum", "30.11.1994"),
            ("Anschrift", "Kastanienallee 17, 10435 Berlin"),
            ("Steuer-ID", "44 123 456 789"),
            ("IBAN", "DE21 1001 0010 0123 4567 89"),
            ("E-Mail", "sophie.adlerova@example.de"),
        ],
        "sign": "Sophie Adlerová",
    },
    {
        "id": "07-bank-statement-en", "title": "Account Statement",
        "issuer": "Meridian Bank · Statement period: Apr 2026",
        "rows": [
            ("Account holder", "James P. Donnelly"),
            ("Address", "112 Riverside Drive, Dublin D04 X5N2"),
            ("IBAN", "IE29 AIBK 9311 5212 3456 78"),
            ("BIC", "AIBKIE2D"),
            ("Email", "j.donnelly@example.ie"),
            ("Closing balance", "€ 7,841.93"),
        ],
        "sign": None,
    },
    {
        "id": "08-tax-form-fr", "title": "Déclaration de revenus",
        "issuer": "Direction générale des Finances publiques",
        "rows": [
            ("Déclarant", "Théo Marchand"),
            ("Date de naissance", "17/05/1985"),
            ("Adresse", "23 Avenue Jean Jaurès, 31000 Toulouse"),
            ("N° fiscal", "13 24 56 789 012 34"),
            ("Courriel", "theo.marchand@example.fr"),
            ("Téléphone", "+33 5 61 22 88 41"),
        ],
        "sign": "T. Marchand",
    },
    {
        "id": "09-rental-agreement-de", "title": "Mietvertrag",
        "issuer": "Hausverwaltung Nordstern",
        "rows": [
            ("Mieter", "Friederike Vollmer"),
            ("Geburtsdatum", "08.07.1990"),
            ("Mietobjekt", "Hafenweg 3, 20457 Hamburg"),
            ("Ausweisnummer", "T22000129"),
            ("IBAN", "DE62 2005 0550 1234 5678 90"),
            ("Telefon", "+49 40 5566 7788"),
        ],
        "sign": "Friederike Vollmer",
    },
    {
        "id": "10-loan-application-en", "title": "Personal Loan Application",
        "issuer": "Crowngate Finance",
        "rows": [
            ("Applicant", "Priya Nair"),
            ("Date of birth", "03 February 1988"),
            ("Address", "9 Oakfield Road, Manchester M14 6FS"),
            ("National Insurance", "QQ 12 34 56 C"),
            ("Email", "priya.nair@example.com"),
            ("Phone", "+44 161 442 7790"),
            ("Requested amount", "£ 15,000"),
        ],
        "sign": "P. Nair",
    },
    {
        "id": "11-medical-letter-de", "title": "Arztbrief",
        "issuer": "Praxis Dr. med. Köhler · Innere Medizin",
        "rows": [
            ("Patient", "Hans-Jürgen Bauer"),
            ("Geburtsdatum", "26.10.1968"),
            ("Anschrift", "Bergstraße 5, 70173 Stuttgart"),
            ("Versichertennr.", "A123456780"),
            ("Telefon", "+49 711 998 4410"),
        ],
        "sign": "Dr. Köhler",
    },
    {
        "id": "12-kyc-onboarding-fr", "title": "Formulaire KYC",
        "issuer": "Banque Aurore · Conformité",
        "rows": [
            ("Client", "Nadia Benali"),
            ("Date de naissance", "11/12/1993"),
            ("Adresse", "47 Boulevard Haussmann, 75009 Paris"),
            ("Pièce d'identité", "20FX48219"),
            ("IBAN", "FR76 3000 6000 0112 3456 7890 189"),
            ("Courriel", "nadia.benali@example.fr"),
        ],
        "sign": "Nadia Benali",
    },
]


def _render(doc: dict) -> Image.Image:
    img = Image.new("RGB", A4, "white")
    d = ImageDraw.Draw(img)
    title_f, issuer_f, label_f, value_f, sign_f = (
        _font(46, bold=True), _font(24), _font(24), _font(28, bold=True), _font(40),
    )
    y = MARGIN
    d.text((MARGIN, y), doc["title"], font=title_f, fill=INK)
    y += 64
    d.text((MARGIN, y), doc["issuer"], font=issuer_f, fill=MUTED)
    y += 44
    d.line([(MARGIN, y), (A4[0] - MARGIN, y)], fill=RULE, width=2)
    y += 50
    for label, value in doc["rows"]:
        d.text((MARGIN, y), label, font=label_f, fill=MUTED)
        d.text((MARGIN + 360, y - 4), value, font=value_f, fill=INK)
        y += 70
    if doc.get("sign"):
        y += 60
        d.text((MARGIN, y), "Signature:", font=issuer_f, fill=MUTED)
        # a "handwritten" signature in an italic-ish stand-in
        d.text((MARGIN + 220, y - 18), doc["sign"], font=sign_f, fill=(40, 60, 130))
        d.line([(MARGIN + 210, y + 44), (MARGIN + 720, y + 44)], fill=RULE, width=2)
    return img


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic sample PDFs.")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "samples"))
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for doc in _DOCS:
        img = _render(doc)
        path = out_dir / f"{doc['id']}.pdf"
        img.save(path, format="PDF", resolution=150.0)
        print(f"  ✓ {path.name}")
    print(f"\n{len(_DOCS)} synthetic sample PDFs written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
