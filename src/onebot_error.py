class OneBotError(RuntimeError):
    def __init__(self, action, response):
        self.response = response
        self.retcode = response.get("retcode")
        self.detail = str(response.get("message") or response.get("wording") or "NapCat未提供原因")
        super().__init__(f"NapCat {action} 失败：{self.detail}")

    @property
    def permission_denied(self):
        text = (str(self.response.get("message", "")) + " " + str(self.response.get("wording", ""))).lower()
        return any(word in text for word in ("权限不足", "无权限", "没有权限", "无权", "permission denied", "no permission", "insufficient permission"))
