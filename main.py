#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests


# ----------------------------
# Config
# ----------------------------

@dataclass(frozen=True)
class Config:
    base_url: str
    username: str
    password: str

    poll_interval_seconds: int
    timeout_seconds: float

    telegram_bot_token: str
    telegram_chat_id: str  # keep as string to support negative group ids too
    telegram_poll_seconds: int

    notify_on_first_run: bool
    state_file: str

    @staticmethod
    def from_env() -> "Config":
        def req(name: str) -> str:
            v = os.getenv(name)
            if not v:
                raise ValueError(f"Missing required env var: {name}")
            return v

        return Config(
            base_url=os.getenv("KBTU_BASE_URL", "https://wsp2.kbtu.kz").rstrip("/"),
            username=req("KBTU_USERNAME"),
            password=req("KBTU_PASSWORD"),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
            telegram_bot_token=req("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=req("TELEGRAM_CHAT_ID"),
            telegram_poll_seconds=int(os.getenv("TELEGRAM_POLL_SECONDS", "2")),
            notify_on_first_run=os.getenv("NOTIFY_ON_FIRST_RUN", "1").strip().lower() in ("1", "true", "yes"),
            state_file=os.getenv("STATE_FILE", "wsp_reg_state.json"),
        )


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
    )


# ----------------------------
# State model
# ----------------------------

@dataclass
class RightsSnapshot:
    can_registration: Optional[bool]
    can_schedule_edit: Optional[bool]
    start: Optional[str]
    missing: List[str]

    def key_fields(self) -> Dict[str, Any]:
        return {
            "canRegistration": self.can_registration,
            "canScheduleEdit": self.can_schedule_edit,
            "start": self.start,
        }


