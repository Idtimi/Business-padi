# Voice-to-Ledger (Smart Challenge 3rd Edition)

AI-powered voice & text bookkeeping on WhatsApp for Nigerian traders and MSMEs.
Built with FastAPI, SQLAlchemy, SQLite/PostgreSQL, Meta WhatsApp Cloud API, and ReportLab.

---

## Features
- **Voice & Text Ledgering**: Multilingual speech recognition (Nigerian Pidgin, English, Hausa, Yoruba, Igbo) with deterministic arithmetic (all monetary computations strictly in integer kobo).
- **Confirm-Before-Save Architecture**: Pending actions safeguard financial integrity; no transaction commits without trader confirmation.
- **Credit Book & Debt Reminders**: Oldest-debt FIFO repayment allocation, smart debt aging nudges, and trader-sent WhatsApp reminder links (`wa.me`).
- **Professional Documents**: Branded PDF receipts, invoices, and periodic account statements with SHA-256 verification and atomic sequential numbering (`R-0001`, `INV-0001`).
- **Stock & Inventory Control**: Real-time stock movement, weighted average cost accounting, packaging unit conversions (carton to sachet), and low-stock alerts.
- **Role Permissions & Approvals**: Multi-seat business teams with attendant recording, one-time invite codes (`JOIN-XXXXXX`), and owner approval thresholds for large sales ($\ge \unicode{x20A6}100,000$).
- **Message Cost & Window Controls**: Enforcement of Meta's 24-hour customer service window, utility message templates, customer opt-in/STOP compliance, daily message budget alerts, and admin cost reporting.

---

## Quick Start

### 1. Environment Setup
```bash
python -m venv .venv
.\.venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 2. Configuration (`.env`)
```ini
APP_ENV=dev
DATABASE_URL=sqlite:///./ledger.db
TZ=Africa/Lagos

# WhatsApp Cloud API credentials
WHATSAPP_TOKEN=your_permanent_system_user_token
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_VERIFY_TOKEN=your_webhook_verify_token
WHATSAPP_APP_SECRET=your_app_secret
GRAPH_API_VERSION=v20.0

# Message Cost & Budget Controls
USD_NGN=1400
WA_USD_PER_MESSAGE=0.0077
DAILY_MESSAGE_COST_ALERT_KOBO=500000
```

### 3. Run Server
```bash
uvicorn app.main:app --reload --port 8000
```

### 4. Run Tests
```bash
pytest -v
```

---

## WhatsApp Cloud API Go-Live Checklist

To transition from Meta developer sandbox to live production with real Nigerian traders and customers, complete every step in this checklist:

### 1. Meta Business Verification
- [ ] Go to [Meta Business Settings](https://business.facebook.com/settings/) > **Security Center**.
- [ ] Initiate Business Verification by uploading official incorporation documents (CAC Certificate of Incorporation / Business Name Registration in Nigeria).
- [ ] Provide a matching utility bill, bank statement, or tax certificate showing the legal business name and official physical address.
- [ ] Verify phone number or official domain email via OTP. Wait for Meta approval status (**Verified**).

### 2. Display Name Review & Approval
- [ ] Under **WhatsApp** > **Phone Numbers** in Meta Business Manager, request your official WhatsApp Display Name (e.g. `Voice Ledger` or trader brand).
- [ ] Ensure the display name strictly complies with [Meta Display Name Guidelines](https://www.facebook.com/business/help/529612240925342):
  - Must have a direct relationship to your legal business or brand.
  - Must not contain generic words alone, punctuation, or all-caps unless trademarked.
- [ ] Wait for Meta automated or manual certificate review until status changes to **Approved**.

### 3. Permanent System-User Access Token
- [ ] In **Business Settings** > **Users** > **System Users**, create a new System User with the **Admin** role (e.g., `VoiceLedgerBot`).
- [ ] Under **Assigned Assets**, grant access to your WhatsApp Business Account (WABA) with **Full Control**.
- [ ] Generate a new token with permanent (never-expiring) lifetime.
- [ ] Select required permissions:
  - `whatsapp_business_messaging`
  - `whatsapp_business_management`
- [ ] Store this token securely as `WHATSAPP_TOKEN` in your production secret manager (never commit to git).

### 4. Dedicated Phone Number Registration & 2FA PIN
- [ ] Obtain a clean, dedicated E.164 phone number (e.g. Nigerian `+234...`) that is **not** currently active on any personal WhatsApp or WhatsApp Business mobile app (delete any existing WhatsApp account on this number first).
- [ ] In Meta WhatsApp Manager, add the phone number and receive the 6-digit verification code via SMS or voice call.
- [ ] Set up a permanent 6-digit Two-Factor Authentication (2FA) PIN and record it in secure operational storage.
- [ ] Note down the assigned `WHATSAPP_PHONE_NUMBER_ID` for configuration.

### 5. Utility & Service Template Submissions
Under WhatsApp Manager > **Message Templates**, submit the required utility templates for Meta review before initiating any customer messages outside the 24-hour service window:

#### A. Document Delivery Template (`receipt_invoice_delivery`)
- **Category**: `UTILITY`
- **Header**: Document (PDF attachment)
- **Body**:
  ```
  Hello {{1}}, here is your {{2}} of {{3}} from {{4}}. Thank you for your business!
  ```
- **Sample values**:
  `{{1}}`: Mama Ngozi, `{{2}}`: Receipt R-0001, `{{3}}`: ₦77,500, `{{4}}`: Alhaja Gold Stores

#### B. Payment Reminder Template (`payment_reminder`)
- **Category**: `UTILITY`
- **Header**: None
- **Body**:
  ```
  Hello {{1}}, this is a friendly reminder from {{4}} regarding the balance of {{2}} due on {{3}}. Thank you!
  ```
- **Sample values**:
  `{{1}}`: Bro Emeka, `{{2}}`: ₦25,000, `{{3}}`: Friday, `{{4}}`: Alhaja Gold Stores

### 6. Webhook Setup & Security
- [ ] Configure Webhook URL in Meta App Dashboard: `https://your-domain.com/whatsapp/webhook`.
- [ ] Set `WHATSAPP_VERIFY_TOKEN` matching Meta App settings.
- [ ] Enable signature verification via `WHATSAPP_APP_SECRET` and validate `X-Hub-Signature-256` on every incoming webhook POST.
- [ ] Subscribe to the `messages` webhook field.

### 7. Message Cost Controls & Opt-Out Compliance
- [ ] Verify that customer `STOP` requests immediately mark records as `opted_out = 1` and cease automated sends.
- [ ] Confirm free-form messages outside the 24-hour window are blocked, enforcing template delivery.
- [ ] Monitor daily and monthly message budgets via the admin `cost report` command.
- [ ] Run `python scripts/price_check.py` periodically to track tier margins against Meta's rate card.
