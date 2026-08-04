# 🪈 ISKCON Society — Janmashtami Devotee Gathering & Entry Gateway 2026

A full-stack, highly visual web application built with **Flask**, **MySQL**, and **Vanilla JavaScript & Stitch UI principles** to manage guest registrations, family token QR code generation, camera QR entry scanning, real-time analytics, and volunteer management for **ISKCON Janmashtami 2026**.

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Design Aesthetics](#-design-aesthetics)
- [Key Features](#-key-features)
- [Architecture & Tech Stack](#-architecture--tech-stack)
- [Project Structure](#-project-structure)
- [Database Schema](#-database-schema)
- [Environment Variables](#-environment-variables)
- [Quick Start & Installation](#-quick-start--installation)
  - [1. Prerequisites](#1-prerequisites)
  - [2. Database Setup](#2-database-setup)
  - [3. Backend Setup](#3-backend-setup)
  - [4. Running the Application](#4-running-the-application)
- [User Roles & Default Credentials](#-user-roles--default-credentials)
- [API Endpoints Reference](#-api-endpoints-reference)
- [Production Deployment (Railway / Heroku)](#-production-deployment-railway--heroku)
- [License](#-license)

---

## 🌟 Overview

The **ISKCON Janmashtami Devotee Gathering System** streamlines event access control, guest token issuance, and entry scanning:
- **Centralized Server**: Host locally on a PC within a local WiFi (LAN) network or deploy to cloud platforms (e.g. Railway, Heroku).
- **Zero App Installation**: Devotees and gate volunteers access the web application directly via mobile browsers or tablets via QR scanning.
- **Family-Wise Tokens**: Generates formatted tokens (e.g., `001`, `002`) with embedded QR codes representing the entire family unit.
- **Webcam QR Scanner**: Gatekeepers scan family QR codes directly using device cameras to check in devotees in seconds.

---

## 🎨 Design Aesthetics

Designed with modern UI principles inspired by **Stitch**:
- **Theme**: Deep Night Royal Blue (`#0B132B`, `#1E1B4B`), Radiant Gold (`#F59E0B`), and Peacock Teal (`#06B6D4`).
- **Typography**: Google Fonts `Outfit` (headings) and `Inter` (body text).
- **Glassmorphism UI**: Blur cards with subtle gold borders, glowing badge status indicators, and smooth micro-interactions.

---

## ✨ Key Features

### 👤 1. Role-Based Access Control (RBAC)
- **Session-based Authentication**: Password hashing with `Werkzeug` and HTTP-only session cookies.
- **Admin Role**: Complete access to stats dashboard, collection totals, volunteer user management, leaderboard, and dangerous data reset option.
- **User / Volunteer Role**: Devotee family registration, registration list, and gate QR attendance scanning.

### 📝 2. Devotee & Family Registration
- **Formatted Token Generation**: Auto-incrementing token counter (`001`, `002`, `003`).
- **Family Details**: Captures head of family name, mobile number (10-digit validation), address, member count (1–50), and payment or **Free Entry Pass** options.
- **Instant Printable QR Card**: Generates a dynamic QR code token card for printing or downloading.
- **One-Tap WhatsApp Delivery**: Agents can send the family's token details directly to the registered mobile number via WhatsApp (`wa.me` deep link) immediately after downloading the QR image to attach.

### 💰 3. Configurable Pricing & Donations
- **Admin-Adjustable Token Rate**: Per-person entry charge is set centrally by the admin (Volunteers & Admin → Pricing Settings) and applied automatically — volunteers never type it in, so it can't be tampered with per booking.
- **Aarti / Abhishek Seva**: Optional facilities a family can opt into at booking time, with admin-configured suggested prices that the volunteer can override per family.
- **Donations (80G)**: A separate optional donation amount, tracked distinctly from the entry/seva charges so it can be exported later for 80G tax-exemption receipt preparation.
- **Volunteer Settlement Tracking**: Every volunteer's total collection (cash/UPI they've received from families) is tracked against what they've formally remitted to the central admin, with a running balance-due shown on both the admin leaderboard and the volunteer's own dashboard.

### 🚪 4. Gate Entry & QR Attendance Scanning
- **Integrated Camera Scanner**: Scans QR codes using device webcam/camera.
- **Smart Duplicate Entry Handling**: Detects prior gate scans, displays timestamp of original entry, and allows logging extra family members arriving separately.
- **Hourly Gate Traffic**: Visual flow chart tracking attendee volume across the full event window (afternoon through midnight).

### 📊 5. Analytics & Reports
- **Real-Time Dashboard**: Monitors total registered families, total registered devotees, checked-in families, devotees inside, and pending arrivals.
- **CSV Data Export**: One-click export of complete registration & gate logs, including the token/aarti/abhishek/donation breakdown, as a clean `.csv` file.

---

## 🛠️ Architecture & Tech Stack

| Layer | Technology | Description |
|---|---|---|
| **Frontend** | HTML5, CSS3, JS (ES6) | Single Page Application (SPA) with QR generator & HTML5 camera scanner |
| **Backend** | Python 3.8+ / Flask | RESTful API backend handling sessions, auth, and database operations |
| **Database** | MySQL 5.7+ / MariaDB | Relational database (`iskcon_janmastmi_db`) with auto-migrations |
| **Server / WSGI** | Gunicorn | Production WSGI HTTP Server configured in `Procfile` |

---

## 📁 Project Structure

```
iskcon_janmastmi/
├── app.py            # Main Flask API backend, authentication & database logic
├── index.html        # Responsive Single Page Application (SPA) frontend
├── schema.sql        # MySQL database creation script
├── Procfile          # Production process file for Gunicorn deployment
├── requirements.txt  # Python package dependencies
└── README.md         # Detailed application documentation
```

---

## 🗄️ Database Schema

The database `iskcon_janmastmi_db` consists of 7 main tables:

1. **`users`**: System users (admins and volunteer operators).
2. **`registrations`**: Family registration records with token, contact info, member count, and a payment breakdown (token/aarti/abhishek/donation amounts plus the total `paid`).
3. **`attendance`**: One row per token with the current total persons checked in (used for duplicate-scan detection).
4. **`attendance_log`**: One row per scan event (initial entry + each "add more arriving separately"), used to compute accurate hourly footfall.
5. **`token_counter`**: Single-row tracking table managing auto-incrementing token numbers.
6. **`settings`**: Single-row table holding the admin-adjustable token rate and suggested Aarti/Abhishek prices.
7. **`settlements`**: Log of cash/UPI amounts each volunteer has formally remitted to the central admin, used to compute their outstanding balance due.

---

## ⚙️ Environment Variables

| Variable | Default Value | Description |
|---|---|---|
| `SECRET_KEY` | Random Hex | Session encryption key |
| `MYSQLHOST` | `localhost` | MySQL server host |
| `MYSQLPORT` | `3306` | MySQL server port |
| `MYSQLUSER` | `root` | MySQL database username |
| `MYSQLPASSWORD` | `root` | MySQL database password |
| `MYSQL_DATABASE` | `iskcon_janmastmi_db` | Database name |
| `ADMIN_PASSWORD` | `admin123` | Initial admin account password |
| `ADMIN_MOBILE` | `0000000000` | Initial admin account mobile number |
| `PORT` | `5005` | Application HTTP server port |
| `RAILWAY_ENVIRONMENT` | *None* | Enables secure HTTPS cookies when set |

---

## 🚀 Quick Start & Installation

### 1. Prerequisites
- **Python**: 3.8 or higher installed and added to PATH.
- **MySQL / MariaDB**: Server running on port 3306.

### 2. Database Setup
Execute `schema.sql` to initialize the database:

```bash
mysql -u root -p < schema.sql
```

### 3. Backend Setup
Navigate into the application directory and install dependencies:

```bash
cd c:\Project_AI\iskcon_janmastmi
pip install -r requirements.txt
```

### 4. Running the Application

Start the Flask application server:

```bash
python app.py
```

Console Output:
```text
  🪈  ISKCON SOCIETY — JANMASTHAMI CELEBRATION 2026
  Devotee Gathering & Gate Attendance Gateway
=================================================================
  Local Access:   http://localhost:5001
  LAN Access:     http://<YOUR_LAN_IP>:5001
  Default Login:  admin / admin123
=================================================================
```

---

## 🔑 User Roles & Default Credentials

On initial launch, the system creates the default administrator account:

- **Username**: `admin`
- **Password**: `admin123` *(or configured via `ADMIN_PASSWORD` env var)*
- **Role**: `admin`

Admins can create additional volunteer accounts through the Admin tab.

---

## 📡 API Endpoints Reference

### Auth
- `POST /api/auth/login` — Authenticate user session
- `POST /api/auth/logout` — Terminate active session
- `GET /api/auth/me` — Retrieve active user session info

### Admin Users
- `GET /api/users` — List system user accounts
- `POST /api/users` — Create volunteer or admin user
- `PUT /api/users/<id>` — Update user details or password
- `DELETE /api/users/<id>` — Delete user account

### Registration & Gate Scan
- `GET /api/registrations` — Fetch all registrations
- `POST /api/register` — Register family and generate token (server computes token/aarti/abhishek/donation total)
- `POST /api/gate/scan` — Process QR token gate entry & update attendee counts
- `GET /api/attendance` — Fetch gate attendance log
- `GET /api/attendance/hourly` — Get hourly attendee flow data (12 PM–12 AM)

### Pricing Settings
- `GET /api/settings` — Current token rate and suggested Aarti/Abhishek prices
- `PUT /api/admin/settings` — Update token rate and suggested prices (Admin only)

### Volunteer Settlements
- `GET /api/admin/settlements` — List all cash/UPI remittances recorded for volunteers (Admin only)
- `POST /api/admin/settlements` — Record a volunteer's remittance to the central admin (Admin only)
- `GET /api/my-stats` — A volunteer's own collection, submitted, and balance-due totals

### Analytics & Reports
- `GET /api/stats` — Overall registration & attendance counts (admin view includes collection breakdown)
- `GET /api/admin/user-stats` — Volunteer leaderboard with collection, submitted, and balance-due stats
- `GET /api/export/csv` — Export complete CSV report including the fee breakdown
- `POST /api/admin/clear` — Reset registration & attendance records (Admin only)

---

## 🌐 Production Deployment (Railway / Heroku)

The project includes a `Procfile` configured for Gunicorn:

```text
web: gunicorn app:app --workers 4 --threads 2 --timeout 60 --bind 0.0.0.0:$PORT
```

When deploying to Railway:
1. Connect the repository.
2. Provision a MySQL database service.
3. Configure Environment Variables (`MYSQLHOST`, `MYSQLUSER`, `MYSQLPASSWORD`, `MYSQLPORT`, `MYSQL_DATABASE`, `SECRET_KEY`).
4. Railway will automatically launch the Gunicorn server.

---

## 📜 License

Created for **ISKCON Society — Janmashtami Celebration 2026**. All rights reserved.
