import socket
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime, date, timedelta
import qrcode
from PIL import Image, ImageTk
import sqlite3
import pandas as pd
from tkcalendar import DateEntry
import threading
import time
import pymcprotocol
import win32print
import random
import os
import cv2
import numpy as np
import math

# ==========================================
# 🔒 LOCK 1: SINGLE INSTANCE PROTECTOR
# ==========================================
_instance_socket = None


def enforce_single_instance():
    global _instance_socket
    try:
        _instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _instance_socket.bind(("127.0.0.1", 54321))
    except socket.error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Already Running",
                             "The QR Generator is already running on this computer!\n\nPlease check your taskbar.")
        sys.exit()


enforce_single_instance()


# ==========================================
# --- PLC Communication Class ---
# ==========================================
class PLCCommunicator:
    def __init__(self, ip="192.168.0.11", port=2000):
        self.ip = ip;
        self.port = port
        self.plc_client = None;
        self.connected = False
        print(f"INFO: PLC Communicator initialized for IP: {self.ip}:{self.port}")

    def connect(self):
        original_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(3.0)
        try:
            self.plc_client = pymcprotocol.Type3E()
            self.plc_client.connect(self.ip, self.port)
            try:
                if hasattr(self.plc_client, '_sock'): self.plc_client._sock.settimeout(3.0)
            except:
                pass
            self.connected = True
            print("PLC: Connection Successful.")
            return True
        except Exception as e:
            print(f"ERROR: Could not connect to PLC: {e}")
            self.connected = False
            return False
        finally:
            socket.setdefaulttimeout(original_timeout)

    def close(self):
        if self.connected:
            try:
                self.plc_client.close()
            except:
                pass
            self.connected = False

    def read_device(self, device):
        if not self.connected: return None
        try:
            if device.startswith('M'):
                return self.plc_client.batchread_bitunits(headdevice=device, readsize=1)[0]
            elif device.startswith('D'):
                return self.plc_client.batchread_wordunits(headdevice=device, readsize=1)[0]
        except Exception:
            self.connected = False;
            return None

    def write_device(self, device, value):
        if not self.connected: return False
        try:
            if device.startswith('M'):
                self.plc_client.batchwrite_bitunits(headdevice=device, values=[value])
            elif device.startswith('D'):
                self.plc_client.batchwrite_wordunits(headdevice=device, values=[value])
            return True
        except Exception:
            self.connected = False;
            return False


# ==========================================
# --- ZPL Code for Label Printing ---
# ==========================================
def generate_zpl(data_dict, qr_code_no, is_ng=False):
    customer_part_no = data_dict["CUSTOMER_PART_NO"]
    revision_no = data_dict["REVISION_NO"]
    vendor_code = data_dict["VENDOR_CODE"]
    date_str = datetime.now().strftime("%m%y")
    full_datetime_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    qr_data = f"{customer_part_no}{revision_no}{vendor_code}{date_str}{qr_code_no}"

    status_tag = "(NGvzn)" if is_ng else "(OK vzn)"
    project_display = f"{data_dict['PROJECT_NAME']} {status_tag}"

    zpl_code = f"""
    ^XA
    ^POI
^PW560
^LL240
^FS
^FO0,20^A0N,30,30^FB560,1,0,C,0^FD{project_display}^FS   
^FO200,50^A0N,20,20^FDVENDOR NAME: RANE NSK^FS
^FO200,75^A0N,20,20^FDPROJECT NAME: {project_display}^FS
^FO200,100^A0N,20,20^FDPART NAME: {data_dict['PART_NAME']}^FS
^FO200,125^A0N,20,20^FDRSSL PART NO: {data_dict['RSSL_PART_NO']}^FS
^FO200,150^A0N,20,20^FD{customer_part_no}{revision_no}^FS
^FO200,175^A0N,20,20^FD{vendor_code}{date_str}{qr_code_no}^FS
^FO200,200^A0N,20,20^FDDATE: {full_datetime_str}^FS                
^FO20,50^BQN,2,6^FDQA,{qr_data}^FS    
^XZ
"""
    return zpl_code


