from __future__ import annotations

class Identity:
    def __init__(self, config, workspace):
        self.config, self.workspace = config, workspace
        self.observed_ids = set(workspace.load_state("self_ids", []))

    def observe(self, qq) -> None:
        if qq and str(qq).isdigit() and str(qq) not in self.observed_ids:
            self.observed_ids.add(str(qq))
            self.workspace.save_state("self_ids", sorted(self.observed_ids))

    @property
    def ids(self) -> set[str]:
        return self.observed_ids | ({self.config.load().bot_qq} - {""})

    def prompt(self) -> str:
        settings = self.config.load()
        return (f"机器人自身账号 QQ：{', '.join(sorted(self.ids)) or '尚未连接，未识别'}；网名：{settings.nickname}。"
                f"账号说明：{settings.account_description}\n")

