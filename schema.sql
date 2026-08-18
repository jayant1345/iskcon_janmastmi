-- ============================================================
--  ISKCON JANMASTHAMI DEVOTEE GATHERING SYSTEM — MySQL Schema
--  Run this ONCE to set up the database
--  mysql -u root -p < schema.sql
-- ============================================================

CREATE DATABASE IF NOT EXISTS iskcon_janmastmi_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE iskcon_janmastmi_db;

-- ── USERS TABLE ──
CREATE TABLE IF NOT EXISTS users (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(50)   NOT NULL UNIQUE,
    password_hash VARCHAR(256)  NOT NULL,
    name          VARCHAR(150)  NOT NULL,
    mobile        VARCHAR(15)   NOT NULL,
    upi_id        VARCHAR(100)  NULL,          -- Real UPI VPA (e.g. name@ybl), used for payment QR codes
    role          ENUM('admin','user') NOT NULL DEFAULT 'user',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_username (username)
) ENGINE=InnoDB CHARACTER SET utf8mb4;

-- ── FAMILY REGISTRATIONS TABLE ──
CREATE TABLE IF NOT EXISTS registrations (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    token            VARCHAR(10)  NOT NULL UNIQUE,   -- e.g. 001, 002, 003
    name             VARCHAR(150) NOT NULL,           -- Family head name
    address          TEXT         NOT NULL,           -- Full address
    mobile           VARCHAR(15)  NOT NULL,           -- 10-digit mobile
    persons          INT          NOT NULL DEFAULT 1, -- Number of family members
    paid             INT          NOT NULL DEFAULT 0, -- Total amount (token + aarti + abhishek + donation)
    token_amount     INT          NOT NULL DEFAULT 0, -- persons x settings.token_rate at time of booking
    aarti_amount     INT          NOT NULL DEFAULT 0, -- Optional Aarti seva amount
    abhishek_amount  INT          NOT NULL DEFAULT 0, -- Optional Abhishek seva amount
    donation_amount  INT          NOT NULL DEFAULT 0, -- Optional donation, 80G receipt issued separately
    payment_mode     VARCHAR(10)  NOT NULL DEFAULT 'cash', -- 'cash', 'upi', 'free', 'pending', or 'razorpay' -- how payment was confirmed
    payment_ref      VARCHAR(64)  NULL,                -- Razorpay payment ID (or other gateway reference), for reconciliation
    free_entry       TINYINT(1)   NOT NULL DEFAULT 0, -- Free Entry flag (zeroes token_amount only)
    category         VARCHAR(10)  NOT NULL DEFAULT 'volunteer', -- 'volunteer' (staff-entered) or 'online' (public self-registration)
    registered_by    INT          NULL,               -- FK to users.id, NULL for online self-registrations
    collected_by     INT          NULL,               -- FK to users.id -- volunteer who later collected a pending online payment
    reg_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_token (token),
    INDEX idx_mobile (mobile),
    INDEX idx_reg_by (registered_by)
) ENGINE=InnoDB CHARACTER SET utf8mb4;

-- ── ATTENDANCE TABLE ──
CREATE TABLE IF NOT EXISTS attendance (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    token       VARCHAR(10)  NOT NULL UNIQUE,   -- matches registrations.token
    name        VARCHAR(150) NOT NULL,
    persons     INT          NOT NULL DEFAULT 1, -- Family members counted at gate
    paid        INT          NOT NULL DEFAULT 0,
    mobile      VARCHAR(15),
    gate_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    scanned_by  INT          NULL,
    FOREIGN KEY (token) REFERENCES registrations(token) ON DELETE CASCADE,
    INDEX idx_token (token)
) ENGINE=InnoDB CHARACTER SET utf8mb4;

-- ── TOKEN COUNTER TABLE ──
-- Two independent series: id=1 is volunteer/one-on-one registrations (pass numbers
-- start at 1000), id=2 is online self-registration via the public /register page --
-- e.g. Razorpay links shared on social media (starts at 5000).
CREATE TABLE IF NOT EXISTS token_counter (
    id      INT PRIMARY KEY DEFAULT 1,
    current INT NOT NULL DEFAULT 0
) ENGINE=InnoDB;

-- Seed both counters at their baseline (first token issued will be baseline + 1)
INSERT IGNORE INTO token_counter (id, current) VALUES (1, 999);
INSERT IGNORE INTO token_counter (id, current) VALUES (2, 4999);

-- ── ATTENDANCE LOG TABLE ── (per-scan-event log, used for accurate hourly footfall)
CREATE TABLE IF NOT EXISTS attendance_log (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    token      VARCHAR(10)  NOT NULL,
    persons    INT          NOT NULL DEFAULT 1,
    scanned_by INT          NULL,
    scan_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_token (token),
    INDEX idx_scan_time (scan_time)
) ENGINE=InnoDB CHARACTER SET utf8mb4;

-- ── SETTINGS TABLE ── (admin-adjustable pricing, single row)
CREATE TABLE IF NOT EXISTS settings (
    id             INT PRIMARY KEY DEFAULT 1,
    token_rate     INT NOT NULL DEFAULT 20,   -- Rs per person entry token charge
    aarti_price    INT NOT NULL DEFAULT 101,  -- Suggested Aarti seva price
    abhishek_price INT NOT NULL DEFAULT 251,  -- Suggested Abhishek seva price
    updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    tz_backfilled  TINYINT(1) NOT NULL DEFAULT 0 -- Internal flag: one-time UTC->IST timestamp backfill has run
) ENGINE=InnoDB;

INSERT IGNORE INTO settings (id, token_rate, aarti_price, abhishek_price) VALUES (1, 20, 101, 251);

-- ── SETTLEMENTS TABLE ── (volunteer cash/UPI collected, remitted to central admin)
CREATE TABLE IF NOT EXISTS settlements (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    volunteer_id INT          NOT NULL,
    amount       INT          NOT NULL,
    note         VARCHAR(255) NULL,
    recorded_by  INT          NULL,          -- admin who recorded the remittance
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (volunteer_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_volunteer (volunteer_id)
) ENGINE=InnoDB CHARACTER SET utf8mb4;

SELECT 'ISKCON Janmashtami DB schema created successfully!' AS status;
