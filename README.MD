# 🏭 Edge-AI Vision & Traceability Interlock System

**An Industry 4.0 Smart Manufacturing Ecosystem for Zero-Defect Production**

This repository contains the complete source code for a unified AI Vision and Traceability Interlock System. Designed for high-volume Tier-1 manufacturing environments, this application bridges the gap between machine automation (PLCs), edge AI computer vision, and industrial barcode printing. 

It consists of two main components:
1. **The Edge-AI Terminal:** A robust Python application running on the factory floor.
2. **The Admin Secure Key:** A cross-platform mobile web app for  authorization and error handling.

### 🏆 Snapdragon AI Lab Build & Present Challenge Proposal
This project demonstrates a CPU-based Edge AI implementation (OpenCV/SIFT) successfully deployed on the factory floor. The roadmap for this project involves migrating the heavy computer vision workloads to **Snapdragon Edge AI Platforms**. Utilizing the Snapdragon NPU will reduce the hardware footprint from bulky PCs to ultra-low-power edge devices, accelerating inference times to milliseconds and enabling advanced deep learning (YOLOv8) defect detection natively at the edge.

---

## 📑 Table of Contents
1. [The Admin Secure Key (Mobile Companion App)](#-the-admin-secure-key-mobile-companion-app)
2. [Hardcore Security & Compliance Features](#-hardcore-security--compliance-features)
3. [AI Vision & Hardware Interlock Workflow](#-ai-vision--hardware-interlock-workflow)
4. [Authentication & Master Passwords](#-authentication--master-passwords)
5. [Software Architecture & Tech Stack](#-software-architecture--tech-stack)
6. [Team & Credits](#-team--credits)

---

## 📱 The Admin Secure Key (Mobile Companion App)

To manage factory floor exceptions without compromising security, we built the **Admin Secure Key**, a lightweight, cross-platform companion application written in pure HTML/CSS/JavaScript.

* **Cross-Platform Compatibility:** Runs flawlessly on any browser across **iOS, Android, Windows, and macOS**. No app store installation required.
* **The Problem:** When the AI detects a bad part, or the operator scans a duplicate/invalid QR code, the main Python terminal goes into a strict **Red Alarm Lockdown**. The machine stops. 
* **The Solution:** The locked terminal displays a randomized 4-digit "Reference Number". The floor supervisor opens the Admin Secure Key app on their mobile phone, logs in, and enters this reference number. 
* **The Cryptographic Algorithm:** The mobile app securely calculates an unlock key using a matching mathematical salt algorithm: `(Ref No * 7 + 1313) % 10000`. The supervisor enters this generated key into the locked terminal to authorize a skip, override a camera failure, or log a defect, ensuring air-gapped security.

---

## 🛡️ Hardcore Security & Compliance Features

This software is built to survive the chaotic reality of a factory floor. It includes aggressive fail-safes:

* **State Persistence (Crash-Proof):** If the PC loses power, crashes, or is accidentally closed, the system loses zero data. Upon restart, it instantly resumes from the exact shift, model, and serial number state by querying the local SQLite database.
* **Zero-Touch Serial Auto-Reset:** The system continuously monitors the system date. The moment a new calendar month begins, it seamlessly and automatically resets the serial number counter back to `000001` for all models without any human intervention.
* **Anti-Time Tampering Lock:** Factory operators sometimes roll back the Windows PC clock to manipulate shift targets or fake production records. This software tracks the "highest seen timestamp" in the database. If the PC clock is moved backward, the system instantly triggers a `🚨 TIME TAMPERING DETECTED 🚨` alarm and permanently locks production until the Admin intervenes.
* **Single Instance Enforcer:** Uses local socket binding (Port 54321) to prevent operators from opening multiple windows of the software simultaneously, completely eliminating the risk of serial number duplication or database corruption.
* **Data Isolation Protection:** The SQLite database and daily Excel audit logs are hardcoded to save to a fixed path on a secondary drive (`E:\qr_app.db` and `E:\AUDIT`). If the C: drive (Windows OS) corrupts and requires formatting, all traceability data remains perfectly safe.

---

## 🔄 AI Vision & Hardware Interlock Workflow

The genius of the system lies in its physical interlocks. An operator cannot physically bypass a failed part.

1. **Trigger:** The Mitsubishi PLC sends a signal (`M5006` High) that a part is clamped.
2. **AI Strict Inspection:** The software uses OpenCV to compare the live feed against a Master Image.
    * *Color Check:* HSV Histogram matching.
    * *Feature Check:* SIFT (Scale-Invariant Feature Transform).
    * *Geometric Check:* Homography matrix mapping. **If the part is rotated by more than 5 degrees, it fails instantly.**
3. **Physical Interlock:**
    * *If PASS:* Generates a collision-proof serial number, compiles Zebra Programming Language (ZPL), and prints a unique QR code via the TSC TE210 USB printer/ Or any Barcode and qr code label printer.
    * *If FAIL:* System locks down. An Admin must use the Mobile App to unlock the station.
4. **Closed-Loop Validation:** The operator pastes the label and scans it. The system verifies the length, model prefix (`CUSTOMER_PART_NO` + `REVISION_NO` + `VENDOR_CODE`), and checks for duplicates. Only if matched perfectly does it send the "Unlock" signal (`M5002`) back to the PLC.

---

## 🔑 Authentication & Master Passwords

*(Note: For the purpose of the Qualcomm evaluation, the master credentials are provided below. In production, these are encrypted or changed by the plant manager).*

**Desktop Terminal (Python):**
* **Admin Login Username:** `admin`
* **Admin Login Password:** `1313`
**SEPRATE LOGIN FOR EMPLOYEE-**
* **Emp Login Username:** `user1`
              Password: pass1
              emp name: xyz
* **Set Custom Serial Number Password:** `1313`
* **Universal Master Override Key:** `2026` *(Can be used to bypass the mobile app generator in emergencies)*

**Mobile Companion App (HTML):**
* **App Login PIN:** `1313`

---

## 🏗 Software Architecture & Tech Stack

* **Edge Application:** Python 3.x, Tkinter (GUI)
* **Computer Vision:** OpenCV (`cv2`), NumPy, Pillow
* **Hardware Communication:** `pymcprotocol` (Mitsubishi PLCs), `win32print` (Windows Spooler APIs for ZPL Printers)
* **Mobile App:** HTML5, CSS3, JavaScript (PWA-ready with `inputmode="numeric"` for native mobile keyboard invocation)
* **Database & Reporting:** SQLite3, Pandas (for automated daily Shift A/B/C Excel reporting)

---

## 👥 Harpreet Singh – Software Developer & Architect
