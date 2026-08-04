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
    role          ENUM('admin','user') NOT NULL DEFAULT 'user',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_username (username)
) ENGINE=InnoDB CHARACTER SET utf8mb4;

-- ── FAMILY REGISTRATIONS TABLE ──
CREATE TABLE IF NOT EXISTS registrations (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    token         VARCHAR(10)  NOT NULL UNIQUE,   -- e.g. 001, 002, 003
    name          VARCHAR(150) NOT NULL,           -- Family head name
    address       TEXT         NOT NULL,           -- Full address
    mobile        VARCHAR(15)  NOT NULL,           -- 10-digit mobile
    persons       INT          NOT NULL DEFAULT 1, -- Number of family members
    paid          INT          NOT NULL DEFAULT 0, -- Amount paid (Rs)
    free_entry    TINYINT(1)   NOT NULL DEFAULT 0, -- Free Entry flag
    registered_by INT          NULL,               -- FK to users.id
    reg_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
CREATE TABLE IF NOT EXISTS token_counter (
    id      INT PRIMARY KEY DEFAULT 1,
    current INT NOT NULL DEFAULT 0
) ENGINE=InnoDB;

-- Seed the initial counter row
INSERT IGNORE INTO token_counter (id, current) VALUES (1, 0);

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

SELECT 'ISKCON Janmashtami DB schema created successfully!' AS status;
