from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import time

from .capital_crosscheck import CrossCheckResult, evaluate_seq13


class CapitalReplySessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionHealth:
    ready: bool
    local_connected: bool
    reply_state: int | None
    reply_connected: bool
    account_selected: bool
    action: str
    message: str = ""


@dataclass
class ReplySnapshot:
    replay_complete: bool = False
    all_rows: list[str] = field(default_factory=list)
    tc_rows: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    announcements: list[str] = field(default_factory=list)

    real_balance_rows: list[str] = field(default_factory=list)
    real_balance_complete: bool = False

    def copy(self) -> "ReplySnapshot":
        return ReplySnapshot(
            replay_complete=self.replay_complete,
            all_rows=list(self.all_rows),
            tc_rows=list(self.tc_rows),
            accounts=list(self.accounts),
            announcements=list(self.announcements),
            real_balance_rows=list(self.real_balance_rows),
            real_balance_complete=self.real_balance_complete,
        )


def load_env_file(path: str | Path, *, override: bool = False) -> Path:
    p = Path(path)
    if not p.exists():
        raise CapitalReplySessionError(f"env file not found: {p}")
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value
    return p


def find_default_env(project_root: str | Path) -> Path | None:
    root = Path(project_root)
    for p in (root / ".env", root / "掛單測試" / ".env"):
        if p.exists():
            return p
    for folder in (root, root / "掛單測試"):
        if folder.exists():
            envs = sorted(folder.glob("*.env"))
            if envs:
                return envs[0]
    return None


def find_skcom_dll(project_root: str | Path) -> Path:
    explicit = os.getenv("CAPITAL_COM_DLL", "").strip()
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise CapitalReplySessionError(f"CAPITAL_COM_DLL not found: {p}")

    root = Path(project_root)
    hits = list(root.glob("CapitalAPI_2.13.59/**/x64/SKCOM.dll"))
    if not hits:
        hits = list(root.glob("**/CapitalAPI_2.13.59/**/x64/SKCOM.dll"))
    if not hits:
        raise CapitalReplySessionError(f"Cannot find x64 SKCOM.dll under {root}")

    preferred = [p for p in hits if "SKCOMVerify" not in str(p)]
    return (preferred or hits)[0]


