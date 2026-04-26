from django.core.management.base import BaseCommand
from django.db import transaction
from payments.models import Merchant, BankAccount, LedgerEntry, PayoutRequest, IdempotencyKey
from payments.services import LedgerService

MERCHANTS_DATA = [
    {"name": "Arjun Design Studio","email": "arjun@arjundesign.in","bank_account_number": "50100234567890","bank_ifsc": "HDFC0001234","bank_account_holder": "Arjun Sharma","credits": [(150000,"Payment: Logo design - Acme Corp"),(85000,"Payment: Social media kit - StartupXYZ"),(200000,"Payment: Brand identity - TechFlow"),(75000,"Payment: UI mockups - MobileApp"),]},
    {"name": "Priya Content Agency","email": "priya@priyacontent.in","bank_account_number": "919876543210123","bank_ifsc": "ICIC0002567","bank_account_holder": "Priya Mehta","credits": [(300000,"Payment: Monthly retainer - EcomBrand"),(120000,"Payment: Blog writing - TechStartup"),(180000,"Payment: Social media - RetailCo"),(250000,"Payment: Video scripts - EdTechCo"),]},
    {"name": "Rohan Dev Freelancer","email": "rohan@rohandev.in","bank_account_number": "00441234567891","bank_ifsc": "SBIN0003456","bank_account_holder": "Rohan Verma","credits": [(500000,"Payment: Full-stack app - HealthTech"),(220000,"Payment: REST API - LogisticsApp"),(175000,"Payment: Django backend - MarketplaceX"),(320000,"Payment: React dashboard - AnalyticsCo"),]},
    {"name": "Sneha Photography","email": "sneha@snehaphotos.in","bank_account_number": "62310098765432","bank_ifsc": "PUNB0004567","bank_account_holder": "Sneha Kapoor","credits": [(80000,"Payment: Wedding shoot - Sharma Family"),(120000,"Payment: Corporate event - TechConf"),(60000,"Payment: Product photography - FashionBrand"),(95000,"Payment: Portrait session - StartupTeam"),]},
    {"name": "Vikram SEO Agency","email": "vikram@vikramseo.in","bank_account_number": "38291765430012","bank_ifsc": "AXIS0005678","bank_account_holder": "Vikram Singh","credits": [(250000,"Payment: SEO retainer Q1 - EcomStore"),(180000,"Payment: Link building - SaaSProduct"),(320000,"Payment: SEO audit - FinTechCo"),(150000,"Payment: Content strategy - HealthApp"),]},
    {"name": "Meera Illustration Studio","email": "meera@meeraart.in","bank_account_number": "45678901234567","bank_ifsc": "KOTAK006789","bank_account_holder": "Meera Nair","credits": [(120000,"Payment: Book illustrations - PenguinIN"),(85000,"Payment: App icons - MobileStudio"),(200000,"Payment: Brand mascot - FoodBrand"),(95000,"Payment: Infographic series - EdTech"),]},
    {"name": "Aakash Video Production","email": "aakash@aakashvideo.in","bank_account_number": "11223344556677","bank_ifsc": "YESB0007890","bank_account_holder": "Aakash Gupta","credits": [(450000,"Payment: Brand film - AutoCompany"),(280000,"Payment: YouTube series - CreatorCo"),(190000,"Payment: Product launch video - StartupY"),(320000,"Payment: Corporate training - MNCCorp"),]},
    {"name": "Divya Translation Services","email": "divya@divyatranslates.in","bank_account_number": "99887766554433","bank_ifsc": "IDBI0008901","bank_account_holder": "Divya Iyer","credits": [(75000,"Payment: Legal doc translation - LawFirm"),(110000,"Payment: Website localization - GlobalSaaS"),(65000,"Payment: Medical reports - Hospital"),(90000,"Payment: Marketing content - EcomBrand"),]},
    {"name": "Rahul Music Composer","email": "rahul@rahulmusic.in","bank_account_number": "55443322110099","bank_ifsc": "FINO0009012","bank_account_holder": "Rahul Bose","credits": [(200000,"Payment: Jingle composition - AdAgency"),(350000,"Payment: Background score - WebSeries"),(150000,"Payment: Podcast intro - MediaCo"),(280000,"Payment: Game soundtrack - IndieStudio"),]},
    {"name": "Kavya Marketing Consultant","email": "kavya@kavyamarketing.in","bank_account_number": "77665544332211","bank_ifsc": "KKBK0010123","bank_account_holder": "Kavya Reddy","credits": [(400000,"Payment: Growth strategy - Series A Startup"),(250000,"Payment: GTM plan - SaaSCompany"),(180000,"Payment: Performance marketing - D2CBrand"),(320000,"Payment: Brand positioning - HealthcareApp"),]},
]

class Command(BaseCommand):
    help = 'Seed database with 10 test merchants'

    def add_arguments(self, parser):
        parser.add_argument('--clear', action='store_true', help='Clear existing data first')

    def handle(self, *args, **options):
        if options['clear']:
            self.stdout.write('Clearing existing data...')
            IdempotencyKey.objects.all().delete()
            PayoutRequest.objects.all().delete()
            LedgerEntry.objects.all().delete()
            BankAccount.objects.all().delete()
            Merchant.objects.all().delete()
            self.stdout.write(self.style.WARNING('Cleared.'))

        self.stdout.write('Seeding 10 merchants...')
        for data in MERCHANTS_DATA:
            with transaction.atomic():
                merchant, created = Merchant.objects.get_or_create(
                    email=data['email'],
                    defaults={'name': data['name'],'bank_account_number': data['bank_account_number'],'bank_ifsc': data['bank_ifsc'],'bank_account_holder': data['bank_account_holder'],}
                )
                if created:
                    bank = BankAccount.objects.create(merchant=merchant,account_number=data['bank_account_number'],ifsc_code=data['bank_ifsc'],account_holder_name=data['bank_account_holder'],is_primary=True,)
                    for amount, desc in data['credits']:
                        LedgerService.credit_merchant(merchant=merchant, amount_paise=amount, description=desc)
                    self.stdout.write(self.style.SUCCESS(f"Created: {merchant.name} | Rs.{merchant.get_balance()/100:,.2f} | bank_id: {bank.id}"))
                else:
                    self.stdout.write(self.style.WARNING(f"Skipped: {merchant.name}"))
        self.stdout.write(self.style.SUCCESS('Done! 10 merchants ready.'))
