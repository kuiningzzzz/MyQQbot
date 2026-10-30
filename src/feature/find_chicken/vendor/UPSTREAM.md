# 鱼鱼找鸡 1.0.0

来源：用户提供的「鱼鱼找鸡-1.0.0」发行目录，包名 `coser-image-search`。
原作者：小鲸鱼。MIT 许可证见同目录 LICENSE。

保留原版三个源码文件，搜图、结果解析和账号提取来自上游。本项目没有启用
`createSearchFn` / `inferAccounts`、VPN 管理、系统代理修改、临时公网图床等接口，
不调用 DeepSeek，也不读取上游独立配置或 DSH 凭据。

本地修改：浏览器增加 Linux/Playwright 安装位置发现；profile 移到项目 data 下；
补充容器启动参数、启动失败及退出时的进程清理；临时profile随任务清理。QQ、排队、缓存、配置和进程监管
由相邻的 Python Feature 和 worker.mjs 实现，未复用原 OneBot 适配器的任意本地文件输入。
