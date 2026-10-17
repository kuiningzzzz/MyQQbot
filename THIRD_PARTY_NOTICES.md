# 第三方项目与许可证说明

本项目自身代码采用 [MIT](LICENSE)。本文件说明已确认的第三方使用方式，不把上游实现归为本项目原创，也不改变任何第三方软件的许可证。

核对日期：2026-10-17。依据为本仓库的依赖声明、npm 锁文件、已安装发行包所带的 LICENSE 和上游项目。Python 使用了版本范围，下表中的本地核对版本不等于所有部署都会安装该版本；最终以实际发行包为准。

## 直接复用的主要项目

| 项目 | 本项目如何使用 | 使用位置 | 许可证与声明 |
| --- | --- | --- | --- |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | 安装官方 npm 内核，复用模型适配、Agent 循环、工具、压缩、持久会话及 Spawn/Fork；不是本项目自行实现的推理内核 | `dsh/package.json`、`dsh/profile.yml`、`dsh/plugin.mjs`、`src/feature/ai/dsh_runtime.py` | `0.2.1-rc.2`，MIT；Copyright (c) 2026 DeepSeek。[许可证副本](licenses/DeepSeek-Harness-MIT.txt) |
| [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) | 调用 `download_album`、配置构造及 `Feature.export_pdf`；下载、图片处理和 PDF 导出核心来自上游，本项目负责 QQ 队列、回执、自检和管理界面 | `src/feature/jm/feature.py`、`src/feature/jm/diagnostics.py` | `jmcomic>=2.6.19`，本地核对 `2.7.5`；MIT，Copyright (c) 2023 hect0x7。[许可证副本](licenses/JMComic-Crawler-Python-MIT.txt) |
| [PixivPy](https://github.com/upbit/pixivpy) | 使用 Pixiv API 的认证、作品详情和图片下载接口 | `src/feature/pixiv/client.py` | `pixivpy3>=3.7.5`，本地核对 `3.7.5`；Unlicense。[声明副本](licenses/PixivPy-Unlicense.txt) |
| [get-pixivpy-token](https://github.com/eggplants/get-pixivpy-token) | 调用 `gppt.login` 和浏览器支持代码获取授权；本项目另做独立进程、超时、冷却和凭据存储 | `src/feature/pixiv/login_worker.py`、`src/feature/pixiv/auth.py` | `gppt==5.1.0`；MIT，Copyright 2021 haruna。[许可证副本](licenses/get-pixivpy-token-MIT.txt) |
| [lunar-python](https://github.com/6tail/lunar-python) | 用于农历和公历换算，周期与调度状态由本项目处理 | `src/feature/timing/schedule.py` | `1.4.8`，MIT |
| [Sentence Transformers](https://github.com/huggingface/sentence-transformers) | 加载本地嵌入模型，计算文本向量 | `src/feature/ai/index.py` | Apache-2.0 |
| [BAAI BGE](https://huggingface.co/BAAI/bge-small-zh-v1.5) | 默认使用 `bge-small-zh-v1.5` 模型权重；不是自训练模型 | `src/feature/ai/config.py`、`src/feature/ai/index.py` | 该模型的[模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5/raw/main/README.md)标注 MIT；更换模型后应重新核对 |
| [Chroma](https://github.com/chroma-core/chroma) | 本地持久化向量索引 | `src/feature/ai/index.py` | Apache-2.0 |

这些接入主要通过安装依赖和调用接口完成。`dsh/plugin.mjs`、`dsh/policy.mjs` 及 Python Feature 是本项目的接入层；dsh 原生工具、模型循环及子 Agent 生命周期来自上游，QQ 定时触发由本项目的 Python 模块处理。当前没有把 JM、PixivPy 或 dsh 整仓源码复制到 `src/` 中。

## 其他 Python 依赖

完整的直接依赖入口是 [requirements.txt](requirements.txt)。以下按用途列出许可证，部署及发布时仍需保留安装包自带的版权与许可证文件。

| 用途 | 依赖 | 许可证 |
| --- | --- | --- |
| Web 服务 | FastAPI、Uvicorn；FastAPI 间接依赖 Starlette | MIT；BSD-3-Clause；BSD-3-Clause |
| WebSocket | websockets | BSD-3-Clause |
| 网络请求 | requests、httpx | Apache-2.0；BSD-3-Clause |
| 数据与配置 | pydantic、PyYAML、filelock | MIT |
| 时区 | tzdata | Python 包声明 Apache-2.0，内含时区数据另有自身声明 |
| 账号加密 | cryptography | Apache-2.0 OR BSD-3-Clause，及其发行包附带的第三方声明 |
| 图像与 PDF | Pillow、pypdf、img2pdf | MIT-CMU；BSD-3-Clause；LGPL-3.0-or-later |
| XML 解析保护 | defusedxml | PSFL |
| 文件生成库 | openpyxl、python-docx、reportlab | MIT；MIT；BSD 类许可证，以安装版本 LICENSE 为准 |
| 环境文件 | python-dotenv | 本地核对版本 `1.2.3` 声明 BSD-3-Clause，不能套用本项目 MIT |
| 浏览器自动化 | Playwright，由 gppt 间接引入 | Apache-2.0；所下载的 Chromium 还带有自己的第三方许可证 |
| 嵌入模型运行 | PyTorch 及其依赖 | 包含 BSD/Apache 等多种声明，以实际 PyTorch 发行包的 LICENSE 和 notices 为准 |

`img2pdf` 由 JM 的 PDF 链路使用，也被显式列入依赖。它的 LGPL 许可证没有因为 JM 主库采用 MIT 而改变。发行包附有 LGPLv3 文本，源码声明允许 v3 或后续版本。

## npm 间接依赖和镜像中的系统工具

[dsh/package-lock.json](dsh/package-lock.json) 记录确切的 npm 包版本、来源与完整性信息。[npm 许可证清单](docs/third-party/npm-licenses.tsv)从当前锁文件提取，包含各平台可选包；它不是“本机全部已安装包”或“全部启用插件”的清单。

需要特别留意的条目包括：

| 组件 | 为什么会出现 | 锁文件或上游声明 |
| --- | --- | --- |
| `@deepseek-ai/libreoffice-kit` 及平台/WASM 包 `0.1.5` | dsh 的间接依赖，尽管本项目没有挂载对应功能插件，也可能随完整依赖安装进入制品 | MPL-2.0 |
| `@img/sharp-libvips-*` `1.3.4` | dsh 图像链路间接依赖；具体安装哪一项取决于平台 | LGPL-3.0-or-later |
| 部分 `@img/sharp-*` 平台包 `0.35.5` | 包含平台图像处理运行时 | Apache-2.0、LGPL-3.0-or-later，WASM 包还声明 MIT |
| [Poppler](https://poppler.freedesktop.org/) / `poppler-utils` | Dockerfile 安装，OCR 子进程调用 PDF 工具 | GPL；具体文件与系统依赖以发行版版权声明为准 |
| Tesseract | Dockerfile 安装，调用 OCR 命令 | Apache-2.0，语言模型文件单独保留其声明 |
| Chromium、字体、Node.js、Python、Debian 基础系统 | 镜像运行环境 | 各有许可证及第三方声明，不统一按 MIT 标注 |

分发源码仓库与分发已经装入第三方软件的 Docker 镜像是两种不同的发布物。发布镜像时，需要按实际制品核对许可证、版权告知和适用的源码提供要求，保留 `/usr/share/doc` 及各包自带的 LICENSE/NOTICE 等文件。仅有本文件不能替代镜像的完整依赖与许可证清单。

## 外部服务和参考资料

- [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 是外部运行的 QQ 接入服务，本项目通过 OneBot 协议通信；没有将其二进制或源码打进自己的镜像。NapCat 的使用条件由其上游说明。
- DeepSeek 模型通过远程 API 调用，本项目不包含其模型权重；API 服务使用条件与代码的 MIT 许可证是不同事项。
- JM 接入参考了 [jmcomic 的 PDF 导出说明](https://jmcomic.readthedocs.io/zh-cn/latest/tutorial/13_export_and_feature/)并直接调用其 Feature 接口。没有将该下载/PDF 核心声明为本项目原创。
- Pixiv 管理页面链接到 [PixivPy 推荐的 OAuth 示例](https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362)。当前只是提供外部链接，没有将脚本内嵌或在服务端自动下载执行。后续若要复制脚本，需先核对该脚本自己的许可。
- `tutorial.md` 提到的 Yunzai、Redis/Valkey 等是历史教程中的外部项目，不是本项目当前运行依赖。

后续若直接复制或修改上游源码，应在相关文件保留原作者、来源、版本及许可证说明，并同步更新本文件。各上游保留自己的名称和版权；此处列出项目不表示上游为本项目背书。