def load_state(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        logging.exception("Failed to read state file; ignoring.")
        return None


def save_state(path: str, state: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ----------------------------
# KBTU client
# ----------------------------

class KbtuClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": os.getenv(
                    "USER_AGENT",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                ),
                "Origin": self.cfg.base_url,
                "Referer": f"{self.cfg.base_url}/login",
            }
        )
        self._student_id: Optional[int] = None


    @property
    def timeout(self) -> Tuple[float, float]:
        # Kill slow requests: connect timeout small, read timeout bounded.
        # Total wall clock is still bounded by read timeout.
        connect = min(3.0, self.cfg.timeout_seconds)
        read = self.cfg.timeout_seconds
        return (connect, read)

    @property
    def student_id(self) -> int:
        if self._student_id is None:
            raise RuntimeError("student_id not set. Call login() first.")
        return self._student_id

    def login(self) -> Dict[str, Any]:
        url = f"{self.cfg.base_url}/bachelor/api/login"
        data = {"username": self.cfg.username, "password": self.cfg.password}

        logging.info("Logging in...")
        resp = self.session.post(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        js = resp.json()

        sid = js.get("id")
        if not isinstance(sid, int):
            raise ValueError(f"Login response missing integer 'id'. Got {sid!r}")
        self._student_id = sid

        has_jsession = any(c.name == "JSESSIONID" for c in self.session.cookies)
        if not has_jsession:
            logging.warning("Login ok but JSESSIONID not found. Cookies: %s", list(self.session.cookies.keys()))
        else:
            logging.info("Login OK. student_id=%s, JSESSIONID present.", self._student_id)

        return js

    def get_registration(self) -> Dict[str, Any]:
        url = f"{self.cfg.base_url}/bachelor/api/registration/student/{self.student_id}"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def parse_rights(payload: Dict[str, Any]) -> RightsSnapshot:
        rights = payload.get("RIGHTS")
        if not isinstance(rights, dict):
            return RightsSnapshot(None, None, None, missing=["RIGHTS"])

        can_reg = rights.get("canRegistration")
        can_sched = rights.get("canScheduleEdit")
        start = rights.get("start")

        # normalize types
        if can_reg is not None and not isinstance(can_reg, bool):
            can_reg = bool(can_reg)
        if can_sched is not None and not isinstance(can_sched, bool):
            can_sched = bool(can_sched)
        if start is not None and not isinstance(start, str):
            start = str(start)

        missing: List[str] = []
        if "canRegistration" not in rights:
            missing.append("canRegistration")
        if "canScheduleEdit" not in rights:
            missing.append("canScheduleEdit")
        if "start" not in rights:
            missing.append("start")

        return RightsSnapshot(
            can_registration=can_reg if "canRegistration" in rights else None,
            can_schedule_edit=can_sched if "canScheduleEdit" in rights else None,
            start=start if "start" in rights else None,
            missing=missing,
        )


# ----------------------------
# Telegram bot
# ----------------------------

class TelegramBot:
    def __init__(self, token: str, chat_id: str, timeout_seconds: float) -> None:
        self.chat_id = str(chat_id)
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        self.timeout = (min(3.0, timeout_seconds), timeout_seconds)
        self._last_update_id: Optional[int] = None

    def send(self, text: str) -> None:
        url = f"{self.base}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        try:
            resp = self.session.post(url, json=payload, timeout=self.timeout)
            if resp.status_code >= 400:
                logging.warning("Telegram send failed: %s %s", resp.status_code, resp.text)
        except requests.RequestException:
            logging.warning("Telegram send exception; ignoring.", exc_info=True)

    def get_updates(self) -> List[Dict[str, Any]]:
        url = f"{self.base}/getUpdates"
        params: Dict[str, Any] = {"timeout": 0}
        if self._last_update_id is not None:
            params["offset"] = self._last_update_id + 1

        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return []
        updates = data.get("result", [])

        for u in updates:
            uid = u.get("update_id")
            if isinstance(uid, int):
                self._last_update_id = uid
        return updates

    def iter_commands(self) -> List[Tuple[str, str]]:
        """
        Returns list of (chat_id, text).
        """
        out: List[Tuple[str, str]] = []
        try:
            updates = self.get_updates()
        except requests.RequestException:
            return out

        for u in updates:
            msg = u.get("message") or u.get("edited_message")
            if not isinstance(msg, dict):
                continue
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            text = msg.get("text")
            if cid is None or not isinstance(text, str):
                continue
            out.append((str(cid), text.strip()))
        return out


# ----------------------------
# Formatting / diff
# ----------------------------

def reg_icon(can_registration: Optional[bool]) -> str:
    return "✅" if can_registration is True else "❌"


def format_status(payload: Dict[str, Any], snap: RightsSnapshot, student_id: int) -> str:
    now = payload.get("CURRENT_DATE_TIME")
    student = payload.get("STUDENT") or {}
    student_code = student.get("code")
    student_name_ru = student.get("nameRu")

    icon = reg_icon(snap.can_registration)
    miss_line = f"- missing: {', '.join(snap.missing)}\n" if snap.missing else ""

    return (
        f"{icon} KBTU Registration status\n"
        f"- server_time: {now}\n"
        f"- student_id: {student_id}\n"
        f"- student: {student_code} | {student_name_ru}\n"
        f"- canRegistration: {snap.can_registration}\n"
        f"- canScheduleEdit: {snap.can_schedule_edit}\n"
        f"- start: {snap.start}\n"
        f"{miss_line}"
    ).rstrip()


def diff_fields(old: RightsSnapshot, new: RightsSnapshot) -> Dict[str, Tuple[Any, Any]]:
    diffs: Dict[str, Tuple[Any, Any]] = {}
    for k in ("canRegistration", "canScheduleEdit", "start"):
        o = old.key_fields().get(k)
        n = new.key_fields().get(k)
        if o != n:
            diffs[k] = (o, n)
    return diffs


def missing_changed(old_missing: List[str], new_missing: List[str]) -> bool:
    return set(old_missing) != set(new_missing)


# ----------------------------
# App
# ----------------------------

class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.kbtu = KbtuClient(cfg)
        self.tg = TelegramBot(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.timeout_seconds)

        self.state = load_state(cfg.state_file) or {}
        self.last_snapshot: Optional[RightsSnapshot] = None
        self.last_missing: List[str] = self.state.get("missing", []) if isinstance(self.state.get("missing"), list) else []

        # restore last snapshot if available
        if isinstance(self.state.get("rights"), dict):
            r = self.state["rights"]
            self.last_snapshot = RightsSnapshot(
                can_registration=r.get("canRegistration"),
                can_schedule_edit=r.get("canScheduleEdit"),
                start=r.get("start"),
                missing=self.last_missing,
            )

    def persist(self, snap: RightsSnapshot) -> None:
        self.state["rights"] = {
            "canRegistration": snap.can_registration,
            "canScheduleEdit": snap.can_schedule_edit,
            "start": snap.start,
        }
        self.state["missing"] = list(snap.missing)
        self.state["saved_at_epoch"] = int(time.time())
        # optional student_id persistence
        if getattr(self.kbtu, "_student_id", None) is not None:
            self.state["student_id"] = self.kbtu.student_id
        save_state(self.cfg.state_file, self.state)

    def ensure_login(self) -> bool:
        try:
            self.kbtu.login()
            return True
        except requests.Timeout:
            self.tg.send("❌ Login failed: timeout (>=10s).")
            return False
        except requests.RequestException as e:
            self.tg.send(f"❌ Login failed: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            self.tg.send(f"❌ Login failed: {type(e).__name__}: {e}")
            return False

    def check_once(self, notify: bool) -> Optional[Tuple[Dict[str, Any], RightsSnapshot]]:
        if getattr(self.kbtu, "_student_id", None) is None:
            if not self.ensure_login():
                return None

        # Try request; relogin on 401/403 once
        try:
            payload = self.kbtu.get_registration()
        except requests.Timeout:
            self.tg.send("❌ KBTU check failed: timeout (>=10s).")
            return None
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (401, 403):
                self.tg.send("❌ Session expired (401/403). Re-login...")
                if not self.ensure_login():
                    return None
                try:
                    payload = self.kbtu.get_registration()
                except Exception as e2:
                    self.tg.send(f"❌ Retry failed: {type(e2).__name__}: {e2}")
                    return None
            else:
                self.tg.send(f"❌ KBTU check failed: HTTP {code}")
                return None
        except requests.RequestException as e:
            self.tg.send(f"❌ KBTU check failed: {type(e).__name__}: {e}")
            return None

        snap = self.kbtu.parse_rights(payload)

        # Missing fields notification (only when missing-set changes)
        if missing_changed(self.last_missing, snap.missing):
            if snap.missing:
                self.tg.send(f"❌ KBTU RIGHTS missing fields: {', '.join(snap.missing)}")
            else:
                self.tg.send("✅ KBTU RIGHTS fields are present again.")
            self.last_missing = list(snap.missing)

        if self.last_snapshot is None:
            self.last_snapshot = snap
            self.persist(snap)
            if self.cfg.notify_on_first_run:
                self.tg.send(format_status(payload, snap, self.kbtu.student_id))
            return payload, snap

        diffs = diff_fields(self.last_snapshot, snap)
        if diffs and notify:
            icon = reg_icon(snap.can_registration)
            lines = [f"{icon} KBTU RIGHTS changed!", f"- student_id: {self.kbtu.student_id}", f"- server_time: {payload.get('CURRENT_DATE_TIME')}"]
            for k, (o, n) in diffs.items():
                lines.append(f"- {k}: {o} -> {n}")
            self.tg.send("\n".join(lines))

        self.last_snapshot = snap
        self.persist(snap)
        return payload, snap

    def handle_commands(self) -> None:
        for cid, text in self.tg.iter_commands():
            # Only accept commands from configured chat
            if cid != str(self.cfg.telegram_chat_id):
                continue
            if text.startswith("/hc"):
                res = self.check_once(notify=False)
                if res is None:
                    self.tg.send("❌ Manual /hc failed (see previous error).")
                else:
                    payload, snap = res
                    self.tg.send("Manual /hc:\n" + format_status(payload, snap, self.kbtu.student_id))

    def run(self) -> None:
        # initial login attempt (non-fatal)
        self.ensure_login()

        next_poll = time.monotonic()
        while True:
            self.handle_commands()

            now = time.monotonic()
            if now >= next_poll:
                self.check_once(notify=True)
                next_poll = now + self.cfg.poll_interval_seconds

            time.sleep(self.cfg.telegram_poll_seconds)


def main() -> int:
    setup_logging()
    try:
        cfg = Config.from_env()
    except Exception as e:
        logging.error("Config error: %s", e)
        return 2

    App(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