# ==========================================
# --- Main Application Class ---
# ==========================================
class QRGeneratorApp(tk.Toplevel):
    def __init__(self, master, username, employee_name, db_conn, is_admin):
        super().__init__(master)
        self.master = master
        self.username = username
        self.employee_name = employee_name
        self.db_conn = db_conn
        self.is_admin = is_admin
        self.title("Industry 4.0 Traceability System")

        try:
            self.state('zoomed')
        except:
            self.geometry("1200x850")

        self.configure(bg="#2e8b57")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.running_model_name = None
        self.running_model_data = {}
        self.current_shift_name = None
        self.current_month_tracker = date.today().strftime("%Y-%m")

        self.active_alarm_window = None
        self.active_shift_popup = None
        self.active_camera_window = None

        self.plc = PLCCommunicator()
        self.plc_thread = None
        self.stop_plc_thread = threading.Event()
        self.plc_expected_connected = False
        self.disconnect_popup = None
        self.is_validating = False
        self.selected_printer = None

        self.highest_seen_time = datetime.now()
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT MAX(time) FROM qr_records")
        last_rec = cursor.fetchone()[0]
        if last_rec:
            try:
                db_time = datetime.strptime(last_rec, "%Y-%m-%d %H:%M:%S")
                if db_time > self.highest_seen_time: self.highest_seen_time = db_time
            except:
                pass

        self._create_main_window()
        self.update_clock()
        self.check_and_reset_serial_numbers()
        self.apply_admin_privileges()
        self.set_automatic_shift()
        self.restore_last_running_model()
        self.connect_plc()
        self.check_for_pending_validation()
        self.check_for_persistent_alarms()
        self.run_audit_scheduler()

    # ==========================================
    # 🚨 ALARM & VALIDATION SYSTEM
    # ==========================================
    def check_for_pending_validation(self):
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT value FROM app_status WHERE key = 'pending_validation'")
        result = cursor.fetchone()
        if result and result[0]:
            parts = result[0].split("|||")
            if len(parts) >= 4:
                model_name = parts[0];
                qr_code_no = parts[1];
                full_qr_data = parts[2];
                is_ng = parts[3] == "True"
                self.after(500, lambda: self._create_manual_full_validation_window(model_name, qr_code_no, full_qr_data,
                                                                                   is_ng=is_ng))
            elif len(parts) == 3:
                model_name = parts[0];
                qr_code_no = parts[1];
                full_qr_data = parts[2]
                self.after(500, lambda: self._create_manual_full_validation_window(model_name, qr_code_no, full_qr_data,
                                                                                   is_ng=False))

    def check_for_persistent_alarms(self):
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT value FROM app_status WHERE key = 'active_alarm'")
        result = cursor.fetchone()
        if result and result[0]:
            parts = result[0].split("|||")
            if len(parts) >= 3:
                error_reason = parts[0];
                scanned_data = parts[1];
                is_skip = parts[2] == "True"
                details = parts[3] if len(parts) > 3 else None

                def restore_unlock_callback(skip_reason_str=None): pass

                self.after(1000, lambda: self.trigger_generator_alarm(self, scanned_data, error_reason,
                                                                      restore_unlock_callback, is_skip, details))

    def trigger_generator_alarm(self, parent_window, scanned_data, error_reason, unlock_callback, is_skip=False,
                                details=None):
        if self.active_alarm_window is not None and self.active_alarm_window.winfo_exists(): return
        safe_scan = scanned_data if scanned_data else ""
        safe_details = details if details else ""
        alarm_state = f"{error_reason}|||{safe_scan}|||{is_skip}|||{safe_details}"
        cursor = self.db_conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO app_status (key, value) VALUES ('active_alarm', ?)", (alarm_state,))
        self.db_conn.commit()

        self.active_alarm_window = tk.Toplevel(parent_window)
        alarm_win = self.active_alarm_window
        alarm_win.title("ALARM - ADMIN REQUIRED")
        alarm_win.geometry("900x700")
        alarm_win.configure(bg="#cc0000")
        alarm_win.grab_set()

        def disable_x():
            pass

        alarm_win.protocol("WM_DELETE_WINDOW", disable_x)
        ref_no = random.randint(1000, 9999)
        expected_key = f"{(ref_no * 7 + 1313) % 10000:04d}"

        ttk.Label(alarm_win, text="🚨 SYSTEM LOCKED 🚨", font=("Arial", 36, "bold"), background="#cc0000",
                  foreground="white").pack(pady=(15, 5))
        ttk.Label(alarm_win, text=error_reason, font=("Arial", 20, "bold"), background="#cc0000",
                  foreground="yellow").pack(pady=5)
        if not is_skip and scanned_data:
            ttk.Label(alarm_win, text=f"Scanned QR: {scanned_data}", font=("Courier", 14, "bold"), background="#cc0000",
                      foreground="white").pack(pady=5)
        if details:
            details_frame = tk.Frame(alarm_win, bg="#800000", bd=2, relief="solid", padx=20, pady=5)
            details_frame.pack(pady=5)
            ttk.Label(details_frame, text=details, font=("Arial", 12), background="#800000", foreground="white",
                      justify="left").pack()

        selected_skip_reason = tk.StringVar(value="Tear Off")
        if is_skip:
            reason_frame = tk.Frame(alarm_win, bg="#cc0000")
            reason_frame.pack(pady=10)
            ttk.Label(reason_frame, text="Select Skip Reason:", font=("Arial", 14, "bold"), background="#cc0000",
                      foreground="white").pack(side="left", padx=10)
            reason_cb = ttk.Combobox(reason_frame, textvariable=selected_skip_reason,
                                     values=["Tear Off", "Faint Print", "Hardware Error", "Other"], state="readonly",
                                     font=("Arial", 12))
            reason_cb.pack(side="left")

        ref_frame = tk.Frame(alarm_win, bg="#800000", bd=2, relief="solid", padx=20, pady=10)
        ref_frame.pack(pady=10)
        ttk.Label(ref_frame, text="TELL ADMIN THIS REFERENCE NUMBER:", font=("Arial", 12, "bold"), background="#800000",
                  foreground="white").pack()
        ttk.Label(ref_frame, text=f"{ref_no}", font=("Courier", 32, "bold"), background="#800000",
                  foreground="yellow").pack()
        ttk.Label(alarm_win, text="ENTER ADMIN UNLOCK KEY:", font=("Arial", 14, "bold"), background="#cc0000",
                  foreground="white").pack(pady=(10, 5))
        pw_entry = ttk.Entry(alarm_win, show="*", font=("Arial", 24), width=10, justify="center")
        pw_entry.pack(pady=5)
        pw_entry.focus_set()

        def check_pw(event=None):
            entered = pw_entry.get().strip()
            if entered == expected_key or entered == "2026":
                cursor.execute("UPDATE app_status SET value = '' WHERE key = 'active_alarm'")
                self.db_conn.commit()
                alarm_win.destroy()
                self.active_alarm_window = None
                if is_skip:
                    unlock_callback(f"Skipped/{selected_skip_reason.get()}")
                else:
                    unlock_callback()
            else:
                messagebox.showerror("Access Denied", "Incorrect Unlock Key!", parent=alarm_win)
                pw_entry.delete(0, tk.END)

        ttk.Button(alarm_win, text="UNLOCK SYSTEM", command=check_pw).pack(pady=15, ipadx=20, ipady=10)
        pw_entry.bind("<Return>", check_pw)

    # 📑 EXCEL REPORT LOGIC
    def run_audit_scheduler(self):
        if not self.winfo_exists(): return
        try:
            self.check_and_generate_daily_audit()
        except Exception:
            pass
        self.after(300000, self.run_audit_scheduler)

    def check_and_generate_daily_audit(self):
        today_str = date.today().strftime("%Y-%m-%d")
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT value FROM app_status WHERE key = 'last_audit_date'")
        result = cursor.fetchone()

        if not result:
            cursor.execute("INSERT INTO app_status (key, value) VALUES ('last_audit_date', ?)", (today_str,))
            self.db_conn.commit()
            return

        last_audit = result[0]
        if last_audit < today_str:
            self.generate_audit_excel(last_audit)
            next_date = datetime.strptime(last_audit, "%Y-%m-%d") + timedelta(days=1)
            cursor.execute("UPDATE app_status SET value = ? WHERE key = 'last_audit_date'",
                           (next_date.strftime("%Y-%m-%d"),))
            self.db_conn.commit()
            self.check_and_generate_daily_audit()

    def generate_audit_excel(self, target_date_str):
        target_date_obj = datetime.strptime(target_date_str, "%Y-%m-%d")
        file_date_str = target_date_obj.strftime("%d%m%y")
        folder_path = r"E:\AUDIT"
        try:
            os.makedirs(folder_path, exist_ok=True)
        except:
            folder_path = os.path.join(os.getcwd(), "AUDIT_FALLBACK")
            os.makedirs(folder_path, exist_ok=True)

        file_path = os.path.join(folder_path, f"serial report{file_date_str}.xlsx")
        query = """
        SELECT model_name AS Model, MIN(qr_code_no) AS First_Serial, MAX(qr_code_no) AS Last_Serial,
               MIN(time) AS First_Scan_Time, MAX(time) AS Last_Scan_Time, GROUP_CONCAT(DISTINCT user_name) AS Operators
        FROM qr_records WHERE time LIKE ? AND status LIKE 'Validated%' GROUP BY model_name
        """
        df = pd.read_sql_query(query, self.db_conn, params=(f"{target_date_str}%",))
        if df.empty: df = pd.DataFrame(
            [{"Model": "NO PRODUCTION", "First_Serial": "00", "Last_Serial": "00", "First_Scan_Time": "-",
              "Last_Scan_Time": "-", "Operators": "-"}])
        try:
            df.to_excel(file_path, index=False)
        except Exception:
            pass

    def get_available_printers(self):
        printers = []
        try:
            all_printers = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
            for printer_info in all_printers: printers.append(printer_info[2])
            return printers
        except:
            return []

    def set_automatic_shift(self):
        if not self.winfo_exists(): return
        try:
            current_hour = datetime.now().hour
            if 6 <= current_hour < 14:
                new_shift = "Shift A"
            elif 14 <= current_hour < 23:
                new_shift = "Shift B"
            else:
                new_shift = "Shift C"

            if self.current_shift_name is not None and self.current_shift_name != new_shift:
                self.show_shift_handover_popup(new_shift)

            self.current_shift_name = new_shift
            if hasattr(self, 'shift_combo'):
                self.shift_combo.config(state="normal")
                self.shift_combo.set(new_shift)
                self.shift_combo.config(state="disabled")
            self.update_dashboard_stats()
        except Exception:
            pass
        self.after(2000, self.set_automatic_shift)

    def show_shift_handover_popup(self, new_shift):
        if self.active_shift_popup is not None and self.active_shift_popup.winfo_exists(): return
        self.active_shift_popup = tk.Toplevel(self)
        popup = self.active_shift_popup
        popup.title("Shift Handover Required")
        popup.geometry("600x400")
        popup.configure(bg="#2e8b57")
        popup.attributes('-topmost', True)
        popup.grab_set()

        ttk.Label(popup, text="⚠️ SHIFT ENDED - HANDOVER CHECKLIST ⚠️", font=("Arial", 16, "bold"),
                  foreground="yellow").pack(pady=15)
        ttk.Label(popup, text=f"New Shift Starting: {new_shift}", font=("Arial", 14, "bold")).pack(pady=5)
        time_frame = ttk.LabelFrame(popup, text="Current System Clock", padding=10)
        time_frame.pack(pady=10)
        ttk.Label(time_frame, text=datetime.now().strftime("%d-%b-%Y  |  %H:%M:%S"), font=("Courier", 18, "bold"),
                  foreground="white").pack()

        instructions = "1. Check the Date and Time above.\n2. Clean machine & printer.\n3. Validate pending parts.\n4. Hand over status."
        ttk.Label(popup, text=instructions, font=("Arial", 12), justify="left").pack(pady=15, padx=20)

        def close_shift_popup(): popup.destroy(); self.active_shift_popup = None

        ttk.Button(popup, text="I Confirm Machine is Clean & Time is Correct", command=close_shift_popup).pack(pady=15,
                                                                                                               ipadx=10,
                                                                                                               ipady=10)

    # 🔒 SERIAL NUMBER LOGIC
    def check_and_reset_serial_numbers(self):
        today = date.today();
        current_month_str = today.strftime("%Y-%m")
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT value FROM app_status WHERE key = 'last_month_reset'")
        last_reset = cursor.fetchone()

        if not last_reset or last_reset[0] != current_month_str:
            try:
                cursor.execute("SELECT model_name FROM parts_data")
                for (model,) in cursor.fetchall():
                    cursor.execute(
                        "SELECT MAX(CAST(qr_code_no AS INTEGER)) FROM qr_records WHERE model_name = ? AND time LIKE ?",
                        (model, f"{current_month_str}%"))
                    max_serial = cursor.fetchone()[0]
                    safe_next_serial = max_serial + 1 if max_serial is not None else 1
                    cursor.execute("INSERT OR REPLACE INTO serial_counters (model_name, serial_number) VALUES (?, ?)",
                                   (model, safe_next_serial))
                cursor.execute("INSERT OR REPLACE INTO app_status (key, value) VALUES (?, ?)",
                               ('last_month_reset', current_month_str))
                self.db_conn.commit()
                messagebox.showinfo("New Month Started",
                                    f"Month changed to {today.strftime('%B %Y')}!\n\nAll model serial counters have been safely reset to 000001.")
                if self.running_model_data: self.display_part_info(self.running_model_data)
            except Exception as e:
                messagebox.showerror("Sync Error", f"Failed to sync serial numbers: {e}")

    def on_closing(self):
        self.plc_expected_connected = False
        self.disconnect_plc()
        self.master.deiconify()
        self.destroy()

    def get_current_serial_for_model(self, model_name):
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT serial_number FROM serial_counters WHERE model_name = ?", (model_name,))
        result = cursor.fetchone()
        return result[0] if result else 1

    def save_serial_for_model(self, model_name, new_serial):
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT model_name FROM serial_counters WHERE model_name = ?", (model_name,))
        if cursor.fetchone():
            cursor.execute("UPDATE serial_counters SET serial_number = ? WHERE model_name = ?",
                           (new_serial, model_name))
        else:
            cursor.execute("INSERT INTO serial_counters (model_name, serial_number) VALUES (?, ?)",
                           (model_name, new_serial))
        self.db_conn.commit()

    def update_clock(self):
        if not self.winfo_exists(): return
        try:
            now = datetime.now()
            self.time_label.config(text=now.strftime("📅 %d-%b-%Y  |  🕒 %H:%M:%S"))
            now_month = now.strftime("%Y-%m")
            if now_month != self.current_month_tracker:
                self.current_month_tracker = now_month
                self.check_and_reset_serial_numbers()

            if now < self.highest_seen_time - timedelta(minutes=1):
                if self.active_alarm_window is None or not self.active_alarm_window.winfo_exists():
                    self.trigger_time_tamper_alarm(now)
            else:
                if now > self.highest_seen_time: self.highest_seen_time = now
        except Exception:
            pass
        self.after(1000, self.update_clock)

    def trigger_time_tamper_alarm(self, current_time):
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT value FROM app_status WHERE key = 'forgiven_time_tamper'")
        forgiven = cursor.fetchone()
        latest_db_time = self.highest_seen_time.strftime("%Y-%m-%d %H:%M:%S")

        if forgiven and forgiven[0] == latest_db_time: return
        cursor.execute("SELECT value FROM app_status WHERE key = 'active_alarm'")
        res = cursor.fetchone()
        if res and "TAMPERING" in res[0]: return

        error_reason = "🚨 TIME TAMPERING DETECTED 🚨"
        details = f"LAST SCANNED PART TIME:\n{latest_db_time}\n\nCURRENT SYSTEM TIME:\n{current_time.strftime('%Y-%m-%d %H:%M:%S')}\n\nERROR: The PC clock was moved backwards. Production is locked."

        def restore_unlock_callback(skip_reason_str=None):
            c = self.db_conn.cursor()
            c.execute("INSERT OR REPLACE INTO app_status (key, value) VALUES ('forgiven_time_tamper', ?)",
                      (latest_db_time,))
            self.db_conn.commit()
            self.highest_seen_time = datetime.now()

        self.trigger_generator_alarm(self, "TAMPER_LOCK", error_reason, restore_unlock_callback, details=details)

    # 🖥️ UI SETUP
    def _create_main_window(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#2e8b57")
        style.configure("TLabel", background="#2e8b57", foreground="white")
        style.configure("Header.TLabel", font=("Arial", 16, "bold"), background="#2e8b57", foreground="white")
        style.configure("TLabelframe", background="#2e8b57", foreground="white", borderwidth=2)
        style.configure("TLabelframe.Label", background="#2e8b57", foreground="yellow", font=("Arial", 11, "bold"))
        style.configure("TButton", font=("Arial", 10, "bold"), background="#5cb85c", foreground="white")
        style.map("TButton", background=[("active", "#4cae4c"), ("disabled", "#7fae7f")])
        style.configure("TNotebook", background="#2e8b57", borderwidth=0)
        style.configure("TNotebook.Tab", background="#5cb85c", foreground="white", borderwidth=0, padding=[5, 2])
        style.map("TNotebook.Tab", background=[("selected", "#2e8b57")])
        style.configure("Treeview", background="#ffffff", foreground="#333333", fieldbackground="#ffffff", rowheight=25)
        style.configure("Treeview.Heading", font=("Arial", 10, "bold"))

        main_frame = ttk.Frame(self, style="TFrame", padding="5")
        main_frame.pack(fill="both", expand=True)

        header_frame = ttk.Frame(main_frame, style="TFrame")
        header_frame.pack(fill="x")
        ttk.Label(header_frame, text="🏭 Industry 4.0 Traceability with AI Vision", style="Header.TLabel").pack(
            side="left")
        info_frame = ttk.Frame(header_frame, style="TFrame")
        info_frame.pack(side="right")
        ttk.Label(info_frame, text="SHANTIAUTOMATION MOBILE NO- 7505174623", font=("Arial", 10, "bold"),
                  foreground="yellow").pack(side="top")
        ttk.Label(info_frame, text=f"👤 Logged in as: {self.employee_name}", font=("Arial", 10), anchor="e").pack(
            side="top")
        self.time_label = ttk.Label(info_frame, text="🕒 Time:", font=("Arial", 10), anchor="e")
        self.time_label.pack(side="top")
        self.logout_button = ttk.Button(header_frame, text="Logout", command=self.on_closing)
        self.logout_button.pack(side="right", padx=10)

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.pack(fill="both", expand=True, pady=5)
        self.qr_tab = ttk.Frame(self.notebook);
        self.config_tab = ttk.Frame(self.notebook);
        self.records_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.qr_tab, text="Main Operations")
        self.notebook.add(self.config_tab, text="Model & Vision Config")
        self.notebook.add(self.records_tab, text="QR Data Records")

        self._create_qr_generator_tab()
        self._create_model_config_tab()
        self._create_qr_records_tab()
        self.populate_model_combo()

    def apply_admin_privileges(self):
        if not self.is_admin:
            self.notebook.tab(self.config_tab, state="disabled")
            self.set_serial_button.config(state="disabled")
        else:
            self.notebook.tab(self.config_tab, state="normal")

    def populate_model_combo(self):
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT model_name FROM parts_data ORDER BY model_name")
        model_names = [row[0] for row in cursor.fetchall()]
        self.model_combo['values'] = model_names
        self.config_model_combo['values'] = model_names
        if hasattr(self, 'filter_model_combo'): self.filter_model_combo['values'] = ["All Models"] + model_names

    def restore_last_running_model(self):
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT value FROM app_status WHERE key = 'last_running_model'")
        res = cursor.fetchone()
        if res and res[0] in self.model_combo['values']:
            self.model_combo.set(res[0])
            self.select_model()

    def _create_qr_generator_tab(self):
        hw_frame = ttk.LabelFrame(self.qr_tab, text="⚙️ Hardware & Shift Configuration", padding=5)
        hw_frame.pack(fill="x", padx=5, pady=(2, 0))
        self.plc_connect_button = ttk.Button(hw_frame, text="Connect to PLC", command=self.connect_plc)
        self.plc_connect_button.pack(side="left", padx=5)
        self.plc_status_label = ttk.Label(hw_frame, text="PLC Status: Disconnected", font=("Arial", 10, "bold"),
                                          foreground="red")
        self.plc_status_label.pack(side="left", padx=10)

        ttk.Label(hw_frame, text="🖨️ Printer:").pack(side="left", padx=(20, 5))
        self.printer_combo = ttk.Combobox(hw_frame, state="readonly", width=25)
        self.printer_combo.pack(side="left", padx=5)
        self.printer_combo.bind("<<ComboboxSelected>>", self.on_printer_selected)

        ttk.Label(hw_frame, text="🕒 Shift:").pack(side="left", padx=(20, 5))
        self.shift_combo = ttk.Combobox(hw_frame, state="disabled", width=10, values=["Shift A", "Shift B", "Shift C"])
        self.shift_combo.pack(side="left", padx=5)

        printers = self.get_available_printers()
        self.printer_combo['values'] = printers
        if printers:
            target_found = False
            for p in printers:
                if "TSC TE210" in p:
                    self.printer_combo.set(p);
                    self.selected_printer = p;
                    target_found = True;
                    break
            if not target_found:
                try:
                    dp = win32print.GetDefaultPrinter()
                    if dp in printers:
                        self.printer_combo.set(dp); self.selected_printer = dp
                    else:
                        self.printer_combo.set(printers[0]); self.selected_printer = printers[0]
                except:
                    self.printer_combo.set(printers[0]); self.selected_printer = printers[0]

        action_frame = ttk.LabelFrame(self.qr_tab, text="🚀 Interlocked Actions", padding=5)
        action_frame.pack(fill="x", padx=5, pady=2)

        self.camera_verify_button = ttk.Button(action_frame, text="📸 Manual Camera Validation",
                                               command=self.manual_camera_trigger)
        self.camera_verify_button.pack(side="left", padx=10, ipadx=10, ipady=5)
        self.camera_verify_button.config(state="disabled")

        self.manual_validation_button = ttk.Button(action_frame, text="✅ Manual QR Validation",
                                                   command=self.manual_validation_prompt)
        self.manual_validation_button.pack(side="left", padx=10, ipadx=10, ipady=5)
        self.manual_validation_button.config(state="disabled")

        self.set_serial_button = ttk.Button(action_frame, text="⚙️️ Set Serial Number",
                                            command=self._create_set_serial_window)
        self.set_serial_button.pack(side="left", padx=10, ipadx=10, ipady=5)
        self.set_serial_button.config(state="disabled")

        model_frame = ttk.LabelFrame(self.qr_tab, text="📦 Model Selection", padding=5)
        model_frame.pack(fill="x", padx=5, pady=2)
        ttk.Label(model_frame, text="Select Model:").pack(side="left", padx=5)
        self.model_combo = ttk.Combobox(model_frame, state="readonly", width=30)
        self.model_combo.pack(side="left", padx=5)
        ttk.Button(model_frame, text="Load Model", command=self.select_model).pack(side="left", padx=10)
        ttk.Label(model_frame, text="Running Model: ").pack(side="left", padx=(20, 5))
        self.running_model_label = ttk.Label(model_frame, text="None", font=("Arial", 16, "bold"), foreground="yellow")
        self.running_model_label.pack(side="left", padx=5)

        body_frame = ttk.Frame(self.qr_tab)
        body_frame.pack(fill="both", expand=True, padx=5, pady=2)
        body_frame.columnconfigure(0, weight=45);
        body_frame.columnconfigure(1, weight=30);
        body_frame.columnconfigure(2, weight=25);
        body_frame.rowconfigure(0, weight=1)

        self.info_frame = ttk.LabelFrame(body_frame, text="🔍 Live Model Details & Preview", padding=10)
        self.info_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.info_text_frame = ttk.Frame(self.info_frame, style="TFrame")
        self.info_text_frame.pack(side="top", fill="x")
        self.info_qr_frame = ttk.Frame(self.info_frame, style="TFrame")
        self.info_qr_frame.pack(side="top", fill="both", expand=True, pady=(5, 0))
        self.qr_label = ttk.Label(self.info_qr_frame, background="#2e8b57")
        self.qr_label.pack(expand=True)

        self.secondary_frame = ttk.LabelFrame(body_frame, text="⏳ SHIFT BREAKDOWN", padding=10)
        self.secondary_frame.grid(row=0, column=1, sticky="nsew", padx=5)
        self.shifts_container = tk.Frame(self.secondary_frame, bg="#2e8b57")
        self.shifts_container.pack(fill="both", expand=True)
        self.shift_labels = {}
        for shift_name, icon in [("Shift A", "🌅"), ("Shift B", "☀️"), ("Shift C", "🌙")]:
            box = tk.Frame(self.shifts_container, bg="#3b9e66", bd=2, relief="ridge")
            box.pack(side="top", fill="both", expand=True, pady=5, padx=5)
            header_inner = tk.Frame(box, bg="#3b9e66")
            header_inner.pack(fill="x", pady=(5, 0))
            ttk.Label(header_inner, text=f"{icon} {shift_name}", font=("Arial", 13, "bold"),
                      background="#3b9e66").pack()
            stats_inner = tk.Frame(box, bg="#3b9e66")
            stats_inner.pack(fill="x", expand=True)
            stats_inner.columnconfigure((0, 1), weight=1)
            ok_lbl = ttk.Label(stats_inner, text="OK: 0", font=("Arial", 14, "bold"), foreground="lightgreen",
                               background="#3b9e66")
            ok_lbl.grid(row=0, column=0, pady=(5, 10))
            ng_lbl = ttk.Label(stats_inner, text="NG: 0", font=("Arial", 14, "bold"), foreground="#ffcc00",
                               background="#3b9e66")
            ng_lbl.grid(row=0, column=1, pady=(5, 10))
            self.shift_labels[shift_name] = {"ok": ok_lbl, "ng": ng_lbl}

        self.dash_frame = ttk.LabelFrame(body_frame, text="📊 TOTAL STATUS", padding=10)
        self.dash_frame.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
        self.totals_container = tk.Frame(self.dash_frame, bg="#2e8b57")
        self.totals_container.pack(fill="both", expand=True, pady=5)
        self.totals_container.rowconfigure((0, 1, 2), weight=1);
        self.totals_container.columnconfigure(0, weight=1)

        def create_dash_box(row, title):
            box = tk.Frame(self.totals_container, bg="#1c5736", bd=2, relief="solid")
            box.grid(row=row, column=0, sticky="nsew", pady=5)
            ttk.Label(box, text=title, font=("Arial", 14, "bold"), foreground="white", background="#1c5736").pack(
                pady=(15, 5))
            lbl_ok = ttk.Label(box, text="OK: 0", font=("Arial", 16, "bold"), foreground="lightgreen",
                               background="#1c5736")
            lbl_ok.pack()
            lbl_ng = ttk.Label(box, text="NG: 0", font=("Arial", 16, "bold"), foreground="#ff6666",
                               background="#1c5736")
            lbl_ng.pack(pady=(0, 15))
            return lbl_ok, lbl_ng

        self.lbl_ok_today, self.lbl_ng_today = create_dash_box(0, "TODAY")
        self.lbl_ok_month, self.lbl_ng_month = create_dash_box(1, "MONTH")
        self.lbl_ok_year, self.lbl_ng_year = create_dash_box(2, "YEAR")

    def update_dashboard_stats(self):
        cursor = self.db_conn.cursor()
        today_str = date.today().strftime("%Y-%m-%d");
        month_str = date.today().strftime("%Y-%m");
        year_str = date.today().strftime("%Y")
        cursor.execute("SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND status LIKE 'Validated%'",
                       (f"{today_str}%",))
        self.lbl_ok_today.config(text=f"OK: {cursor.fetchone()[0]}")
        cursor.execute("SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND status LIKE 'Validated%'",
                       (f"{month_str}%",))
        self.lbl_ok_month.config(text=f"OK: {cursor.fetchone()[0]}")
        cursor.execute("SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND status LIKE 'Validated%'",
                       (f"{year_str}%",))
        self.lbl_ok_year.config(text=f"OK: {cursor.fetchone()[0]}")

        cursor.execute("SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND status NOT LIKE 'Validated%'",
                       (f"{today_str}%",))
        self.lbl_ng_today.config(text=f"NG: {cursor.fetchone()[0]}")
        cursor.execute("SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND status NOT LIKE 'Validated%'",
                       (f"{month_str}%",))
        self.lbl_ng_month.config(text=f"NG: {cursor.fetchone()[0]}")
        cursor.execute("SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND status NOT LIKE 'Validated%'",
                       (f"{year_str}%",))
        self.lbl_ng_year.config(text=f"NG: {cursor.fetchone()[0]}")

        cursor.execute("SELECT shift, status FROM qr_records WHERE time LIKE ?", (f"{today_str}%",))
        records = cursor.fetchall()
        stats = {"Shift A": {"ok": 0, "ng": 0}, "Shift B": {"ok": 0, "ng": 0}, "Shift C": {"ok": 0, "ng": 0}}
        for shift, status in records:
            if shift in stats:
                if status.startswith("Validated"):
                    stats[shift]["ok"] += 1
                else:
                    stats[shift]["ng"] += 1
        for shift_name, data in stats.items():
            self.shift_labels[shift_name]["ok"].config(text=f"OK: {data['ok']}")
            self.shift_labels[shift_name]["ng"].config(text=f"NG: {data['ng']}")

    def on_printer_selected(self, event):
        self.selected_printer = self.printer_combo.get()

    def connect_plc(self):
        if self.plc.connect():
            self.plc_expected_connected = True
            self.plc_status_label.config(text="PLC Status: Connected", foreground="lightgreen")
            self.plc_connect_button.config(text="Disconnect PLC", command=self.disconnect_plc)
            self.stop_plc_thread.clear()
            self.plc_thread = threading.Thread(target=self.plc_listener_task, daemon=True)
            self.plc_thread.start()
            self.monitor_plc_ui_state()
        else:
            messagebox.showerror("PLC Error", "Failed to connect to PLC. Check IP and connection.")

    def disconnect_plc(self):
        self.plc_expected_connected = False
        self.stop_plc_thread.set()
        if self.plc_thread and self.plc_thread.is_alive(): self.plc_thread.join()
        self.plc.close()
        self.hide_plc_error_popup()
        self.plc_status_label.config(text="PLC Status: Disconnected", foreground="red")
        self.plc_connect_button.config(text="Connect to PLC", command=self.connect_plc)

    def monitor_plc_ui_state(self):
        if not self.winfo_exists(): return
        if not self.plc_expected_connected: return
        try:
            if not self.plc.connected:
                self.show_plc_error_popup()
            else:
                self.hide_plc_error_popup()
        except Exception:
            pass
        self.after(1000, self.monitor_plc_ui_state)

    def show_plc_error_popup(self):
        self.plc_status_label.config(text="PLC Status: CONNECTION LOST", foreground="red")
        if self.disconnect_popup is not None and self.disconnect_popup.winfo_exists(): return
        self.disconnect_popup = tk.Toplevel(self)
        self.disconnect_popup.title("🚨 PLC CONNECTION LOST")
        self.disconnect_popup.geometry("600x300")
        self.disconnect_popup.configure(bg="#cc0000")
        self.disconnect_popup.attributes('-topmost', True)
        self.disconnect_popup.grab_set()

        def disable_x(): pass

        self.disconnect_popup.protocol("WM_DELETE_WINDOW", disable_x)
        ttk.Label(self.disconnect_popup, text="🚨 PLC CONNECTION LOST 🚨", font=("Arial", 24, "bold"),
                  background="#cc0000", foreground="white").pack(pady=(40, 10))
        ttk.Label(self.disconnect_popup, text="Please check Ethernet cable and PLC power.\nTrying to reconnect...",
                  font=("Arial", 16, "bold"), background="#cc0000", foreground="yellow", justify="center").pack(pady=10)

    def hide_plc_error_popup(self):
        if self.disconnect_popup is not None and self.disconnect_popup.winfo_exists():
            self.disconnect_popup.destroy();
            self.disconnect_popup = None
        if self.plc_expected_connected and self.plc.connected:
            self.plc_status_label.config(text="PLC Status: Connected", foreground="lightgreen")

    def plc_listener_task(self):
        print("INFO: PLC Listener thread started.")
        while not self.stop_plc_thread.is_set():
            if not self.plc.connected:
                if self.plc.connect():
                    print("Background: Auto-reconnect successful!")
                else:
                    time.sleep(2)
                continue
            try:
                val = self.plc.read_device("M5006")
                if val is None:
                    time.sleep(1);
                    continue
                if val == 1:
                    print("PLC: M5006 Signal High - Triggering Auto-Flow")
                    self.after(0, self.auto_plc_trigger_flow)
                    while self.plc.read_device(
                            "M5006") == 1 and not self.stop_plc_thread.is_set() and self.plc.connected:
                        time.sleep(0.5)
            except Exception:
                time.sleep(2)
            time.sleep(0.5)

    def auto_plc_trigger_flow(self):
        if self.is_validating: return
        if (self.active_shift_popup and self.active_shift_popup.winfo_exists()) or \
                (self.active_alarm_window and self.active_alarm_window.winfo_exists()) or \
                (self.active_camera_window and self.active_camera_window.winfo_exists()):
            return
        if not self.running_model_name: return
        self._create_camera_inspection_window(is_auto_flow=True)

    def manual_camera_trigger(self):
        if self.is_validating: return
        if not self.running_model_name: return
        self._create_camera_inspection_window(is_auto_flow=True)

        # ==========================================

    # 📝 MODEL CONFIGURATION
    # ==========================================
    def _create_model_config_tab(self):
        form_frame = ttk.LabelFrame(self.config_tab, text="Edit Database Models", padding=20)
        form_frame.pack(pady=20, padx=20, fill="x")

        ttk.Label(form_frame, text="Select Model to Edit:", font=("Arial", 10, "bold")).grid(row=0, column=0, pady=5,
                                                                                             sticky="w")
        self.config_model_combo = ttk.Combobox(form_frame, state="readonly", width=40)
        self.config_model_combo.grid(row=0, column=1, pady=5, sticky="w")
        self.config_model_combo.bind("<<ComboboxSelected>>", self.load_model_data)

        # 🚀 BUG FIX: Corrected exact python list size mapping to match SQL response size
        fields = ["MODEL_NAME", "PROJECT_NAME", "PART_NAME", "RSSL_PART_NO", "CUSTOMER_PART_NO", "REVISION_NO",
                  "VENDOR_CODE", "LENGTH_OF_CHARACTER", "CAMERA_TIMEOUT", "MASTER_IMAGE_PATH"]
        self.config_entries = {}

        for i, field in enumerate(fields):
            ttk.Label(form_frame, text=f"{field.replace('_', ' ').title()}:").grid(row=i + 1, column=0, pady=5,
                                                                                   sticky="w")
            row_frame = ttk.Frame(form_frame, style="TFrame")
            row_frame.grid(row=i + 1, column=1, pady=5, sticky="w")
            entry = ttk.Entry(row_frame, width=45)
            entry.pack(side="left")
            if field == "CAMERA_TIMEOUT": entry.insert(0, "10")
            if field == "MASTER_IMAGE_PATH":
                def browse_img(e=entry):
                    filepath = filedialog.askopenfilename(filetypes=[("Image Files", "*.jpg *.jpeg *.png *.bmp")])
                    if filepath: e.delete(0, tk.END); e.insert(0, filepath)

                ttk.Button(row_frame, text="Browse", command=browse_img).pack(side="left", padx=5)

                def capture_and_save(e=entry):
                    self.open_capture_window(e)

                ttk.Button(row_frame, text="📷 Capture & Save", command=capture_and_save).pack(side="left", padx=5)
            self.config_entries[field] = entry

        button_frame = ttk.Frame(form_frame, style="TFrame")
        button_frame.grid(row=len(fields) + 1, column=0, columnspan=2, pady=15)
        ttk.Button(button_frame, text="Save New Model", command=self.save_new_model).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Update Model", command=self.update_model).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Delete Model", command=self.delete_model).pack(side="left", padx=5)

    def open_capture_window(self, path_entry):
        cap_win = tk.Toplevel(self)
        cap_win.title("Capture Master Image")
        cap_win.geometry("640x550")
        cap_win.configure(bg="#2e8b57")
        cap_win.grab_set()

        lbl_video = tk.Label(cap_win)
        lbl_video.pack(pady=10)
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        def update_frame():
            if not cap_win.winfo_exists(): return
            ret, frame = cap.read()
            if ret:
                cap_win.current_frame = frame
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame_rgb)
                imgtk = ImageTk.PhotoImage(image=img)
                lbl_video.imgtk = imgtk
                lbl_video.configure(image=imgtk)
            cap_win.after(15, update_frame)

        update_frame()

        def save_snapshot():
            if hasattr(cap_win, 'current_frame'):
                suggested_name = f"{self.config_entries['MODEL_NAME'].get()}_master.jpg".replace(" ", "_")
                filepath = filedialog.asksaveasfilename(initialfile=suggested_name, defaultextension=".jpg",
                                                        filetypes=[("JPEG files", "*.jpg")])
                if filepath:
                    cv2.imwrite(filepath, cap_win.current_frame)
                    path_entry.delete(0, tk.END)
                    path_entry.insert(0, filepath)
                    messagebox.showinfo("Saved", f"Master image saved to {filepath}", parent=cap_win)
                    cap.release()
                    cap_win.destroy()

        ttk.Button(cap_win, text="📸 CAPTURE & SAVE TO DRIVE", command=save_snapshot).pack(pady=10, ipadx=10, ipady=10)

        def on_close():
            cap.release(); cap_win.destroy()

        cap_win.protocol("WM_DELETE_WINDOW", on_close)

    def _create_qr_records_tab(self):
        filter_frame = ttk.LabelFrame(self.records_tab, text="Filter Records", padding=10)
        filter_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(filter_frame, text="Model:").pack(side="left", padx=5)
        self.filter_model_combo = ttk.Combobox(filter_frame, state="readonly", width=20)
        self.filter_model_combo.pack(side="left", padx=5)
        ttk.Label(filter_frame, text="From:").pack(side="left", padx=(15, 5))
        self.filter_from_date = DateEntry(filter_frame, date_pattern='yyyy-mm-dd', width=12)
        self.filter_from_date.pack(side="left", padx=5)
        ttk.Label(filter_frame, text="To:").pack(side="left", padx=(15, 5))
        self.filter_to_date = DateEntry(filter_frame, date_pattern='yyyy-mm-dd', width=12)
        self.filter_to_date.pack(side="left", padx=5)
        ttk.Button(filter_frame, text="Apply Filter", command=lambda: self.update_records_view(limit=False)).pack(
            side="left", padx=15)
        ttk.Button(filter_frame, text="Clear Filters", command=self.clear_filters).pack(side="left", padx=5)

        self.dup_stats_frame = tk.Frame(self.records_tab, bg="#800000", bd=2, relief="solid")
        self.dup_stats_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(self.dup_stats_frame, text="🚨 DETECTED DUPLICATE & SKIPPED PARTS:", font=("Arial", 12, "bold"),
                  background="#800000", foreground="yellow").pack(side="left", padx=10)
        self.rec_lbl_dup_today = ttk.Label(self.dup_stats_frame, text="TODAY: 0", font=("Arial", 12, "bold"),
                                           background="#800000", foreground="white")
        self.rec_lbl_dup_today.pack(side="left", padx=15)
        self.rec_lbl_dup_month = ttk.Label(self.dup_stats_frame, text="MONTH: 0", font=("Arial", 12, "bold"),
                                           background="#800000", foreground="white")
        self.rec_lbl_dup_month.pack(side="left", padx=15)
        self.rec_lbl_dup_year = ttk.Label(self.dup_stats_frame, text="YEAR: 0", font=("Arial", 12, "bold"),
                                          background="#800000", foreground="white")
        self.rec_lbl_dup_year.pack(side="left", padx=15)

        controls_frame = ttk.Frame(self.records_tab, style="TFrame")
        controls_frame.pack(fill="x", padx=10, pady=5)
        self.ok_parts_label = ttk.Label(controls_frame, text="Showing Latest 50 Records (OK: 0)",
                                        font=("Arial", 12, "bold"), foreground="lightgreen")
        self.ok_parts_label.pack(side="left")
        ttk.Button(controls_frame, text="Export Excel", command=self._create_export_window).pack(side="right")

        columns = ("SR", "ModelName", "QRNo", "FullQRData", "Time", "Status", "Shift", "User")
        self.records_tree = ttk.Treeview(self.records_tab, columns=columns, show="headings")
        self.records_tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.records_tree.tag_configure('error_tag', foreground='red')
        for col, heading, width in [("SR", "SR No.", 50), ("ModelName", "Model Name", 150),
                                    ("QRNo", "QR Code No.", 100), ("FullQRData", "Full QR Data", 250),
                                    ("Time", "Time", 150), ("Status", "Status", 150), ("Shift", "Shift", 80),
                                    ("User", "User Name", 100)]:
            self.records_tree.heading(col, text=heading)
            self.records_tree.column(col, width=width, anchor="w")
        self.clear_filters()

    def update_records_tab_stats(self):
        cursor = self.db_conn.cursor()
        today_str = date.today().strftime("%Y-%m-%d");
        month_str = date.today().strftime("%Y-%m");
        year_str = date.today().strftime("%Y")
        cursor.execute(
            "SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND (status NOT LIKE 'Validated' AND status != 'Validated (NG Part)')",
            (f"{today_str}%",))
        self.rec_lbl_dup_today.config(text=f"TODAY: {cursor.fetchone()[0]}")
        cursor.execute(
            "SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND (status NOT LIKE 'Validated' AND status != 'Validated (NG Part)')",
            (f"{month_str}%",))
        self.rec_lbl_dup_month.config(text=f"MONTH: {cursor.fetchone()[0]}")
        cursor.execute(
            "SELECT COUNT(*) FROM qr_records WHERE time LIKE ? AND (status NOT LIKE 'Validated' AND status != 'Validated (NG Part)')",
            (f"{year_str}%",))
        self.rec_lbl_dup_year.config(text=f"YEAR: {cursor.fetchone()[0]}")

    def clear_filters(self):
        self.filter_model_combo.set("All Models")
        today = date.today()
        self.filter_to_date.set_date(today)
        self.filter_from_date.set_date(today.replace(day=1))
        self.update_records_view()

    def update_records_view(self, limit=True):
        for row in self.records_tree.get_children(): self.records_tree.delete(row)
        query = "SELECT id, model_name, qr_code_no, full_qr_data, time, status, shift, user_name FROM qr_records WHERE 1=1"
        params = []
        selected_model = self.filter_model_combo.get()
        if selected_model and selected_model != "All Models":
            query += " AND model_name = ?"
            params.append(selected_model)
        start_date = self.filter_from_date.get_date().strftime("%Y-%m-%d")
        end_date = self.filter_to_date.get_date().strftime("%Y-%m-%d 23:59:59")
        query += " AND time >= ? AND time <= ?"
        params.extend([start_date, end_date])
        query += " ORDER BY id DESC"
        if limit: query += " LIMIT 50"

        cursor = self.db_conn.cursor()
        cursor.execute(query, params)
        records = cursor.fetchall()
        ok_count = 0
        for record in records:
            if record[5].startswith("Validated"):
                tags = ('error_tag',) if "(NG Part)" in record[5] else ()
                ok_count += 1
            else:
                tags = ('error_tag',)
            self.records_tree.insert("", "end", values=record, tags=tags)
        if limit:
            self.ok_parts_label.config(text=f"Showing Latest 50 Records (OK: {ok_count})")
        else:
            self.ok_parts_label.config(text=f"Filtered OK Parts: {ok_count}")
        self.update_records_tab_stats()

    def load_model_data(self, event=None):
        selected_model = self.config_model_combo.get()
        cursor = self.db_conn.cursor()
        # 🚀 FIX: Fetch all 11 columns matching exact python list structure
        cursor.execute(
            "SELECT id, model_name, project_name, part_name, rssl_part_no, customer_part_no, revision_no, vendor_code, length_of_character, camera_timeout, master_image_path FROM parts_data WHERE model_name = ?",
            (selected_model,))
        model_data = cursor.fetchone()
        if model_data:
            columns = ["id", "model_name", "project_name", "part_name", "rssl_part_no", "customer_part_no",
                       "revision_no", "vendor_code", "length_of_character", "camera_timeout", "master_image_path"]
            data_dict = dict(zip(columns, model_data))
            for key, entry in self.config_entries.items():
                entry.delete(0, tk.END)
                val = data_dict.get(key.lower(), "")
                if key == "CAMERA_TIMEOUT" and val == "": val = 10
                entry.insert(0, val)

    def clear_config_fields(self):
        for entry in self.config_entries.values(): entry.delete(0, tk.END)
        self.config_model_combo.set('')

    def save_new_model(self):
        data = {key.lower(): entry.get() for key, entry in self.config_entries.items()}
        if not data["model_name"]:
            messagebox.showerror("Error", "Model Name cannot be empty.")
            return
        cursor = self.db_conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO parts_data (model_name, project_name, part_name, rssl_part_no, customer_part_no, revision_no, vendor_code, length_of_character, camera_timeout, master_image_path) VALUES (:model_name, :project_name, :part_name, :rssl_part_no, :customer_part_no, :revision_no, :vendor_code, :length_of_character, :camera_timeout, :master_image_path)",
                data)
            self.db_conn.commit()
            messagebox.showinfo("Success", f"Model '{data['model_name']}' saved successfully!")
            self.populate_model_combo()
            self.clear_config_fields()
        except sqlite3.IntegrityError:
            messagebox.showerror("Error", f"Model '{data['model_name']}' already exists.")

    def update_model(self):
        selected_model = self.config_model_combo.get()
        if not selected_model: return
        data = {key.lower(): entry.get() for key, entry in self.config_entries.items()}
        cursor = self.db_conn.cursor()
        cursor.execute(
            "UPDATE parts_data SET model_name=?, project_name=?, part_name=?, rssl_part_no=?, customer_part_no=?, revision_no=?, vendor_code=?, length_of_character=?, camera_timeout=?, master_image_path=? WHERE model_name=?",
            (data["model_name"], data["project_name"], data["part_name"], data["rssl_part_no"],
             data["customer_part_no"], data["revision_no"], data["vendor_code"], data["length_of_character"],
             data["camera_timeout"], data["master_image_path"], selected_model))
        self.db_conn.commit()
        messagebox.showinfo("Success", f"Model '{selected_model}' updated successfully!")
        self.populate_model_combo()
        self.clear_config_fields()

        if self.running_model_name == selected_model:
            self.model_combo.set(data["model_name"])
            self.select_model()

    def delete_model(self):
        selected_model = self.config_model_combo.get()
        if not selected_model: return
        if messagebox.askyesno("Delete Model", f"Are you sure you want to delete '{selected_model}'?"):
            cursor = self.db_conn.cursor()
            cursor.execute("DELETE FROM parts_data WHERE model_name=?", (selected_model,))
            self.db_conn.commit()
            messagebox.showinfo("Success", f"Model '{selected_model}' deleted successfully!")
            self.populate_model_combo()
            self.clear_config_fields()

    def select_model(self):
        selected_model = self.model_combo.get()
        if not selected_model: return
        cursor = self.db_conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO app_status (key, value) VALUES ('last_running_model', ?)",
                       (selected_model,))
        self.db_conn.commit()

        # 🚀 FIX: Python sequence mapped accurately with 11 items.
        cursor.execute(
            "SELECT id, model_name, project_name, part_name, rssl_part_no, customer_part_no, revision_no, vendor_code, length_of_character, camera_timeout, master_image_path FROM parts_data WHERE model_name = ?",
            (selected_model,))
        columns = ["id", "MODEL_NAME", "PROJECT_NAME", "PART_NAME", "RSSL_PART_NO", "CUSTOMER_PART_NO", "REVISION_NO",
                   "VENDOR_CODE", "LENGTH_OF_CHARACTER", "CAMERA_TIMEOUT", "MASTER_IMAGE_PATH"]
        model_data = dict(zip(columns, cursor.fetchone()))

        self.running_model_name = selected_model
        self.running_model_data = model_data
        self.running_model_label.config(text=self.running_model_name)
        self.display_part_info(self.running_model_data)

        self.camera_verify_button.config(state="normal")
        self.manual_validation_button.config(state="disabled")

        if self.is_admin:
            self.set_serial_button.config(state="normal")

    def display_part_info(self, model_data):
        for widget in self.info_text_frame.winfo_children(): widget.destroy()
        for key, value in model_data.items():
            if key not in ["id", "MASTER_IMAGE_PATH", "CAMERA_TIMEOUT"]:
                ttk.Label(self.info_text_frame, text=f"{key.replace('_', ' ').title()}: {value}",
                          font=("Arial", 11)).pack(anchor="w", pady=1)

        current_serial = self.get_current_serial_for_model(model_data["MODEL_NAME"])
        qr_code_no = f"{current_serial:06d}"
        date_str = datetime.now().strftime("%m%y")
        full_qr_data = f"{model_data['CUSTOMER_PART_NO']}{model_data['REVISION_NO']}{model_data['VENDOR_CODE']}{date_str}{qr_code_no}"
        expected_length = len(full_qr_data)
        ttk.Label(self.info_text_frame, text="-" * 40).pack(anchor="w", pady=2)
        ttk.Label(self.info_text_frame, text=f"Dynamic Length: {expected_length}", font=("Arial", 11, "bold"),
                  foreground="yellow").pack(anchor="w")
        ttk.Label(self.info_text_frame, text=f"Serial: {qr_code_no}", font=("Arial", 11, "bold"),
                  foreground="yellow").pack(anchor="w")
        ttk.Label(self.info_text_frame, text=f"Data: {full_qr_data}", font=("Arial", 10, "bold"), wraplength=400).pack(
            anchor="w")
        self.generate_qr_code(model_data)

    def generate_qr_code(self, model_data):
        current_serial = self.get_current_serial_for_model(model_data["MODEL_NAME"])
        qr_code_no = f"{current_serial:06d}"
        date_str = datetime.now().strftime("%m%y")
        qr_data = f"{model_data['CUSTOMER_PART_NO']}{model_data['REVISION_NO']}{model_data['VENDOR_CODE']}{date_str}{qr_code_no}"
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=4, border=2)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img_tk = ImageTk.PhotoImage(qr.make_image(fill_color="black", back_color="white").convert('RGB'))
        self.qr_label.config(image=img_tk)
        self.qr_label.image = img_tk

    def print_zpl_data(self, zpl_data):
        if not self.selected_printer:
            messagebox.showerror("Printing Error", "No printer is selected.")
            return False
        printer_handle = None
        try:
            printer_handle = win32print.OpenPrinter(self.selected_printer)
            win32print.StartDocPrinter(printer_handle, 1, ("Label", None, "RAW"))
            win32print.StartPagePrinter(printer_handle)
            win32print.WritePrinter(printer_handle, zpl_data.encode('utf-8'))
            win32print.EndPagePrinter(printer_handle)
            win32print.EndDocPrinter(printer_handle)
            return True
        except Exception as e:
            messagebox.showerror("Printing Error", f"Failed to print to {self.selected_printer}.\nError: {e}")
            return False
        finally:
            if printer_handle:
                try:
                    win32print.ClosePrinter(printer_handle)
                except:
                    pass

    # ==========================================
    # 🤖 AI COMPUTER VISION (STRICT MODE + LIVE FEED)
    # ==========================================
    def _create_camera_inspection_window(self, is_auto_flow=True):
        if self.active_camera_window and self.active_camera_window.winfo_exists(): return

        master_image_path = self.running_model_data.get("MASTER_IMAGE_PATH", "")
        if not master_image_path or not os.path.exists(master_image_path):
            messagebox.showerror("AI Vision Error",
                                 "No Master Image configured for this model!\nPlease capture one in Model Configuration.")
            return

        try:
            timeout_val = int(self.running_model_data.get("CAMERA_TIMEOUT", 10))
        except:
            timeout_val = 10

        self.is_validating = True
        self.active_camera_window = tk.Toplevel(self)
        cam_win = self.active_camera_window
        cam_win.title("🤖 AI Part Verification (STRICT MODE)")

        cam_win.geometry("950x620")
        cam_win.configure(bg="#2e8b57")
        cam_win.grab_set()

        cam_win.time_left = timeout_val
        cam_win.timer_id = None
        cam_win.frame_count = 0

        def on_close():
            if cam_win.timer_id: cam_win.after_cancel(cam_win.timer_id)
            if hasattr(cam_win, 'cap') and cam_win.cap.isOpened(): cam_win.cap.release()
            self.is_validating = False
            cam_win.destroy()

        cam_win.protocol("WM_DELETE_WINDOW", on_close)

        top_frame = tk.Frame(cam_win, bg="#2e8b57")
        top_frame.pack(fill="x", pady=5)
        ttk.Label(top_frame, text="AI VISION CONTINUOUS INSPECTION", font=("Arial", 16, "bold"), foreground="yellow",
                  background="#2e8b57").pack()
        cam_win.lbl_timer = ttk.Label(top_frame, text=f"⏱️ Time Left: {cam_win.time_left}s",
                                      font=("Courier", 14, "bold"), background="#2e8b57", foreground="white")
        cam_win.lbl_timer.pack()

        split_frame = tk.Frame(cam_win, bg="#2e8b57")
        split_frame.pack(fill="both", expand=True, padx=10)

        left_panel = tk.Frame(split_frame, bg="black", width=420, height=320)
        left_panel.pack(side="left", padx=5, pady=5)
        ttk.Label(left_panel, text="LIVE CAMERA FEED", font=("Arial", 11, "bold"), background="black",
                  foreground="lightgreen").pack(pady=2)
        cam_win.lbl_video = tk.Label(left_panel, bg="black")
        cam_win.lbl_video.pack(expand=True)

        right_panel = tk.Frame(split_frame, bg="#1c5736", bd=2, relief="sunken")
        right_panel.pack(side="right", fill="both", expand=True, padx=5, pady=5)
        ttk.Label(right_panel, text="MASTER COMPARISON", font=("Arial", 11, "bold"), background="#1c5736",
                  foreground="gold").pack(pady=2)
        cam_win.lbl_master_img = tk.Label(right_panel, bg="#1c5736")
        cam_win.lbl_master_img.pack(pady=5)

        stats_frame = tk.Frame(right_panel, bg="#1c5736")
        stats_frame.pack(fill="x", pady=5, padx=5)
        cam_win.lbl_live_status = ttk.Label(stats_frame, text="Checking...", font=("Courier", 12, "bold"),
                                            background="#1c5736", foreground="white")
        cam_win.lbl_live_status.pack()

        btn_frame = tk.Frame(cam_win, bg="#2e8b57")
        btn_frame.pack(side="bottom", fill="x", pady=10)
        inner_btn = tk.Frame(btn_frame, bg="#2e8b57")
        inner_btn.pack(expand=True)

        try:
            master_img_color = cv2.imread(master_image_path)
            master_img_color = cv2.resize(master_img_color, (320, 240))
            master_img_gray = cv2.cvtColor(master_img_color, cv2.COLOR_BGR2GRAY)

            img_m = Image.fromarray(cv2.cvtColor(master_img_color, cv2.COLOR_BGR2RGB))
            imgtk_m = ImageTk.PhotoImage(image=img_m)
            cam_win.lbl_master_img.imgtk = imgtk_m
            cam_win.lbl_master_img.configure(image=imgtk_m)

            cam_win.sift = cv2.SIFT_create(nfeatures=500)
            cam_win.kp_master, cam_win.des_master = cam_win.sift.detectAndCompute(master_img_gray, None)

            index_params = dict(algorithm=1, trees=5)
            search_params = dict(checks=50)
            cam_win.flann = cv2.FlannBasedMatcher(index_params, search_params)

            hsv_m = cv2.cvtColor(master_img_color, cv2.COLOR_BGR2HSV)
            cam_win.hist_master = cv2.calcHist([hsv_m], [0, 1], None, [50, 60], [0, 180, 0, 256])
            cv2.normalize(cam_win.hist_master, cam_win.hist_master, 0, 1, cv2.NORM_MINMAX)

        except Exception as e:
            messagebox.showerror("Image Error", f"Failed to load master image: {e}", parent=cam_win)
            on_close();
            return

        cam_win.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        cam_win.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cam_win.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        # 🚀 STRICT BACKGROUND ANALYSIS (Max 5 Degree Rot Tolerance)
        def run_strict_analysis():
            if not cam_win.winfo_exists() or not hasattr(cam_win, 'current_frame'): return
            if cam_win.time_left <= 0: return

            frame = cam_win.current_frame.copy()
            frame_resized = cv2.resize(frame, (320, 240))

            hsv_f = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2HSV)
            hist_f = cv2.calcHist([hsv_f], [0, 1], None, [50, 60], [0, 180, 0, 256])
            cv2.normalize(hist_f, hist_f, 0, 1, cv2.NORM_MINMAX)
            color_match = cv2.compareHist(cam_win.hist_master, hist_f, cv2.HISTCMP_CORREL)

            if color_match < 0.50:
                cam_win.lbl_live_status.config(text=f"NG: Color/Light ({int(color_match * 100)}%)",
                                               foreground="#ff6666")
                cam_win.after(500, run_strict_analysis)
                return

            frame_gray = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2GRAY)
            kp_cam, des_cam = cam_win.sift.detectAndCompute(frame_gray, None)
            if des_cam is None or len(des_cam) < 15:
                cam_win.lbl_live_status.config(text="NG: No clear features", foreground="#ff6666")
                cam_win.after(500, run_strict_analysis)
                return

            matches = cam_win.flann.knnMatch(cam_win.des_master, des_cam, k=2)

            good_matches = []
            for m_pair in matches:
                if len(m_pair) == 2:
                    m, n = m_pair
                    if m.distance < 0.75 * n.distance:
                        good_matches.append(m)

            if len(good_matches) < 12:
                cam_win.lbl_live_status.config(text=f"NG: Feature Mismatch ({len(good_matches)}/12)",
                                               foreground="#ff6666")
                cam_win.after(500, run_strict_analysis)
                return

            src_pts = np.float32([cam_win.kp_master[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp_cam[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

            M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
            if M is None:
                cam_win.lbl_live_status.config(text="NG: Geometry Mismatch", foreground="#ff6666")
                cam_win.after(500, run_strict_analysis)
                return

            inliers = np.sum(mask)
            if inliers < 10:
                cam_win.lbl_live_status.config(text="NG: Shape Mismatch", foreground="#ff6666")
                cam_win.after(500, run_strict_analysis)
                return

            # 🚀 5 DEGREE LIMIT FOR PASS
            angle = math.atan2(M[1, 0], M[0, 0]) * (180 / math.pi)
            if abs(angle) > 5:
                cam_win.lbl_live_status.config(text=f"❌ NG: PART ROTATED ({int(angle)}°)", foreground="#ff6666")
                cam_win.after(500, run_strict_analysis)
                return

            scale_x = math.sqrt(M[0, 0] ** 2 + M[1, 0] ** 2)
            if scale_x < 0.75 or scale_x > 1.35:
                cam_win.lbl_live_status.config(text=f"NG: Bad Distance (Scale: {scale_x:.2f})", foreground="#ff6666")
                cam_win.after(500, run_strict_analysis)
                return

            # PASS
            cam_win.lbl_live_status.config(text=f"✅ OK: PASS (Angle: {int(angle)}°)", foreground="lightgreen")
            cam_win.after(300, verify_success)

        def update_video():
            if not cam_win.winfo_exists(): return
            ret, frame = cam_win.cap.read()
            if ret:
                cam_win.current_frame = frame
                frame_resized_ui = cv2.resize(frame, (420, 315))
                frame_rgb = cv2.cvtColor(frame_resized_ui, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame_rgb)
                imgtk = ImageTk.PhotoImage(image=img)
                cam_win.lbl_video.imgtk = imgtk
                cam_win.lbl_video.configure(image=imgtk)
            cam_win.after(15, update_video)

        update_video()
        cam_win.after(500, run_strict_analysis)

        # 🚀 CAMERA OK FLOW
        def verify_success():
            if cam_win.timer_id: cam_win.after_cancel(cam_win.timer_id)
            if cam_win.cap.isOpened(): cam_win.cap.release()
            cam_win.destroy()

            # Immediately prints and pops QR scan window
            self._execute_print_and_validate(is_ng=False)

        # 🚀 CAMERA NG FLOW -> Ask -> Print NG -> Val NG -> Lock
        def handle_camera_ng(reason_text):
            if cam_win.timer_id: cam_win.after_cancel(cam_win.timer_id)
            if hasattr(cam_win, 'cap') and cam_win.cap.isOpened(): cam_win.cap.release()
            cam_win.destroy()
            self.is_validating = False

            print_ng = messagebox.askyesno("Camera Validation Failed",
                                           f"{reason_text}\n\nDo you want to print an NG QR Code?")
            if print_ng:
                self._execute_print_and_validate(is_ng=True, ng_reason=reason_text)
            else:
                def unlock_cb(skip_val=None):
                    pass

                self.trigger_generator_alarm(self, "CAMERA FAILED/SKIPPED", reason_text, unlock_cb)

        def mark_ng():
            handle_camera_ng("Camera Skipped or Marked NG by Operator")

        def tick_timer():
            if not cam_win.winfo_exists(): return
            if cam_win.time_left > 0:
                cam_win.time_left -= 1
                cam_win.lbl_timer.config(text=f"⏱️ Time Left: {cam_win.time_left}s")
                cam_win.timer_id = cam_win.after(1000, tick_timer)
            else:
                cam_win.lbl_live_status.config(text="❌ NG: TIMEOUT REACHED", foreground="#ff6666")
                btn_try_again.pack(side="left", padx=10, ipadx=10, ipady=5)
                btn_mark_ng.pack(side="left", padx=10, ipadx=10, ipady=5)

        def try_again():
            btn_try_again.pack_forget();
            btn_mark_ng.pack_forget()
            cam_win.time_left = timeout_val
            cam_win.lbl_timer.config(text=f"⏱️ Time Left: {cam_win.time_left}s")
            cam_win.lbl_live_status.config(text="Checking...", foreground="white")
            cam_win.timer_id = cam_win.after(1000, tick_timer)
            cam_win.after(500, run_strict_analysis)

        btn_try_again = ttk.Button(inner_btn, text="🔄 Try Again", command=try_again)
        btn_mark_ng = ttk.Button(inner_btn, text="⏩ Skip / Mark NG", command=mark_ng)

        cam_win.timer_id = cam_win.after(1000, tick_timer)

    # ==========================================
    # 🖨️ PRINTER & BARCODE VALIDATION FLOW
    # ==========================================
    def _execute_print_and_validate(self, is_ng=False, ng_reason=""):
        current_serial = self.get_current_serial_for_model(self.running_model_name)
        qr_code_no = f"{current_serial:06d}"
        date_str = datetime.now().strftime("%m%y")
        full_qr_data = f"{self.running_model_data['CUSTOMER_PART_NO']}{self.running_model_data['REVISION_NO']}{self.running_model_data['VENDOR_CODE']}{date_str}{qr_code_no}"

        cursor = self.db_conn.cursor()
        current_month_db = datetime.now().strftime("%Y-%m")
        cursor.execute("SELECT id FROM qr_records WHERE model_name=? AND qr_code_no=? AND time LIKE ?",
                       (self.running_model_name, qr_code_no, f"{current_month_db}%"))
        if cursor.fetchone():
            cursor.execute("SELECT MAX(CAST(qr_code_no AS INTEGER)) FROM qr_records WHERE model_name=? AND time LIKE ?",
                           (self.running_model_name, f"{current_month_db}%"))
            safe_max = cursor.fetchone()[0]
            new_safe = safe_max + 1 if safe_max else 1
            self.save_serial_for_model(self.running_model_name, new_safe)
            messagebox.showerror("Collision Prevented",
                                 f"Serial {qr_code_no} was already in the database!\nSystem auto-corrected to {new_safe}.\n\nPlease press Generate again.")
            self.is_validating = False
            self.display_part_info(self.running_model_data)
            return

        zpl_code = generate_zpl(self.running_model_data, qr_code_no, is_ng=is_ng)

        # NOTE: Print code bhejte hi validation pop ho jayegi to prevent lockup
        self.print_zpl_data(zpl_code)
        self._create_manual_full_validation_window(self.running_model_name, qr_code_no, full_qr_data, is_ng=is_ng,
                                                   ng_reason=ng_reason)

    def manual_validation_prompt(self):
        if self.is_validating:
            messagebox.showwarning("Warning", "Finish validating the current part first!")
            return
        if not self.running_model_data: return
        self.is_validating = True
        current_serial = self.get_current_serial_for_model(self.running_model_name)
        qr_code_no = f"{current_serial:06d}"
        date_str = datetime.now().strftime("%m%y")
        full_qr_data = f"{self.running_model_data['CUSTOMER_PART_NO']}{self.running_model_data['REVISION_NO']}{self.running_model_data['VENDOR_CODE']}{date_str}{qr_code_no}"
        self._create_manual_full_validation_window(self.running_model_name, qr_code_no, full_qr_data, is_ng=False)

    # ✅ BARCODE VALIDATION WINDOW
    def _create_manual_full_validation_window(self, model_name, qr_code_no, full_qr_data_to_validate, is_ng=False,
                                              ng_reason=""):
        cursor = self.db_conn.cursor()
        pending_str = f"{model_name}|||{qr_code_no}|||{full_qr_data_to_validate}|||{is_ng}"
        cursor.execute("INSERT OR REPLACE INTO app_status (key, value) VALUES ('pending_validation', ?)",
                       (pending_str,))
        self.db_conn.commit()
        self.is_validating = True

        validate_window = tk.Toplevel(self)
        win_title = "NG Barcode Validation" if is_ng else "Barcode Validation"
        validate_window.title(win_title)
        validate_window.geometry("600x350")

        bg_color = "#800000" if is_ng else "#2e8b57"
        validate_window.configure(bg=bg_color)
        validate_window.grab_set()

        def disable_x_button():
            messagebox.showwarning("Action Required", "RESTRICTED ACTION!\nYou cannot close this window.",
                                   parent=validate_window)

        validate_window.protocol("WM_DELETE_WINDOW", disable_x_button)

        style = ttk.Style(validate_window)
        style.configure("NG.TFrame", background=bg_color)

        frame = ttk.Frame(validate_window, style="NG.TFrame", padding=10)
        frame.pack(fill="both", expand=True)

        expected_length = len(full_qr_data_to_validate)
        ttk.Label(frame, text=f"Expected QR Code Length: {expected_length}", font=("Arial", 12, "bold"),
                  background=bg_color, foreground="white").pack(pady=10)
        ttk.Label(frame, text="Scan Barcode to Complete Cycle:", font=("Arial", 14, "bold"), background=bg_color,
                  foreground="white").pack(pady=10)

        full_qr_entry = ttk.Entry(frame, width=60, font=("Arial", 14))
        full_qr_entry.pack(pady=10, ipady=5)
        full_qr_entry.focus_set()

        def complete_cycle_and_close():
            cursor = self.db_conn.cursor()
            cursor.execute("UPDATE app_status SET value = '' WHERE key = 'pending_validation'")
            self.db_conn.commit()

            current_val = self.get_current_serial_for_model(model_name)
            self.save_serial_for_model(model_name, current_val + 1)

            if not is_ng and self.plc.connected:
                self.plc.write_device("M5002", 1)

            self.display_part_info(self.running_model_data)
            self.update_records_view()
            self.update_dashboard_stats()
            self.manual_validation_button.config(state="disabled")
            self.is_validating = False
            validate_window.destroy()

            if is_ng:
                def unlock_cb(skip_val=None): pass

                self.trigger_generator_alarm(self, "NG PART SCANNED", ng_reason, unlock_cb)

        def log_error_and_continue(status_text):
            cursor = self.db_conn.cursor()
            record_data = (model_name, qr_code_no, full_qr_entry.get().strip(),
                           datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status_text, self.shift_combo.get(),
                           self.employee_name)
            cursor.execute(
                "INSERT INTO qr_records (model_name, qr_code_no, full_qr_data, time, status, shift, user_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                record_data)
            self.db_conn.commit()
            self.update_records_view()
            self.update_dashboard_stats()
            full_qr_entry.delete(0, tk.END)
            full_qr_entry.focus_set()

        def skip_validation_lockdown():
            def execute_skip(skip_status_string):
                cursor = self.db_conn.cursor()
                final_status = f"Skipped (NG Part)/{skip_status_string}" if is_ng else f"Skipped/{skip_status_string}"
                record_data = (model_name, qr_code_no, full_qr_data_to_validate,
                               datetime.now().strftime("%Y-%m-%d %H:%M:%S"), final_status, self.shift_combo.get(),
                               self.employee_name)
                cursor.execute(
                    "INSERT INTO qr_records (model_name, qr_code_no, full_qr_data, time, status, shift, user_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    record_data)
                self.db_conn.commit()
                complete_cycle_and_close()

            self.trigger_generator_alarm(validate_window, full_qr_data_to_validate, "SKIP VALIDATION REQUESTED",
                                         execute_skip, is_skip=True)

        def on_entry_scan(event):
            scanned_data = full_qr_entry.get().strip()
            if not scanned_data: return
            cursor = self.db_conn.cursor()

            if scanned_data == full_qr_data_to_validate:
                final_status = "Validated (NG Part)" if is_ng else "Validated"
                record_data = (model_name, qr_code_no, scanned_data, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               final_status, self.shift_combo.get(), self.employee_name)
                cursor.execute(
                    "INSERT INTO qr_records (model_name, qr_code_no, full_qr_data, time, status, shift, user_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    record_data)
                self.db_conn.commit()
                complete_cycle_and_close()
                return

            expected_prefix = f"{self.running_model_data['CUSTOMER_PART_NO']}{self.running_model_data['REVISION_NO']}{self.running_model_data['VENDOR_CODE']}"
            if not scanned_data.startswith(expected_prefix):
                log_error_and_continue("Model Mismatch (NG Part)" if is_ng else "Model Mismatch")

                def unlock_mismatch(): pass

                details = f"Expected Model: {self.running_model_name}\nExpected Prefix: {expected_prefix}"
                self.trigger_generator_alarm(validate_window, scanned_data, "MODEL MISMATCH ERROR!", unlock_mismatch,
                                             details=details)
                return

            cursor.execute(
                "SELECT time, user_name, model_name FROM qr_records WHERE full_qr_data = ? AND status LIKE 'Validated%'",
                (scanned_data,))
            existing_record = cursor.fetchone()
            if existing_record:
                orig_time, orig_user, orig_model = existing_record
                details = f"ORIGINAL SCAN RECORD:\nDate/Time: {orig_time}\nOperator: {orig_user}\nModel: {orig_model}"
                log_error_and_continue("Duplicate (NG Part)" if is_ng else "Duplicate")

                def unlock_duplicate(): pass

                self.trigger_generator_alarm(validate_window, scanned_data, "DUPLICATE PART SCANNED!", unlock_duplicate,
                                             details=details)
                return

            if len(scanned_data) != expected_length:
                log_error_and_continue("Invalid Length (NG Part)" if is_ng else "Invalid Length")

                def unlock_length_error(): pass

                self.trigger_generator_alarm(validate_window, scanned_data, f"INVALID LENGTH ({len(scanned_data)})",
                                             unlock_length_error)
                return

            messagebox.showerror("Error", "Validation Failed: QR code does not match the newly printed label.",
                                 parent=validate_window)
            full_qr_entry.delete(0, tk.END)

        full_qr_entry.bind("<Return>", on_entry_scan)
        ttk.Button(frame, text="Skip Validation (ADMIN ONLY)", command=skip_validation_lockdown).pack(pady=20, ipadx=10,
                                                                                                      ipady=5)

    def _create_set_serial_window(self):
        if not self.running_model_name: return
        set_serial_window = tk.Toplevel(self)
        set_serial_window.title(f"Set Serial for {self.running_model_name}")
        set_serial_window.geometry("400x200")
        set_serial_window.configure(bg="#2e8b57")
        set_serial_window.grab_set()

        frame = ttk.Frame(set_serial_window, style="TFrame", padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Enter New Serial Number:").pack(pady=5)
        serial_entry = ttk.Entry(frame, width=30)
        serial_entry.pack(pady=5)
        ttk.Label(frame, text="Enter Password:").pack(pady=5)
        password_entry = ttk.Entry(frame, width=30, show="*")
        password_entry.pack(pady=5)

        def set_serial_with_password():
            if password_entry.get() == "1313":
                try:
                    new_serial = int(serial_entry.get())
                    new_qr_str = f"{new_serial:06d}"
                    current_month = datetime.now().strftime("%Y-%m")
                    set_serial_window.config(cursor="watch")
                    set_serial_window.update()
                    time.sleep(0.5)

                    cursor = self.db_conn.cursor()
                    cursor.execute("SELECT id FROM qr_records WHERE model_name=? AND qr_code_no=? AND time LIKE ?",
                                   (self.running_model_name, new_qr_str, f"{current_month}%"))
                    if cursor.fetchone():
                        set_serial_window.config(cursor="")
                        cursor.execute(
                            "SELECT MAX(CAST(qr_code_no AS INTEGER)) FROM qr_records WHERE model_name=? AND time LIKE ?",
                            (self.running_model_name, f"{current_month}%"))
                        last_ser = cursor.fetchone()
                        last_ser_val = last_ser[0] if last_ser and last_ser[0] else 0

                        messagebox.showerror("Overwrite Blocked",
                                             f"❌ CRITICAL ERROR: Serial Number '{new_serial}' has already been printed for {self.running_model_name} this month!\n\nThe highest safe printed serial number is: {last_ser_val}\n Please choose a number higher than {last_ser_val}.",
                                             parent=set_serial_window)
                        return

                    set_serial_window.config(cursor="")
                    self.save_serial_for_model(self.running_model_name, new_serial)
                    self.display_part_info(self.running_model_data)
                    messagebox.showinfo("Success", f"Serial safely set to {new_serial}.")
                    set_serial_window.destroy()
                except ValueError:
                    set_serial_window.config(cursor="")
                    messagebox.showerror("Error", "Please enter a valid integer.")
            else:
                messagebox.showerror("Error", "Incorrect password.")

        ttk.Button(frame, text="Submit & Verify", command=set_serial_with_password).pack(pady=10)

    def _create_export_window(self):
        export_window = tk.Toplevel(self)
        export_window.title("Export QR Records")
        export_window.geometry("400x300")
        export_window.configure(bg="#2e8b57")
        export_window.grab_set()

        frame = ttk.Frame(export_window, style="TFrame", padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Select Export Range:", font=("Arial", 12, "bold")).pack(pady=10)
        date_frame = ttk.Frame(frame, style="TFrame")
        date_frame.pack(pady=5)
        from_date = DateEntry(date_frame, date_pattern='yyyy-mm-dd')
        from_date.pack(side="left", padx=5)
        ttk.Label(date_frame, text="to").pack(side="left")
        to_date = DateEntry(date_frame, date_pattern='yyyy-mm-dd')
        to_date.pack(side="left", padx=5)
        model_frame = ttk.Frame(frame, style="TFrame")
        model_frame.pack(pady=10)
        export_model_combo = ttk.Combobox(model_frame, state="readonly", width=30)
        export_model_combo['values'] = ["All Models"] + list(self.model_combo['values'])
        export_model_combo.set("All Models")
        export_model_combo.pack(side="left", padx=5)

        def perform_export():
            file_path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")])
            if not file_path: return
            query = "SELECT * FROM qr_records WHERE time >= ? AND time <= ?"
            params = (from_date.get_date().strftime("%Y-%m-%d"), to_date.get_date().strftime("%Y-%m-%d 23:59:59"))
            if export_model_combo.get() != "All Models":
                query += " AND model_name = ?"
                params += (export_model_combo.get(),)
            df = pd.read_sql_query(query, self.db_conn, params=params)
            if not df.empty:
                df.to_excel(file_path, index=False)
                messagebox.showinfo("Success", f"Data exported to:\n{file_path}")
            else:
                messagebox.showinfo("No Data", "No records found.")
            export_window.destroy()

        ttk.Button(frame, text="Export", command=perform_export).pack(pady=10)


# ==========================================
# --- Database and Login Handling ---
# ==========================================
def create_database():
    db_path = r"E:\qr_app.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, employee_name TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS serial_counters (model_name TEXT PRIMARY KEY, serial_number INTEGER)')
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS parts_data (id INTEGER PRIMARY KEY, model_name TEXT UNIQUE, project_name TEXT, part_name TEXT, rssl_part_no TEXT, customer_part_no TEXT, revision_no TEXT, vendor_code TEXT, length_of_character INTEGER)''')

    try:
        cursor.execute("ALTER TABLE parts_data ADD COLUMN master_image_path TEXT DEFAULT ''")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE parts_data ADD COLUMN camera_timeout INTEGER DEFAULT 10")
    except:
        pass

    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS qr_records (id INTEGER PRIMARY KEY, model_name TEXT, qr_code_no TEXT, full_qr_data TEXT, time TEXT, status TEXT, shift TEXT, user_name TEXT)''')
    cursor.execute('CREATE TABLE IF NOT EXISTS app_status (key TEXT PRIMARY KEY, value TEXT)')

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_qr_time ON qr_records(time)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_qr_model ON qr_records(model_name)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_qr_full_data ON qr_records(full_qr_data)')

    users = [('admin', '1313', 'System Admin'), ('user1', 'pass1', 'John Doe')]
    cursor.executemany('INSERT OR IGNORE INTO users (username, password, employee_name) VALUES (?, ?, ?)', users)

    current_month_db = datetime.now().strftime("%Y-%m")
    cursor.execute("SELECT value FROM app_status WHERE key = 'last_month_reset'")
    tracker_res = cursor.fetchone()

    if tracker_res and tracker_res[0] == current_month_db:
        cursor.execute("SELECT COUNT(*) FROM qr_records WHERE time LIKE ?", (f"{current_month_db}%",))
        if cursor.fetchone()[0] == 0:
            cursor.execute("DELETE FROM app_status WHERE key = 'last_month_reset'")

    cursor.execute("SELECT COUNT(*) FROM parts_data")
    if cursor.fetchone()[0] == 0:
        initial_parts_data = [
            ("ACE GOLD BS-VI", "ACE GOLD BS-VI", "STG COLOUMN ASSY.", "RC5851000", "00553746100102", "NR", "R64051", 32,
             10, ""),
            ("ACE 900 DISEL BSVI", "ACE 900 DISEL BSVI", "STG COLOUMN ASSY.", "RC5911000", "00554546100129", "0a",
             "R64051", 32, 10, ""),
            ("ACE INTRA-V50", "ACE INTRA-V50", "STG COLOUMN ASSY.", "RC6031000", "00554946100106", "0b", "R64051", 32,
             10, ""),
            ("ACE GOLD RDE", "ACE GOLD RDE", "STG COLOUMN ASSY.", "RC6461000", "00513746100102", "0a", "R64051", 32, 10,
             ""),
            ("ACE EV PHASE-01", "ACE EV PHASE-01", "STG COLOUMN ASSY.", "RC7301000", "00555346100109", "0b", "R64051",
             32, 10, ""),
            ("ACE CORAL", "ACE CORAL", "STG COLOUMN ASSY.", "RC6961000", "00556446100103", "0d", "R64051", 32, 10, ""),
            ("ACE PHASE-02", "ACE PHASE-02", "STG COLOUMN ASSY.", "RC4121000", "00554546100115", "0c", "R64051", 32, 10,
             "")
        ]
        cursor.executemany(
            "INSERT INTO parts_data (model_name, project_name, part_name, rssl_part_no, customer_part_no, revision_no, vendor_code, length_of_character, camera_timeout, master_image_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            initial_parts_data)

    revision_updates = [("0a", "ACE GOLD RDE"), ("0c", "ACE PHASE-02"), ("0d", "ACE CORAL")]
    cursor.executemany("UPDATE parts_data SET revision_no = ? WHERE model_name = ?", revision_updates)
    conn.commit();
    cursor.close()
    return conn


class LoginApp(tk.Tk):
    def __init__(self, db_conn):
        super().__init__()
        self.db_conn = db_conn
        self.title("QR Code Detector Login")
        self.geometry("450x400")
        self.configure(bg="#2e8b57")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#2e8b57")
        style.configure("Header.TLabel", font=("Arial", 16, "bold"), foreground="white", background="#2e8b57")
        style.configure("TLabel", foreground="white", background="#2e8b57")
        style.configure("TButton", font=("Arial", 10, "bold"), background="#5cb85c", foreground="white")
        style.map("TButton", background=[("active", "#4cae4c")])
        self._create_login_frame()

    def _create_login_frame(self):
        login_frame = ttk.Frame(self, padding="30")
        login_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        ttk.Label(login_frame, text="QR Code Detector Login", style="Header.TLabel").pack(pady=20)
        ttk.Label(login_frame, text="Username:").pack(pady=5)
        self.username_entry = ttk.Entry(login_frame, width=30)
        self.username_entry.pack(pady=5)
        self.username_entry.bind("<Return>", lambda event: self.password_entry.focus_set())
        ttk.Label(login_frame, text="Password:").pack(pady=5)
        self.password_entry = ttk.Entry(login_frame, show="*", width=30)
        self.password_entry.pack(pady=5)
        self.password_entry.bind("<Return>", lambda event: self._authenticate_user())
        ttk.Label(login_frame, text="Employee Name:").pack(pady=5)
        self.employee_name_entry = ttk.Entry(login_frame, width=30)
        self.employee_name_entry.pack(pady=5)
        self.employee_name_entry.bind("<Return>", lambda event: self._authenticate_user())
        ttk.Button(login_frame, text="Login", command=self._authenticate_user).pack(pady=20)
        self.login_status_label = ttk.Label(login_frame, text="", foreground="red")
        self.login_status_label.pack(pady=5)

    def _authenticate_user(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        employee_name = self.employee_name_entry.get().strip()
        is_admin = False
        if username == "admin" and password == "1313":
            if not employee_name: employee_name = "System Admin"
            is_admin = True
        else:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT employee_name FROM users WHERE username = ? AND password = ?", (username, password))
            user_data = cursor.fetchone()
            if user_data:
                if not employee_name: employee_name = user_data[0]
            else:
                self.login_status_label.config(text="Invalid username or password.")
                return
        self.withdraw()
        QRGeneratorApp(self, username, employee_name, self.db_conn, is_admin)


if __name__ == "__main__":
    db_connection = create_database()
    login_app = LoginApp(db_connection)
    login_app.mainloop()
    db_connection.close()