import json

ASKED = "asked"


class AskedOnly:
    def _recover(self, heartbeat: object) -> str | None:
        state = json.loads(self.entry.value)
        status = state["state"]
        if status == ASKED and state.get("owner") == self.config.hive:
            return self._ask_again(heartbeat)
        if status == "started":
            self._publish(state)
        elif status == "launching":
            return "uncertain"
        elif status == "claimed":
            # ruleid: hive-spoke-recovery-keeps-own-claimed-busy
            return "busy"
        return None


class LaunchingOnly:
    def _recover(self) -> str | None:
        state = json.loads(self.entry.value)
        status = state["state"]
        if status == "launching" and state.get("owner") == self.config.hive:
            status = "ready"
        if status == "claimed":
            # ruleid: hive-spoke-recovery-keeps-own-claimed-busy
            return "busy"
        return None


class ReversedTest:
    def _recover(self) -> str | None:
        status = json.loads(self.entry.value)["state"]
        if "claimed" == status:
            # ruleid: hive-spoke-recovery-keeps-own-claimed-busy
            return "busy"
        return None


class MemberTest:
    def _recover(self) -> str | None:
        status = json.loads(self.entry.value)["state"]
        if status in ("claimed", "launching"):
            # ruleid: hive-spoke-recovery-keeps-own-claimed-busy
            return "busy"
        return None


class OwnClaimedAskedAgain:
    def _recover(self, heartbeat: object) -> str | None:
        state = json.loads(self.entry.value)
        status = state["state"]
        if status in (ASKED, "claimed") and state.get("owner") == self.config.hive:
            return self._ask_again(heartbeat)
        if status == "started":
            self._publish(state)
        elif status == "launching":
            return "uncertain"
        elif status == "claimed":
            # ok: hive-spoke-recovery-keeps-own-claimed-busy
            return "busy"
        return None


class OwnClaimedBranch:
    def _recover(self, heartbeat: object) -> str | None:
        state = json.loads(self.entry.value)
        status = state["state"]
        if status == "claimed" and state.get("owner") == self.config.hive:
            return self._ask_again(heartbeat)
        if status == "claimed":
            # ok: hive-spoke-recovery-keeps-own-claimed-busy
            return "busy"
        return None


class LaunchingBusy:
    def _recover(self) -> str | None:
        status = json.loads(self.entry.value)["state"]
        if status == "launching":
            # ok: hive-spoke-recovery-keeps-own-claimed-busy
            return "busy"
        return None
