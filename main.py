import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import customtkinter as ctk
import requests
from tkinter import messagebox

DATA_FILE = "play_points_accounts.json"
PERKS_URL = "https://play.google.com/store/points/perks?hl=en"
BATCH_URL = "https://play.google.com/_/PlayStoreUi/data/batchexecute"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def get_zone(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone(timedelta(hours=-7))


def current_week_key(tz_name: str = "America/Los_Angeles") -> str:
    tz = get_zone(tz_name)
    now = datetime.now(tz)
    days_since_fri = (now.weekday() - 4) % 7
    last_fri = (now - timedelta(days=days_since_fri)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    if now < last_fri:
        last_fri -= timedelta(days=7)
    return last_fri.strftime("%Y-%m-%d")


def next_friday_reset(tz_name: str = "America/Los_Angeles") -> datetime:
    tz = get_zone(tz_name)
    now = datetime.now(tz)
    days_until_fri = (4 - now.weekday()) % 7
    next_fri = (now + timedelta(days=days_until_fri)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    if now >= next_fri:
        next_fri += timedelta(days=7)
    return next_fri


def countdown_text(tz_name: str = "America/Los_Angeles") -> str:
    nf = next_friday_reset(tz_name)
    diff = int((nf - datetime.now(nf.tzinfo)).total_seconds())
    diff = max(0, diff)
    days = diff // 86400
    hours = (diff % 86400) // 3600
    minutes = (diff % 3600) // 60
    seconds = diff % 60
    return f"{days} يوم {hours:02d}:{minutes:02d}:{seconds:02d}"


def build_session_from_cookies_json(cookies_json_str: str) -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://play.google.com",
            "Referer": PERKS_URL,
        }
    )
    if not cookies_json_str:
        return sess

    try:
        data = json.loads(cookies_json_str)
        if isinstance(data, dict):
            for _, raw_cookie in data.items():
                if not isinstance(raw_cookie, str):
                    continue
                for part in raw_cookie.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        sess.cookies.set(k.strip(), v.strip(), domain=".google.com")
    except Exception:
        # دعم لصق نص الكوكيز الخام مباشرة أيضاً
        for part in cookies_json_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                sess.cookies.set(k.strip(), v.strip(), domain=".google.com")
    return sess


def extract_inner_payload(raw_response: str, target_rpc: str) -> str:
    for line in raw_response.splitlines():
        line = line.strip()
        if '"wrb.fr"' in line and target_rpc in line:
            try:
                arr = json.loads(line)
                for item in arr:
                    if (
                        isinstance(item, list)
                        and len(item) >= 3
                        and item[0] == "wrb.fr"
                        and item[1] == target_rpc
                        and item[2]
                    ):
                        return str(item[2])
            except Exception:
                pass
    return ""


def parse_won_points(inner_str: str) -> int:
    if not inner_str:
        return 0
    m = re.search(r"(?:won|earned|got|\+)\s*(\d+)\s*(?:Play\s*)?Points?", inner_str, re.I)
    if not m:
        m = re.search(r"(\d+)\s*Play\s*Points?", inner_str, re.I)
    if m:
        return int(m.group(1))

    cleaned = re.sub(r"WEEKLY_REWARD_PROB_\d+", "", inner_str)
    for num_str in re.findall(r'[\[,]"?(\d{1,4})"?[\],]', cleaned):
        val = int(num_str)
        if 1 <= val <= 1000:
            return val
    return 0


def claim_account_rpc(account: dict) -> tuple[bool, bool, int, str]:
    """
    ترجع: (نجاح, مستلم_مسبقاً, عدد_النقاط, رسالة_التفاصيل)
    """
    cookies_json = account.get("cookiesJson", "")
    if "SID=" not in cookies_json and "1PSID=" not in cookies_json:
        return False, False, 0, "الجلسة غير صالحة أو غير محفوظة"

    sess = build_session_from_cookies_json(cookies_json)
    try:
        resp = sess.get(PERKS_URL, timeout=20, allow_redirects=True)
    except Exception as e:
        return False, False, 0, f"خطأ في الاتصال: {e}"

    if "accounts.google.com" in resp.url:
        return False, False, 0, "انتهت صلاحية الجلسة، يرجى تحديث الكوكيز"

    html = resp.text
    m_at = re.search(r'"SNlM0e":"([^"]+)"', html)
    if not m_at:
        return False, False, 0, "تعذر استخراج رمز الأمان (SNlM0e) من الصفحة"

    at_token = m_at.group(1)
    m_bl = re.search(r'"cfb2h":"([^"]+)"', html)
    bl = m_bl.group(1) if m_bl else "boq_playuiserver_20261118.02_p0"
    m_fsid = re.search(r'"FdrFJe":"([^"]+)"', html)
    f_sid = m_fsid.group(1) if m_fsid else ""

    reward_ids = []
    for rid in re.findall(r"WEEKLY_REWARD_PROB_\d+", html):
        if rid not in reward_ids:
            reward_ids.append(rid)
    for default_id in [
        "WEEKLY_REWARD_PROB_2",
        "WEEKLY_REWARD_PROB_3",
        "WEEKLY_REWARD_PROB_4",
        "WEEKLY_REWARD_PROB_5",
        "WEEKLY_REWARD_PROB_6",
        "WEEKLY_REWARD_PROB_1",
    ]:
        if default_id not in reward_ids:
            reward_ids.append(default_id)

    def call_rpc(rpc_id: str, payload_list: list) -> str:
        f_req = json.dumps([[[rpc_id, json.dumps(payload_list), None, "generic"]]], separators=(",", ":"))
        params = {
            "rpcids": rpc_id,
            "source-path": "/store/points/perks",
            "bl": bl,
            "hl": "en",
            "soc-app": "121",
            "soc-platform": "1",
            "soc-device": "1",
            "rt": "c",
        }
        if f_sid:
            params["f.sid"] = f_sid
        r = sess.post(
            BATCH_URL,
            params=params,
            data={"f.req": f_req, "at": at_token},
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=20,
        )
        return r.text

    last_raw = ""
    for rid in reward_ids:
        try:
            call_rpc("xnSgJb", [None, [None, None, None, None, [rid]]])
            res_text = call_rpc("nTRzR", [None, None, [None, None, None, None, [rid]]])
            last_raw = res_text
            inner = extract_inner_payload(res_text, "nTRzR")
            if inner and rid in inner and "null,null,null,null,null" not in inner:
                pts = parse_won_points(inner)
                msg = f"حصلت على +{pts} نقاط ✓" if pts > 0 else "تم استلام الجائزة الأسبوعية بنجاح ✓"
                return True, False, pts, msg
        except Exception:
            continue

    lower_html = html.lower()
    if "available on friday" in lower_html or "check back on friday" in lower_html:
        return True, True, 0, "جائزة هذا الأسبوع مستلمة مسبقاً"

    snippet = re.sub(r"\s+", " ", last_raw)[:140]
    return False, False, 0, f"لم يتم استلام الجائزة.\nرد الخادم: {snippet}"


class PlayPointsDesktopApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Play Points Manager - نسخة الكمبيوتر")
        self.geometry("760x720")
        self.configure(fg_color="#0E1619")

        self.accounts: list[dict] = self.load_accounts()
        self.is_claiming_all = False
        self.auto_claim_var = ctk.BooleanVar(value=False)

        self._build_ui()
        self.refresh_list()
        self.update_clock()
        self.start_background_auto_loop()

    def load_accounts(self) -> list[dict]:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception:
                pass
        return []

    def save_accounts(self):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(self.accounts, f, ensure_ascii=False, indent=2)

    def is_account_claimed(self, acc: dict) -> bool:
        wk = acc.get("lastClaimedWeek", "")
        tz = acc.get("timezone", "America/Los_Angeles")
        return bool(wk) and wk == current_week_key(tz)

    def _build_ui(self):
        # بطاقة الرأس
        header = ctk.CTkFrame(self, fg_color="#162228", corner_radius=14, border_width=1, border_color="#23353E")
        header.pack(fill="x", padx=16, pady=(16, 10))

        lbl_blessing = ctk.CTkLabel(
            header,
            text="اللهم صل وسلم على نبينا محمد وعلى آله وصحبه أجمعين",
            font=("Segoe UI", 13),
            text_color="#CBD5E1",
        )
        lbl_blessing.pack(pady=(10, 2))

        self.lbl_stats = ctk.CTkLabel(
            header,
            text="جاري الحساب...",
            font=("Segoe UI", 15, "bold"),
            text_color="#FFFFFF",
        )
        self.lbl_stats.pack(pady=(0, 8))

        controls_row = ctk.CTkFrame(header, fg_color="transparent")
        controls_row.pack(fill="x", padx=14, pady=(0, 10))

        self.sw_auto = ctk.CTkSwitch(
            controls_row,
            text="استلام تلقائي يوم الجمعة (في الخلفية)",
            variable=self.auto_claim_var,
            progress_color="#28D7B8",
            font=("Segoe UI", 12),
        )
        self.sw_auto.pack(side="right")

        btn_export = ctk.CTkButton(
            controls_row,
            text="نسخ احتياطي (JSON)",
            width=140,
            height=30,
            fg_color="#1E303A",
            hover_color="#29414F",
            font=("Segoe UI", 12, "bold"),
            command=self.export_json_to_clipboard,
        )
        btn_export.pack(side="left")

        # شريط البحث
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_list())
        entry_search = ctk.CTkEntry(
            self,
            textvariable=self.search_var,
            placeholder_text="بحث بالاسم أو البريد أو التصنيف...",
            height=38,
            fg_color="#162228",
            border_color="#23353E",
            text_color="#FFFFFF",
            justify="right",
        )
        entry_search.pack(fill="x", padx=16, pady=(0, 10))

        # قائمة الحسابات القابلة للتمرير
        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        # الشريط السفلي
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(0, 16))

        btn_import = ctk.CTkButton(
            bottom,
            text="+ استيراد من الهاتف / إضافة حساب (JSON)",
            height=44,
            fg_color="#28D7B8",
            hover_color="#20B89D",
            text_color="#082026",
            font=("Segoe UI", 14, "bold"),
            command=self.open_import_modal,
        )
        btn_import.pack(side="right", fill="x", expand=True, padx=(8, 0))

        self.btn_claim_all = ctk.CTkButton(
            bottom,
            text="استلام الكل (10ث)",
            height=44,
            fg_color="#1B2A32",
            hover_color="#263B46",
            border_width=1,
            border_color="#2F4754",
            font=("Segoe UI", 14, "bold"),
            command=self.claim_all_accounts,
        )
        self.btn_claim_all.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def update_clock(self):
        claimed = sum(1 for a in self.accounts if self.is_account_claimed(a))
        total = len(self.accounts)
        cd = countdown_text()
        self.lbl_stats.configure(text=f"{cd}  •  تم استلام {claimed} / {total} هذا الأسبوع")
        self.after(1000, self.update_clock)

    def refresh_list(self):
        for w in self.scroll_frame.winfo_children():
            w.destroy()

        q = self.search_var.get().strip().lower()
        filtered = [
            a
            for a in self.accounts
            if not q
            or q in a.get("name", "").lower()
            or q in a.get("email", "").lower()
            or q in a.get("category", "").lower()
        ]

        if not filtered:
            ctk.CTkLabel(
                self.scroll_frame,
                text="لا توجد حسابات بعد. اضغط «+ استيراد من الهاتف / إضافة حساب» للبدء.",
                text_color="#94A3B8",
                font=("Segoe UI", 13),
            ).pack(pady=40)
            return

        for acc in filtered:
            claimed = self.is_account_claimed(acc)
            card = ctk.CTkFrame(
                self.scroll_frame,
                fg_color="#162228",
                corner_radius=12,
                border_width=1,
                border_color="#22333B",
            )
            card.pack(fill="x", pady=5)

            btn_claim = ctk.CTkButton(
                card,
                text="استلام",
                width=85,
                height=34,
                fg_color="#122633",
                hover_color="#1B384A",
                text_color="#38BDF8",
                font=("Segoe UI", 12, "bold"),
                command=lambda a=acc: self.claim_single(a),
            )
            btn_claim.pack(side="left", padx=12, pady=12)

            btn_del = ctk.CTkButton(
                card,
                text="حذف",
                width=55,
                height=34,
                fg_color="#3B1519",
                hover_color="#541D23",
                text_color="#FCA5A5",
                font=("Segoe UI", 11),
                command=lambda a=acc: self.delete_account(a),
            )
            btn_del.pack(side="left", padx=(0, 4))

            info = ctk.CTkFrame(card, fg_color="transparent")
            info.pack(side="right", fill="x", expand=True, padx=12, pady=8)

            title_txt = acc.get("name", "Account")
            if acc.get("email"):
                title_txt += f"  ({acc['email']})"

            ctk.CTkLabel(
                info,
                text=title_txt,
                font=("Segoe UI", 14, "bold"),
                text_color="#FFFFFF",
                anchor="e",
            ).pack(fill="x")

            status_txt = (
                f"تم الاستلام ✓ {acc.get('lastPointsText', '')}".strip()
                if claimed
                else "بانتظار الاستلام"
            )
            status_color = "#22C55E" if claimed else "#F59E0B"
            ctk.CTkLabel(
                info,
                text=status_txt,
                font=("Segoe UI", 12, "bold"),
                text_color=status_color,
                anchor="e",
            ).pack(fill="x")

    def delete_account(self, acc: dict):
        if messagebox.askyesno("تأكيد الحذف", f"هل تريد حذف الحساب {acc.get('name')}؟"):
            self.accounts = [a for a in self.accounts if a.get("id") != acc.get("id")]
            self.save_accounts()
            self.refresh_list()

    def export_json_to_clipboard(self):
        raw = json.dumps(self.accounts, ensure_ascii=False)
        self.clipboard_clear()
        self.clipboard_append(raw)
        messagebox.showinfo("تم النسخ", f"تم نسخ {len(self.accounts)} حساب إلى الحافظة ✓")

    def open_import_modal(self):
        win = ctk.CTkToplevel(self)
        win.title("استيراد من الهاتف أو إضافة حساب")
        win.geometry("540x430")
        win.configure(fg_color="#0E1619")
        win.grab_set()

        ctk.CTkLabel(
            win,
            text="الطريقة الأسرع: الصق نص النسخ الاحتياطي (JSON) المنسوخ من تطبيق الهاتف",
            font=("Segoe UI", 13, "bold"),
            text_color="#28D7B8",
        ).pack(pady=(14, 6), padx=16)

        txt_box = ctk.CTkTextbox(win, height=160, fg_color="#162228", border_color="#23353E")
        txt_box.pack(fill="x", padx=16, pady=6)

        def do_import_json():
            raw = txt_box.get("1.0", "end").strip()
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    existing_ids = {a.get("id") for a in self.accounts}
                    added = 0
                    for item in parsed:
                        if isinstance(item, dict):
                            if item.get("id") in existing_ids:
                                self.accounts = [
                                    item if a.get("id") == item.get("id") else a
                                    for a in self.accounts
                                ]
                            else:
                                self.accounts.append(item)
                            added += 1
                    self.save_accounts()
                    self.refresh_list()
                    win.destroy()
                    messagebox.showinfo("نجاح", f"تم استيراد وتحديث {added} حساب بنجاح ✓")
                    return
            except Exception:
                pass
            messagebox.showerror("خطأ", "يرجى لصق نص JSON صحيح من زر النسخ الاحتياطي في الهاتف.")

        ctk.CTkButton(
            win,
            text="استيراد جميع الحسابات (JSON)",
            fg_color="#28D7B8",
            text_color="#082026",
            font=("Segoe UI", 13, "bold"),
            command=do_import_json,
        ).pack(pady=(4, 14))

        ctk.CTkLabel(
            win,
            text="أو أضف حساباً يدوياً بلصق الكوكيز من متصفح Brave:",
            font=("Segoe UI", 12),
            text_color="#94A3B8",
        ).pack(pady=(4, 4))

        ent_name = ctk.CTkEntry(
            win,
            placeholder_text=f"اسم الحساب (مثلاً A{len(self.accounts) + 1})",
            fg_color="#162228",
        )
        ent_name.pack(fill="x", padx=16, pady=4)

        ent_cookie = ctk.CTkEntry(
            win,
            placeholder_text="الصق سطر Cookie الخاص بـ play.google.com هنا...",
            fg_color="#162228",
        )
        ent_cookie.pack(fill="x", padx=16, pady=4)

        def add_single_manual():
            c_val = ent_cookie.get().strip()
            if not c_val:
                return
            name = ent_name.get().strip() or f"A{len(self.accounts) + 1}"
            self.accounts.append(
                {
                    "id": str(uuid.uuid4()),
                    "name": name,
                    "email": "",
                    "category": "",
                    "note": "",
                    "timezone": "America/Los_Angeles",
                    "cookiesJson": json.dumps({"https://play.google.com": c_val}),
                    "lastClaimedWeek": "",
                    "lastPointsText": "",
                }
            )
            self.save_accounts()
            self.refresh_list()
            win.destroy()

        ctk.CTkButton(
            win,
            text="إضافة الحساب اليدوي",
            fg_color="#1E303A",
            command=add_single_manual,
        ).pack(pady=8)

    def claim_single(self, acc: dict):
        if self.is_claiming_all:
            return

        def worker():
            ok, already, pts, msg = claim_account_rpc(acc)
            if ok:
                acc["lastClaimedWeek"] = current_week_key(acc.get("timezone", "America/Los_Angeles"))
                if pts > 0:
                    acc["lastPointsText"] = f"+{pts} نقاط"
                elif not already:
                    acc["lastPointsText"] = "تم الاستلام"
                self.save_accounts()
            self.after(0, self.refresh_list)
            self.after(
                0,
                lambda: messagebox.showinfo("مبروك!" if ok else "تنبيه", f"{acc.get('name')}: {msg}"),
            )

        threading.Thread(target=worker, daemon=True).start()

    def claim_all_accounts(self):
        if self.is_claiming_all:
            return
        pending = [a for a in self.accounts if not self.is_account_claimed(a)]
        if not pending:
            messagebox.showinfo("مكتمل", "جميع الحسابات تم الاستلام منها هذا الأسبوع ✓")
            return

        self.is_claiming_all = True

        def worker():
            success_count = 0
            total_pts = 0
            for idx, acc in enumerate(pending):
                self.after(
                    0,
                    lambda i=idx, n=acc.get("name"): self.btn_claim_all.configure(
                        text=f"جارٍ الاستلام ({i + 1}/{len(pending)}): {n}"
                    ),
                )
                ok, already, pts, _ = claim_account_rpc(acc)
                if ok:
                    acc["lastClaimedWeek"] = current_week_key(acc.get("timezone", "America/Los_Angeles"))
                    if pts > 0:
                        acc["lastPointsText"] = f"+{pts} نقاط"
                        total_pts += pts
                    elif not already:
                        acc["lastPointsText"] = "تم الاستلام"
                    if not already:
                        success_count += 1
                    self.save_accounts()
                    self.after(0, self.refresh_list)

                if idx < len(pending) - 1:
                    time.sleep(10)

            self.is_claiming_all = False
            self.after(0, lambda: self.btn_claim_all.configure(text="استلام الكل (10ث)"))
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "اكتمل الاستلام ✓",
                    f"تم الاستلام من {success_count} حساب\nمجموع النقاط الجديدة: +{total_pts} نقطة",
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def start_background_auto_loop(self):
        def loop():
            while True:
                time.sleep(1800)  # فحص كل 30 دقيقة
                if self.auto_claim_var.get() and not self.is_claiming_all:
                    pending = [a for a in self.accounts if not self.is_account_claimed(a)]
                    if pending:
                        self.after(0, self.claim_all_accounts)

        threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    app = PlayPointsDesktopApp()
    app.mainloop()