class CapitalReplySession:
    def __init__(
        self,
        *,
        project_root: str | Path,
        user: str | None = None,
        password: str | None = None,
        account: str | None = None,
        dll_path: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.user = (user or os.getenv("CAPITAL_USER_ID", "")).strip()
        self.password = (password or os.getenv("CAPITAL_PASSWORD", "")).strip()
        self.account_override = (account or os.getenv("CAPITAL_ACCOUNT", "")).strip()
        self.dll_path = Path(dll_path) if dll_path else find_skcom_dll(self.project_root)

        self.snapshot = ReplySnapshot()
        self.account: str | None = None
        self._comtypes = None
        self._sk = None
        self._skC = None
        self._skO = None
        self._skR = None
        self._reply_conn = None
        self._order_conn = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(
        self,
        *,
        account_wait_seconds: float = 8.0,
        replay_wait_seconds: float = 20.0,
        replay_grace_seconds: float = 1.0,
    ) -> ReplySnapshot:
        if not self.user or not self.password:
            raise CapitalReplySessionError("Missing CAPITAL_USER_ID or CAPITAL_PASSWORD.")
        if self._connected:
            return self.snapshot.copy()

        import comtypes.client
        comtypes.client.GetModule(str(self.dll_path))
        import comtypes.gen.SKCOMLib as sk

        self._comtypes = comtypes.client
        self._sk = sk
        self._skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        self._skO = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)
        self._skR = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

        state = self.snapshot

        class ReplyEvents:
            def OnReplyMessage(_self, bstrUserID, bstrMessage):
                state.announcements.append(str(bstrMessage))
                return -1

            def OnComplete(_self, bstrUserID):
                state.replay_complete = True

            def OnNewData(_self, bstrUserID, bstrData):
                raw = str(bstrData)
                state.all_rows.append(raw)
                parts = raw.split(",")
                if len(parts) >= 4 and parts[1].strip() == "TC":
                    state.tc_rows.append(raw)

            def OnReplyClear(_self, bstrMarket):
                return None

        class OrderEvents:
            def OnAccount(_self, bstrLogInID, bstrAccountData):
                raw = str(bstrAccountData)
                p = raw.split(",")
                if len(p) >= 4 and p[0].strip() == "TS":
                    acct = p[1].strip() + p[3].strip()
                    if acct and acct not in state.accounts:
                        state.accounts.append(acct)

            def OnRealBalanceReport(_self, bstrData):
                raw = str(bstrData)
                if raw.startswith("##"):
                    state.real_balance_complete = True
                else:
                    state.real_balance_rows.append(raw)

        self._reply_conn = comtypes.client.GetEvents(self._skR, ReplyEvents())
        self._order_conn = comtypes.client.GetEvents(self._skO, OrderEvents())

        rc = self._skC.SKCenterLib_Login(self.user, self.password)
        if rc != 0:
            raise CapitalReplySessionError(f"SKCenterLib_Login failed: rc={rc}")

        rc = self._skO.SKOrderLib_Initialize()
        if rc != 0:
            raise CapitalReplySessionError(f"SKOrderLib_Initialize failed: rc={rc}")

        rc = self._skO.ReadCertByID(self.user)
        if rc != 0:
            raise CapitalReplySessionError(f"ReadCertByID failed: rc={rc}")

        rc = self._skO.GetUserAccount()
        if rc != 0:
            raise CapitalReplySessionError(f"GetUserAccount failed: rc={rc}")

        if not self.account_override:
            end = time.time() + account_wait_seconds
            while time.time() < end and not state.accounts:
                comtypes.client.PumpEvents(0.25)

        if self.account_override:
            self.account = self.account_override
        elif len(state.accounts) == 1:
            self.account = state.accounts[0]
        elif len(state.accounts) == 0:
            raise CapitalReplySessionError("No TS stock account received.")
        else:
            raise CapitalReplySessionError("Multiple TS accounts; set CAPITAL_ACCOUNT.")

        rc = self._skR.SKReplyLib_ConnectByID(self.user)
        if rc != 0:
            raise CapitalReplySessionError(f"SKReplyLib_ConnectByID failed: rc={rc}")

        end = time.time() + replay_wait_seconds
        while time.time() < end and not state.replay_complete:
            comtypes.client.PumpEvents(0.25)

        if not state.replay_complete:
            raise CapitalReplySessionError("No OnComplete; replay is not trustworthy.")

        end = time.time() + max(0.0, replay_grace_seconds)
        while time.time() < end:
            comtypes.client.PumpEvents(0.25)

        self._connected = True
        return state.copy()

    def pump(self, seconds: float) -> ReplySnapshot:
        if not self._connected or self._comtypes is None:
            raise CapitalReplySessionError("Session is not connected.")
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self._comtypes.PumpEvents(0.25)
        return self.snapshot.copy()

    def tc_rows_copy(self) -> list[str]:
        return list(self.snapshot.tc_rows)

    def query_real_balance_rows(self, timeout_seconds: float = 10.0) -> list[str]:
        """Query current securities inventory using GetRealBalanceReport."""
        if not self._connected or not self.account:
            raise CapitalReplySessionError("Session is not connected.")
        if not hasattr(self._skO, "GetRealBalanceReport"):
            raise CapitalReplySessionError("GetRealBalanceReport is unavailable.")

        self.snapshot.real_balance_rows.clear()
        self.snapshot.real_balance_complete = False

        rc = self._skO.GetRealBalanceReport(self.user, self.account)
        if rc != 0:
            raise CapitalReplySessionError(f"GetRealBalanceReport failed: rc={rc}")

        end = time.time() + timeout_seconds
        while time.time() < end and not self.snapshot.real_balance_complete:
            self._comtypes.PumpEvents(0.25)

        if not self.snapshot.real_balance_complete:
            raise CapitalReplySessionError(
                "GetRealBalanceReport timed out before ## completion marker."
            )

        return list(self.snapshot.real_balance_rows)


    def reply_connection_state(self) -> int | None:
        """Return SKReplyLib_IsConnectedByID state when available.

        Observed SDK semantics:
          1 = connected
          0/2/other = not considered healthy for order execution.
        """
        if self._skR is None or not self.user:
            return None
        fn = getattr(self._skR, "SKReplyLib_IsConnectedByID", None)
        if fn is None:
            return None
        try:
            return int(fn(self.user))
        except Exception:
            return None

    def health_check(self) -> SessionHealth:
        local = bool(self._connected)
        account_ok = bool(self.account)

        if not local or self._skR is None:
            return SessionHealth(
                ready=False,
                local_connected=local,
                reply_state=None,
                reply_connected=False,
                account_selected=account_ok,
                action="LOGIN_REQUIRED",
                message="No active in-process Capital session.",
            )

        state = self.reply_connection_state()

        # If the installed DLL exposes IsConnectedByID, require state=1.
        if state is not None:
            reply_ok = state == 1
            return SessionHealth(
                ready=local and reply_ok and account_ok,
                local_connected=local,
                reply_state=state,
                reply_connected=reply_ok,
                account_selected=account_ok,
                action="REUSE" if reply_ok and account_ok else "REPLY_RECONNECT_REQUIRED",
                message=(
                    "Existing session is healthy."
                    if reply_ok and account_ok
                    else f"SKReply connection is not healthy (state={state})."
                ),
            )

        # Older/unusual typelib: fall back to the session's established state.
        return SessionHealth(
            ready=local and account_ok and self.snapshot.replay_complete,
            local_connected=local,
            reply_state=None,
            reply_connected=bool(self.snapshot.replay_complete),
            account_selected=account_ok,
            action="REUSE" if local and account_ok and self.snapshot.replay_complete else "LOGIN_REQUIRED",
            message="IsConnectedByID unavailable; using established replay/session state.",
        )

    def _reconnect_reply_only(
        self,
        *,
        replay_wait_seconds: float = 20.0,
        replay_grace_seconds: float = 0.5,
    ) -> None:
        """Reconnect SKReply only. This deliberately does NOT call SKCenterLib_Login."""
        if self._skR is None or self._comtypes is None:
            raise CapitalReplySessionError(
                "Cannot reconnect SKReply without an existing logged-in session."
            )

        # A new ConnectByID should produce a new replay completion.
        self.snapshot.replay_complete = False
        rc = self._skR.SKReplyLib_ConnectByID(self.user)
        if rc != 0:
            raise CapitalReplySessionError(
                f"SKReplyLib_ConnectByID reconnect failed: rc={rc}"
            )

        end = time.time() + replay_wait_seconds
        while time.time() < end and not self.snapshot.replay_complete:
            self._comtypes.PumpEvents(0.25)

        if not self.snapshot.replay_complete:
            raise CapitalReplySessionError(
                "Reply reconnect did not reach OnComplete."
            )

        end = time.time() + max(0.0, replay_grace_seconds)
        while time.time() < end:
            self._comtypes.PumpEvents(0.25)

    def ensure_ready(
        self,
        *,
        allow_reply_reconnect: bool = True,
    ) -> SessionHealth:
        """Ensure the current session is usable before BUY/SELL.

        Important:
        - Healthy existing session -> reuse it.
        - Reply channel disconnected -> reconnect SKReply only.
        - Never blindly call SKCenterLib_Login a second time.
        - If a full login would be required, raise and let the action fail/skip.
        """
        health = self.health_check()
        if health.ready:
            return health

        if (
            allow_reply_reconnect
            and health.local_connected
            and health.account_selected
            and health.action == "REPLY_RECONNECT_REQUIRED"
        ):
            self._reconnect_reply_only()
            health = self.health_check()
            if health.ready:
                return SessionHealth(
                    ready=True,
                    local_connected=True,
                    reply_state=health.reply_state,
                    reply_connected=True,
                    account_selected=True,
                    action="REPLY_RECONNECTED",
                    message="Existing login reused; SKReply channel reconnected.",
                )

        raise CapitalReplySessionError(
            f"Capital session is not ready: {health.message} "
            f"(action={health.action}, reply_state={health.reply_state})"
        )

    def get_order_report9(self) -> str:
        if not self._connected or not self.account:
            raise CapitalReplySessionError("Session is not connected.")
        try:
            return str(self._skO.GetOrderReport(self.user, self.account, 9) or "")
        except Exception as exc:
            raise CapitalReplySessionError(
                f"GetOrderReport(9) failed: {type(exc).__name__}: {exc}"
            ) from exc

    def crosscheck_seq13(self, seq13: str, *, include_report9: bool = True) -> CrossCheckResult:
        report9 = None
        if include_report9:
            try:
                report9 = self.get_order_report9()
            except CapitalReplySessionError:
                pass
        return evaluate_seq13(
            seq13,
            replay_rows=self.snapshot.tc_rows,
            replay_complete=self.snapshot.replay_complete,
            get_order_report9=report9,
        )


def build_live_readonly_session_from_project(
    project_root: str | Path,
    *,
    env_path: str | Path | None = None,
) -> CapitalReplySession:
    root = Path(project_root)
    chosen = Path(env_path) if env_path else find_default_env(root)
    if chosen:
        load_env_file(chosen)
    return CapitalReplySession(project_root=root)
