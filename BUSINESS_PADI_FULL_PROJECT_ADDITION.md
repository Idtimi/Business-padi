# Voice-to-Ledger (Business Padi): Full Project Master Document & Feature Addition

## Project Metadata
- **Project Name**: Business Padi (formerly Voice-to-Ledger)
- **Competition**: SMART Challenge (3rd Edition)
- **Theme**: AI and Data Intelligence
- **Platforms**: Telegram Bot (@Business_padi_bot), Meta WhatsApp Cloud API, and Responsive Web Dashboard
- **Live Deployment**: Render Production Environment (https://business-padi.onrender.com/)
- **Core Technology Stack**: Python 3.12, FastAPI, SQLAlchemy (PostgreSQL / SQLite), Groq Whisper Speech Recognition, YarnGPT Text-to-Speech, ReportLab PDF Generation, Pillow Image Processing, APScheduler, Chart.js, and Jinja2

---

## 1. Executive Summary & Evolution

Business Padi is an artificial intelligence voice-first business assistant designed for Nigerian open-market traders and small-to-medium enterprises (MSMEs). Operating directly inside WhatsApp and Telegram, it allows traders and shop attendants to speak naturally in Nigerian Pidgin, English, Yoruba, Hausa, or Igbo to record sales, expenses, customer debts, and inventory movements.

### Key Additions & Refinements Since Original Specification:
1. **Dual-Channel Deployment (Telegram + WhatsApp)**: While the initial specification targeted WhatsApp exclusively, the platform now natively supports Telegram (@Business_padi_bot). This provides zero per-message charges for traders, instant interactive inline keyboards, and one-click deep links, while maintaining full WhatsApp Cloud API compliance.
2. **Autonomous Attendant Selling & End-of-Day Audit**: Moving beyond real-time approval blocking (which halted fast-paced market sales), attendants now record sales, deduct stock, and issue receipts immediately. The owner audits total items sold and reconciles physical cash at closing time via an automated Stock Book.
3. **Sub-Second Direct Audio Streaming**: Voice notes in native formats (.ogg, .opus, .mp3, .wav) stream directly to Groq Whisper with trade terminology glossaries, bypassing heavy ffmpeg CPU transcoding and achieving 1 to 2 second response times.
4. **Unskewed Branded PDF Generation**: Receipts and invoices dynamically calculate image aspect ratios using Pillow, preventing logo distortion while adding bank payment instructions and SHA-256 verification hashes.
5. **Multi-Item Order Parsing**: Single spoken entries with multiple products, quantities, and prices are fully parsed into itemized rows without collapsing.
6. **Strict Role-Based Web Security**: Attendants can sell in chat but are strictly forbidden from the owner web dashboard (403 Forbidden) and cannot modify shop settings or invite staff.

---

## 2. Updated Competition Context & Scoring Map

| Evaluation Criterion | Original Weight | Live Platform Implementation | Evidence in Codebase & Live Demo |
| :--- | :--- | :--- | :--- |
| **Problem Statement** | 10% | Paper ledger loss, debtor forgetting, literacy barriers, and daily cashier balancing friction. | Market survey data, side-by-side paper vs digital comparisons. |
| **Solution & AI Use** | 20% | Dual-channel bot (Telegram + WhatsApp) with multilingual speech-to-text, entity extraction, and deterministic math. | Groq Whisper + local glossaries, integer-kobo arithmetic, ReportLab PDF generator. |
| **Market Size & Strategy** | 15% | 40M+ Nigerian MSMEs; focus on major open markets (Balogun, Alaba, Ariaria, Bodija); association distribution. | Market association referral loops, zero-app friction, tiered freemium pricing. |
| **Traction & Validation** | 10% | Real market voice notes, 85% debt recovery rate, sub-2-minute end-of-day audit. | Pilot metrics log, automated test suite with 31 passing unit/integration tests. |
| **Business & Unit Model** | 15% | Tiered plans (Starter Free, Solo 2,500 NGN, Trader 5,000 NGN, Business 15,000 NGN) and 25-entry add-on packs. | Quota enforcement engine, message cost tracking, positive unit margins. |
| **Impact & Sustainability** | 10% | Financial inclusion, language equity, informal commerce formalization, women trader empowerment. | Support for 5 Nigerian languages, aggregated credit profiles without personal data sale. |
| **Presentation Quality** | 20% | Live interactive voice demo, instant PDF delivery, 15-slide structured deck with visual proofs. | Scripted 2-minute demo with prepared offline fallback audio. |

---

## 3. Comprehensive System Architecture

```mermaid
graph TD
    User([Market Trader or Shop Attendant]) -->|Voice Note or Text| Channel[Telegram Bot / WhatsApp Cloud API]
    Channel -->|Async Webhook Event| FastAPIServer[FastAPI Webhook Handler]
    FastAPIServer -->|Immediate 200 OK| Channel
    FastAPIServer -->|Background Task| InFlightDeduper[In-Flight Message Deduplication]
    InFlightDeduper -->|Audio URL / File ID| AudioProcessor[Direct Audio Streamer]
    AudioProcessor -->|Native Audio Stream + Glossaries| GroqWhisper[Groq Whisper Speech API]
    GroqWhisper -->|Spoken Transcript| LLMNLU[LLM Structured Entity Parser]
    LLMNLU -->|Extracted JSON| ArithmeticEngine[Integer Kobo Math Engine]
    ArithmeticEngine -->|Draft Card| ConfirmGuard[Confirm-Before-Save Interface]
    ConfirmGuard -->|Trader Confirms| Ledger[Core Ledger Database]
    Ledger -->|Immediate Movement| StockEngine[Inventory Catalog & Stock Deductions]
    Ledger -->|Dynamic Aspect Ratio| ReportLabPDF[Branded PDF Generator]
    Ledger -->|Staff Sale Queue| DailyAuditQueue[Daily Staff Audit & Reconciliation]
    DailyAuditQueue -->|End-of-Day Review| OwnerAudit[Telegram /audit & Web Dashboard /d/audit]
```

### 3.1 Technical Components Breakdown
1. **FastAPI Webhook Engine**:
   - Handles webhooks from Telegram and WhatsApp.
   - Dispatches processing to background worker queues (`BackgroundTasks`) and responds with HTTP 200 within 10 milliseconds, preventing duplicate delivery retry loops.
2. **Audio & Speech Recognition (ASR)**:
   - Direct format streaming: Groq Whisper receives native audio formats directly.
   - Dynamic prompt injection: Injects market-specific terms (e.g., "carton", "mudu", "sachet", "derica", "indomie", "half-bag", "pkts") to eliminate regional transcription errors.
3. **Deterministic Arithmetic Engine**:
   - Strictly integer kobo (100 kobo = 1 Naira).
   - No floating-point variables are used for money.
   - Code calculates totals, tax, discounts, and debtor balances independently of the LLM.
4. **Branded Document Engine**:
   - Built on ReportLab running in Python 3.12.
   - Dynamic aspect-ratio scaling via Pillow: checks logo width and height, fitting it into a 1.6 x 0.9 inch header area without horizontal or vertical stretching.
   - Automatic numbering: sequential counters (`R-0001`, `INV-0001`).
   - Secure verification: SHA-256 digital document signature hash stored with each record.
5. **Staff Management & Stock Book Reconciliation**:
   - Attendant sales are marked as confirmed immediately, deducting stock from inventory in real time.
   - Sales are flagged with `reconciled_at = None`.
   - At closing, the owner executes `/audit` or visits `/d/staff` to compare physical cash against sales and balance the stock book with one tap.

---

## 4. Complete Feature Catalog (Baseline + Additions)

### 4.1 Voice & Text Ledgering
- Voice notes in Nigerian Pidgin, English, Yoruba, Hausa, and Igbo.
- Records sales, business expenses, customer debt repayments, and supplier restocks.
- Multi-item single entry: "Sold 7 bags of rice for 350,000 and 3 bags of salt for 210,000 to Mr Bolu" records both items as itemized rows in a single transaction.
- Conversational clarifications: if price or quantity is missing, the assistant asks a short targeted question without resetting the transaction context.
- Confirm-before-save pattern: draft cards present interactive `[Save]`, `[Edit]`, and `[Cancel]` buttons.

### 4.2 Debt Management & Credit Book
- Automated FIFO (First-In, First-Out) debt settlement: partial payments automatically pay off the oldest unpaid invoices first.
- Debtor summary command (`/debtors` or "who dey owe"): lists all customers with outstanding balances and aging categories (e.g., 7 days, 30 days, 60+ days).
- One-click reminder links: generates instant `wa.me` links pre-filled with polite reminders and bank account numbers that traders can forward in one tap.

### 4.3 Branded Receipts & Invoices
- Instant PDF delivery in chat and public secure web URLs (`/d/receipt/{doc_number}` and `/d/invoice/{doc_number}`).
- Unskewed logo scaling fitting trader branding proportionately.
- Clear visual status badges: `FULLY PAID` in forest green or `AMOUNT DUE` with balance.
- Payment Instructions Card: includes shop bank name, account number, and account holder name.
- Multiple item preservation: every line item shows quantity, unit, unit price, and line total.

### 4.4 Inventory & Stock Control
- Real-time stock decrement: stock is deducted the moment a sale is confirmed.
- Chat restock command: `/restock <item> <quantity>` (e.g., `/restock indomie 20 cartons`).
- Split unit conversions: supports selling single sachets from bulk cartons.
- Low-stock warnings: alerts traders inside summary reports when inventory drops below safety thresholds.

### 4.5 Multi-Seat Staff Management & End-of-Day Audit
- One-click Telegram invite link (`https://t.me/Business_padi_bot?start=JOIN-XXXXXX`).
- Attendants join immediately with zero setup wizard friction.
- Autonomous attendant selling: attendants take requests, record sales, deduct stock, and issue receipts/invoices without waiting for real-time owner approvals.
- Document scoping: `/receipt` or `/invoice` sent by an attendant returns her own recent transaction.
- Role-based security: attendants receive `403 Forbidden` if attempting to access the owner web dashboard, and cannot modify shop settings or invite staff.
- Daily Staff Audit & Stock Book (`/audit` in chat or on web dashboard):
  - Aggregates all physical products sold by attendants today.
  - Calculates the exact Cash to Collect from Attendants.
  - Provides a single **[Approve & Reconcile All]** button for closing time.

### 4.6 Web Dashboard & Analytics
- Responsive desktop and mobile interface styled with Business Padi brand colors (Navy `#0D1F45`, Orange `#FF6A13`, Mist `#F1F4F9`).
- Passwordless magic login link (15-minute expiry) and phone OTP login.
- Real-time revenue charts, top debtors table, product inventory tables, and staff team management.
- CSV export for transactions and customer ledgers.

---

## 5. Pricing, Quota & Unit Economics Model

### 5.1 Subscription Tiers

| Tier Name | Price (NGN/mo) | Monthly Voice Entries | Staff Seats | Receipts & Invoices | Stock Control | Financial Profile |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Starter** | Free (0 NGN) | 20 entries | 0 seats | Basic receipts | No | No |
| **Solo Trader** | 2,500 NGN | 50 entries | 0 seats | Branded receipts & invoices | Basic | Basic |
| **Trader** | 5,000 NGN | 100 entries | 0 seats | Branded receipts & invoices | Basic | Basic |
| **Business** | 15,000 NGN | 300 entries | Up to 3 seats | Custom branded documents | Full inventory & Stock Book | Full shareable profile |
| **Partner** | Custom | Unlimited | Custom | Enterprise white-label | Full enterprise | Direct API access |

- **Add-On Packs**: 25 extra voice entries for 2,500 NGN.

### 5.2 Unit Economics Breakdown
- **Speech Recognition (Groq Whisper)**: ~0.0005 USD per 10-second entry (~0.70 NGN).
- **LLM Extraction (Llama 3.3 / Qwen / GPT-4o-mini)**: ~0.0015 USD per entry (~2.10 NGN).
- **Server & Storage Overhead**: ~3.00 NGN per entry.
- **Telegram Channel Cost**: 0.00 NGN per message.
- **WhatsApp Channel Cost (when using Cloud API)**: ~0.0077 USD per outbound message outside 24h window.
- **Gross Margin on Trader Tier (5,000 NGN)**:
  - Estimated total cost for 100 entries: ~1,200 NGN.
  - Typical gross margin: **~76% on Telegram, ~45% on WhatsApp**.

---

## 6. Slide-by-Slide Presentation Blueprint (with Visual Placeholders)

This 15-slide deck is structured for AI presentation tools (such as Gamma, Tome, Canva AI, Claude, or PowerPoint). Each slide contains the narrative, talking points, and explicit image callouts.

---

### Slide 1: Title & Vision
- **Slide Title**: Business Padi: AI Bookkeeping for African Open Markets
- **Subtitle**: Turning Spoken WhatsApp and Telegram Voice Notes into Financial Ledgers, Invoices, and Inventory Control
- **Presenter Script**:
  "Over 40 million small businesses power Nigeria's retail trade, yet over 80% keep records on paper or rely on memory. Business Padi brings enterprise-grade bookkeeping into the apps traders already use every day: WhatsApp and Telegram. No forms, no typing, no accounting jargon. Just speak."
- **Key Bullet Points**:
  - Voice-first bookkeeping in Nigerian Pidgin, English, Yoruba, Hausa, and Igbo.
  - Deterministic integer-kobo math eliminating financial calculation errors.
  - Instant branded PDF receipts, credit book tracking, and end-of-day stock reconciliation.
- **Visual Callout**:
  `[INSERT SCREENSHOT: Dual smartphone mockup displaying Telegram on the left with a voice note transcription and instant PDF receipt, and WhatsApp on the right with an automated customer debt reminder]`

---

### Slide 2: The Problem: The Paper Ledger Crisis
- **Slide Title**: The Hidden Cost of Informal Retail
- **Presenter Script**:
  "Visit any stall in Balogun or Alaba Market. You will see torn, water-stained paper notebooks. When a customer takes goods on credit, it is jotted down in haste. Weeks later, debts are forgotten. When attendants run the stall, owners have no fast way to verify what was sold against the cash in the drawer."
- **Key Bullet Points**:
  - Over 80% of micro-traders keep records on paper notebooks vulnerable to water, theft, and loss.
  - Unrecovered customer credit quietly drains up to 25% of working capital.
  - Existing accounting software requires English literacy, typing, and complex bookkeeping concepts.
  - Shop owners face daily cashier disputes when reconciling goods sold by attendants.
- **Visual Callout**:
  `[INSERT PHOTO: A high-contrast photograph of a real, torn market trader notebook with handwritten figures, placed beside a smartphone displaying a clean digital Business Padi card]`

---

### Slide 3: The Solution: Voice-Ledgering in Natural Languages
- **Slide Title**: Just Speak. Business Padi Balances the Books.
- **Presenter Script**:
  "Business Padi meets traders where they are. A trader simply presses the microphone button and speaks: 'Sell 2 bags of foreign rice 90 thousand to Mr Bolu.' Within 2 seconds, our system extracts the items, calculates balances, and presents an interactive confirmation card."
- **Key Bullet Points**:
  - Zero application downloads: works natively inside Telegram and WhatsApp.
  - Speaks and understands Nigerian Pidgin, Yoruba, Hausa, Igbo, and English.
  - Confirm-before-save pattern: AI never writes directly to the books without trader confirmation.
  - Handles multi-item sales, expenses, debts, and restocks in a single spoken note.
- **Visual Callout**:
  `[INSERT SCREENSHOT: Telegram chat showing a voice note waveform, followed by the bot's instant interactive confirmation card with [Save], [Edit], and [Cancel] buttons]`

---

### Slide 4: High-Speed Architecture & Deterministic Math
- **Slide Title**: AI for Understanding, Deterministic Code for Math
- **Presenter Script**:
  "A core principle of Business Padi is trust. AI is brilliant at understanding human language, but it should never be trusted with arithmetic. In Business Padi, the LLM only extracts entity data. Our deterministic code performs all math in integer kobo, guaranteeing zero rounding errors."
- **Key Bullet Points**:
  - Sub-second speech recognition via Groq Whisper with custom market trade terminology glossaries.
  - Deterministic mathematics: all amounts calculated and stored in integer kobo (100 kobo = 1 Naira).
  - Background asynchronous task dispatch ensuring sub-10ms webhook responses with zero retry loops.
- **Visual Callout**:
  `[INSERT INFOGRAPHIC: Architecture diagram illustrating Voice Note -> Groq Whisper -> Entity Parser -> Integer Kobo Math Engine -> Confirm Guard -> Database Ledger]`

---

### Slide 5: Branded Receipts & Aspect-Ratio Logo Scaling
- **Slide Title**: Professional Branded Documents in Seconds
- **Presenter Script**:
  "Nothing builds trust with customers like an instant, professional receipt. When a sale is saved, Business Padi generates a branded PDF receipt. Our engine dynamically calculates the trader's logo aspect ratio so logos are never squashed or skewed."
- **Key Bullet Points**:
  - Instant PDF delivery in chat plus shareable public download links.
  - Dynamic aspect-ratio logo scaling: logos fit within header bounds without distortion.
  - Complete payment details: includes bank name, account number, customer balance, and a SHA-256 digital verification hash.
- **Visual Callout**:
  `[INSERT SCREENSHOT: High-resolution PDF receipt generated by Business Padi showing an unskewed shop logo, modern zebra striping, FULLY PAID badge, and bank details]`

---

### Slide 6: Multi-Item Invoices for Customer Orders
- **Slide Title**: Itemized Invoices for Bulk and Deposit Orders
- **Key Talking Points**:
  - Full preservation of complex orders: preserves individual quantities, descriptions, and unit prices.
  - Ideal for wholesale traders selling mixed cartons, bags, and packs.
  - Direct delivery to chat and one-tap WhatsApp forward links.
- **Visual Callout**:
  `[INSERT SCREENSHOT: An invoice document preview showing multiple line items (e.g. 7 bags of rice and 3 bags of salt) alongside the Telegram download link]`

---

### Slide 7: Credit Book & FIFO Debt Settlement
- **Slide Title**: Stop Losing Money to Uncollected Debts
- **Key Talking Points**:
  - Automatic balance updates when customers pay partially or take goods on credit.
  - First-In, First-Out (FIFO) allocation automatically applies customer repayments to the oldest outstanding invoices first.
  - Debt aging reports (`/debtors`) reveal exactly who owes, how much, and how long the balance has been overdue.
  - One-tap WhatsApp reminder links (`wa.me`) generate polite, pre-filled debt collection messages.
- **Visual Callout**:
  `[INSERT SCREENSHOT: Chat screen showing the /debtors command output listing top debtor balances, paired with a sample WhatsApp reminder preview]`

---

### Slide 8: Real-Time Inventory Control & Stock Book
- **Slide Title**: Real-Time Stock Tracking Without Manual Counting
- **Key Talking Points**:
  - Automatic stock deductions as sales are recorded throughout the day.
  - Quick restocking via chat commands (e.g., `/restock indomie 20 cartons`) or the web dashboard.
  - Packaging conversions: seamlessly tracks wholesale cartons split into retail single units.
  - Low-stock alerts protect traders from unexpectedly running out of fast-moving goods.
- **Visual Callout**:
  `[INSERT SCREENSHOT: Web dashboard Stock page showing inventory progress bars, quantities remaining, unit costs, and low-stock warning indicators]`

---

### Slide 9: Team & Multi-Seat Staff Management
- **Slide Title**: Empower Shop Attendants with Controlled Access
- **Key Talking Points**:
  - One-click Telegram join links (`https://t.me/Business_padi_bot?start=JOIN-XXXXXX`).
  - Attendants join the team immediately with zero setup friction.
  - Autonomous selling: attendants record sales, deduct stock, and issue receipts/invoices without real-time owner approval bottlenecks.
  - Role-based security: attendants are blocked from the owner web dashboard (`403 Forbidden`) and cannot modify shop settings.
- **Visual Callout**:
  `[INSERT SCREENSHOT: Telegram chat showing the /staff command output displaying registered team attendants alongside the one-click invite code generator]`

---

### Slide 10: The Daily Staff Audit & Cash Reconciliation
- **Slide Title**: End-of-Day Balancing: Reconcile Goods Sold & Cash
- **Key Talking Points**:
  - Solves the primary operational headache for multi-person retail stalls.
  - **Stock Book**: Displays the exact physical quantity of every item sold by attendants today.
  - **Cash Reconciliation**: Shows the exact amount of physical cash and customer transfers to collect from the attendant.
  - One-click approval: run `/audit` in Telegram or tap **Approve All** on the web dashboard to balance the books for the day.
- **Visual Callout**:
  `[INSERT SCREENSHOT: The Daily Staff Audit & Stock Book section on the dashboard showing Total Attendant Sales, Cash to Collect from Attendants, Items Sold breakdown, and the Approve button]`

---

### Slide 11: The Executive Web Dashboard
- **Slide Title**: Desktop & Mobile Web Control Center
- **Key Talking Points**:
  - Passwordless magic login link (15-minute expiry) or phone OTP verification.
  - Real-time visual charts showing sales trends, debtor aging, and top-selling products.
  - Customer ledger management with CSV transaction exports.
  - Built with official Business Padi brand guidelines (Deep Navy, Vibrant Orange, Warm Cream).
- **Visual Callout**:
  `[INSERT SCREENSHOT: Full panoramic view of the web dashboard overview page displaying sales metric cards, recent transaction logs, and monthly sales graphs]`

---

### Slide 12: Business Model & Subscription Tiers
- **Slide Title**: Sustainable, Tiered SaaS Model Built for Growth
- **Key Talking Points**:
  - **Starter (Free)**: 20 monthly entries for micro-traders.
  - **Solo Trader (2,500 NGN/mo)**: 50 entries, branded receipts, basic stock.
  - **Trader (5,000 NGN/mo)**: 100 entries, daily summaries, branded invoices, expense categories.
  - **Business (15,000 NGN/mo)**: 300 entries, up to 3 staff seats, full inventory, P&L reporting, smart reminders.
  - **Entry Add-On Packs**: 25 extra voice notes for 2,500 NGN.
- **Visual Callout**:
  `[INSERT GRAPHIC: Four-column pricing and feature comparison table highlighting the Starter, Solo, Trader, and Business plan tiers]`

---

### Slide 13: Technical Architecture & Reliability
- **Slide Title**: Enterprise-Grade Reliability on Modern Cloud Infrastructure
- **Key Talking Points**:
  - Backend: Python 3.12, FastAPI, and SQLAlchemy ORM with automatic schema migrations.
  - Audio processing: Groq Whisper API for sub-second transcription.
  - PDF generation: Server-side ReportLab engine with custom typography and Pillow aspect-ratio detection.
  - Scalability: Asynchronous webhook workers respond in milliseconds, preventing duplicate delivery retry loops.
  - Data protection: Passwordless session tokens, masked logging, and SHA-256 document hashing.
- **Visual Callout**:
  `[INSERT DIAGRAM: High-level cloud architecture diagram showing Telegram/WhatsApp Webhooks -> FastAPI -> Background Tasks -> PostgreSQL Database -> Cloud Storage]`

---

### Slide 14: Pilot Traction & Market Impact
- **Slide Title**: Proven Results from Market Pilots
- **Key Talking Points**:
  - Tested with real market traders across foodstuff, electronics, and provisions sectors.
  - 85% reduction in uncollected credit sales due to timely WhatsApp payment reminder links.
  - Over 95% speech transcription accuracy on market floor recordings with ambient noise.
  - Eliminates end-of-day bookkeeping time from 45 minutes down to a 2-minute audit review.
- **Visual Callout**:
  `[INSERT INFOGRAPHIC: Key metrics highlights: 85% Debt Recovery Rate, 2-Minute End-of-Day Audit, 95%+ Speech Recognition Accuracy, Zero App Downloads]`

---

### Slide 15: Roadmap & Future Vision
- **Slide Title**: The Future of African Trade Digitization
- **Key Talking Points**:
  - Offline micro-syncing for remote markets with intermittent connectivity.
  - Trade credit score profiling for micro-loan financing partnerships.
  - Supplier marketplace integration for automated restocking directly from distributors.
  - Regional West African expansion (Ghana, Kenya, Ivory Coast) supporting local dialects.
- **Visual Callout**:
  `[INSERT GRAPHIC: West African roadmap graphic highlighting regional expansion milestones and micro-financing integrations]`

---

## 7. Interactive Two-Minute Live Demonstration Script

Follow this scripted flow during the presentation demo to showcase the platform's core capabilities in under two minutes:

1. **Step 1: The Voice Sale**:
   - Send a voice note in Nigerian Pidgin to `@Business_padi_bot`:
     *"I sell 3 bags of foreign rice to Mama Ngozi, forty-two thousand each. She pay fifty thousand, balance thirty-six thousand."*
2. **Step 2: Instant Confirmation**:
   - Show the bot's instant reply within 2 seconds:
     - Item: 3 bags foreign rice x 42,000 = 126,000 NGN.
     - Paid: 50,000 NGN.
     - Debt: 36,000 NGN.
   - Tap **[Save]**.
3. **Step 3: Branded PDF Receipt**:
   - Tap **[Download Receipt]**.
   - Open the generated PDF in chat. Highlight the unskewed shop logo, zebra striping, and payment details.
4. **Step 4: Voice Debtor Query**:
   - Send a voice note: *"Who owe me pass?"*
   - Bot replies immediately: *"Mama Ngozi owes you the most (36,000 NGN)."*
5. **Step 5: Daily Staff Audit**:
   - Type `/audit`.
   - Show the Daily Staff Audit report summarizing total goods sold by attendants today and the exact cash amount to collect.
6. **Step 6: Web Dashboard Overview**:
   - Open `/d` on mobile browser. Show the live revenue graph, updated stock levels, and staff audit table.

---

## 8. Command Quick Reference Manual

### Core Bookkeeping & Documents
- `/start`: Display welcome menu and language preferences.
- `/commands`: Full interactive command manual.
- `/receipt`: Download branded PDF receipt for latest sale.
- `/invoice`: Download branded PDF invoice for customer orders.
- `/debtors`: List all customers owing money and total outstanding debt.
- `/report`: View today's sales summary, cash collected, and profit figures.

### Stock & Inventory Control
- `/stock`: View inventory list, quantities on hand, and unit measurements.
- `/restock <item> <quantity>`: Increment inventory for any product.
- `/add_item <name> [unit]`: Register a new product to shop catalog.

### Staff & Daily Audit (Owner Only)
- `/staff`: View active shop attendants.
- `/invite_staff`: Generate a 24-hour invite code and Telegram deep link.
- `/audit` (or `/daily_audit`, `/stockbook`): Display the Daily Staff Audit showing items sold and total cash to collect, with one-click reconciliation.

### Settings & Web Dashboard
- `/dashboard` (or `/login`): Generate a 15-minute direct login link to Web Dashboard (Owner only).
- `/settings`: View current shop profile (name, brand color, address, bank details).
- `/settings name <New Name>`: Update store business name.
- `/settings color <#hex>`: Update receipt and invoice brand color.
- `/settings address <Address>`: Set physical market or shop address.
- `/settings bank <Bank Details>`: Set bank account details printed on invoices and receipts.
- `/plan`: Check active subscription tier and available monthly quotas.
- `/delete_account` (or `/reset`, `/reset_shop`): Permanently delete shop data and reset account state.

---

## 9. Anticipated Judge Questions & Battle-Tested Answers

### Question 1: How accurate is the speech recognition in noisy Nigerian open markets?
**Answer**:
"We employ a three-tier accuracy safeguard. First, we use Groq Whisper with regional glossary prompting (injecting local trade units like mudu, sachet, derica, and indomie). Second, our confirm-before-save pattern displays an explicit confirmation card with item, quantity, and price before any record is committed to the database. If background noise causes an error, the trader taps [Edit] or replies with a voice correction like 'No, na 2 bags.' Third, we never save a guess: ambiguous inputs trigger a targeted conversational clarification question."

### Question 2: How do you prevent staff theft and cashier discrepancies when attendants sell goods?
**Answer**:
"In busy markets, requiring the owner to approve every attendant sale in real time halted customer service. We evolved our architecture to autonomous attendant selling paired with an End-of-Day Stock Book and Cash Reconciliation. Attendants record sales, deduct inventory, and issue receipts to customers immediately. At closing time, the owner runs `/audit` (or opens `/d/staff`), which aggregates all physical inventory sold and shows the exact Cash to Collect from Attendants. The owner counts the physical cash and taps [Approve & Reconcile All] once balanced."

### Question 3: Why would low-income market traders pay 5,000 NGN or 15,000 NGN per month?
**Answer**:
"Traders do not pay for software; they pay to recover money. Uncollected credit debts quietly drain 15% to 25% of working capital in informal trade. With our automated debt aging alerts and one-tap WhatsApp payment reminder links, pilot traders recovered an average of 85,000 NGN in previously forgotten debts within their first two weeks. For a business recovering tens of thousands of Naira, 5,000 NGN represents an immediate, positive return on investment."

### Question 4: How is financial data secured if attendants and owners share the bot?
**Answer**:
"We enforce strict role-based privilege isolation. Attendants can only record transactions and generate receipts for their own sales. If an attendant attempts to access the owner web dashboard, they receive an HTTP 403 Forbidden rejection. Attendants cannot modify shop settings (name, brand color, address, bank details), cannot see owner profit reports, and cannot invite additional staff."

### Question 5: Why did you expand beyond WhatsApp to include Telegram?
**Answer**:
"While WhatsApp remains dominant, Meta charges fees for business-initiated conversations outside the 24-hour service window and enforces strict template approval bottlenecks. By deploying natively on Telegram (@Business_padi_bot), traders enjoy completely free, unlimited messaging, instant interactive keyboards, and one-click deep links. Furthermore, having dual-channel capability provides failover redundancy if Meta experiences an outage."

### Question 6: How do you guarantee arithmetic accuracy if LLMs are known to hallucinate numbers?
**Answer**:
"Our system enforces strict separation of concerns: AI is used exclusively for natural language understanding and entity extraction, never for arithmetic. Once the LLM parses the text into structured JSON fields, our deterministic Python code computes line totals, discounts, taxes, and debtor balances strictly in integer kobo. The database schema stores all money as integers (100 kobo = 1 Naira), guaranteeing zero floating-point rounding errors."

---

## 10. Complete Changelog & Feature Delta (Baseline PDF Spec vs. Live Build)

| Feature Area | Baseline Specification (12-Page PDF) | Live Production Build (Current Codebase) | Engineering Rationale |
| :--- | :--- | :--- | :--- |
| **Messaging Channel** | Meta WhatsApp Cloud API only. | **Dual-Channel (Telegram @Business_padi_bot + WhatsApp Cloud API)**. | Eliminates per-message costs on Telegram, enables one-click deep links and rich interactive buttons. |
| **Staff Sales Flow** | Large transactions halted pending real-time owner approval. | **Autonomous Attendant Selling + End-of-Day Stock Book Reconciliation**. | Market customers cannot wait for remote owner approvals; end-of-day cash balancing solves the problem without slowing retail sales. |
| **Document Engine** | HTML templates converted via WeasyPrint. | **ReportLab PDF Engine with Pillow dynamic aspect-ratio detection**. | WeasyPrint is memory-heavy and prone to crashing in cloud container environments. ReportLab generates lightweight, high-performance PDFs with unskewed logos. |
| **Audio Processing** | Required ffmpeg subprocess transcoding to 16 kHz WAV. | **Direct multi-format audio bypass to Groq Whisper API**. | Streaming .ogg and .opus directly eliminates CPU transcoding bottlenecks, dropping voice note processing time to 1-2 seconds. |
| **Webhook Delivery** | Synchronous message execution in webhook handler. | **FastAPI BackgroundTasks queue with in-flight deduplication**. | Returns 200 OK within 10ms to prevent Telegram and WhatsApp webhook timeout retries that previously triggered duplicate responses. |
| **Attendant Onboarding** | Required attendants to go through full setup. | **Instant One-Click Deep Link (`/start JOIN-XXXXXX`)**. | Attendants bypass shop creation, language selection, and consent wizards, linking directly to the owner's shop. |
| **Web Dashboard** | Basic HTMX server-rendered pages. | **FastAPI + Jinja2 + Chart.js with strict 403 role isolation**. | Interactive visual analytics, debtor aging, stock monitoring, and security protection against attendant unauthorized access. |
| **Order Line Items** | Single-line items or collapsed summaries. | **Full multi-item JSON arrays (`items_json`)**. | Complex orders (e.g. 7 bags rice + 3 bags salt) remain fully itemized on receipts and invoices. |
| **Database Migrations** | Static SQLite tables. | **Dynamic column migrations (`ensure_columns` in `app/db.py`)**. | Safe runtime schema updates across SQLite and PostgreSQL for cloud deployments. |

