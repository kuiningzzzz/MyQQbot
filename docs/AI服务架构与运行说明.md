# AI服务架构与运行说明

## 约定与模块

本实现基于 `memory_structure.md` 以及后续确认：取消多人设；各场景共享人格和记忆；整合Agent可以读取画像及向量索引；一天为北京时间03:00至次日03:00。文件名使用该周期的开始日期。

```
NapCat事件 → QQBot接收队列 → AiFeature
  ├─ 图片/附件 → FileAgent → 描述
  ├─ 原始记录 → raw/YYYY-MM-DD.md（只追加）
  └─ ContextManager（群聊与私聊、发送者和机器人身份标记）
        → ChatEngine → send_message（当前会话）
                    → query_memory → AttentionAgent → BGE/Chroma → Markdown回读
                    → web_search → Tavily/SearXNG
        → 回复完成 → 后台IntegrationAgent → 画像更新 → 索引同步
03:00调度 → 上一完整周期整合 → 每7天碎片整理 → 清理已处理旧日志
```

主Agent与子Agent是应用内的独立模型调用、提示词和权限入口，不会创建系统进程或执行模型生成的Python代码。主Agent不提供文件读取工具；Attention仅能查询user/common；整合器读取raw及已有画像、利用向量匹配并更新画像；AGENT.md只由管理端写入。向量服务不索引raw。

所有Agent都收到机器人QQ号、从NapCat观察到的自身账号、网名和账号说明。机器人消息保留在上下文及原始日志中，但不能独立成为用户事实来源；代码拒绝生成自身QQ目录，并要求提炼候选引用本批真实人类消息。

## 安装与运行

Python 3.11及以上。安装 `requirements.txt` 后运行 `python -m src.main`，管理界面仍使用10010端口。首次创建 `data/ai/` 时建立目录树、默认AGENT.md和config.yaml；只从旧版配置继承API地址和Key，旧人设、历史、SQLite和新闻数据均不导入、不删除。

在“AI服务”卡片填写账号身份、人设和API配置。文本主模型默认deepseek-v4-flash，视觉模型deepseek-v4-flash-vision-exp。

本地BGE模型首次使用时从Hugging Face下载，缓存到 `data/ai/.vector_cache/models/`，可填写已经下载好的本地模型路径。CPU模式不需要GPU。Docker挂载整个data目录即可保留模型、索引和记忆。安装PyTorch会增大镜像与依赖体积。

执行 `python scripts/verify_ai_vector.py` 可下载本地模型并用合成数据验证真实语义检索；测试数据放在被Git忽略的 `data/ai-vector-validation/`，不进入生产记忆。此脚本不调用付费聊天模型。

## 配置

WebUI写入 `data/ai/config.yaml`。环境变量 `AI_字段大写` 优先于文件，例如 `AI_MODEL`、`AI_BOT_QQ`、`AI_EMBEDDING_MODEL`。文档原列出的RAW_RETENTION_DAYS、CONTEXT_RECENT_COUNT、CONTEXT_COMPRESS_THRESHOLD、VECTOR_INDEX_ENABLED、VECTOR_MERGE_THRESHOLD、TIMEZONE也支持无前缀形式。

关键默认值：上下文1,000,000 Token、压缩比例0.8、压缩后每场景20条、输出8192 Token、请求总超时60秒、工具最多8轮、原始日志保留7周期、碎片整理7天一次。

Token使用保守字符估算，不是服务端精确tokenizer。所有模型调用发送前检查预算。上下文压缩包含原有摘要及旧消息；如果各场景最近消息本身仍超过预算，返回明确错误，不悄悄删除这些消息。摘要生成期间仍可接收新消息，压缩提交仅替换快照内的旧消息。

原有唤醒机制保留：@或网名直接唤醒、活跃期60秒，普通活跃消息允许不回复。每轮通过send_message发送最多4条，工具目标固定为触发会话，不能任意指定其他群或用户。请求使用SSE并完整组装工具调用，QQ不会接收token碎片。新消息更新会话版本号；版本过期时不再执行旧结果中的发送工具。

## 记忆工作流

主Agent按需调用Attention。先做Top-10检索，再从原文回读完整条目；Attention筛选最多7条并返回来源、摘要与信息是否充足。中文向量使用BGE的查询指令，索引按条目切片，QQ检索在数据库内过滤。索引失效时明确降级为词面检索，不冒充语义检索；同步失败设置持久化dirty标记，下次同步重建。

整合器每批最多300条逻辑原始记录，提取候选后利用向量检索和旧文件做条目对照。相同事实刷新时间；冲突按文档的随机权重决策，旧分为max(0,100-5×天数)，新分为50/80/20。原子更新采用临时文件、fsync、os.replace，文件锁避免并发损坏，内容hash避免模型等待期间覆盖人工编辑。失败不标记批次完成。

增量整合合并待处理快照，同一时间只有一个整合写入器。处理回执按原始记录哈希去重，日任务不会反复学习已经成功处理的同一行。每天03:00处理刚结束周期；开机后扫描未完成周期。处理失败次分钟重试。过期且已整合的日志才清理；失败日志保留并记录错误。每7天对相似度>0.8条目提出同义合并，未纳入合并的条目保留。

文档中的RECENTNESS是可检索的近期资料，不是到点提醒服务。当前碎片整理合并重复资料，不自动推断和删除“已过时”的事实，人工可以在文件编辑器维护。

## 附件与事件

图片按DeepSeek官方 `image_url` 内容块调用视觉模型，共用主模型API Key。普通文件最大20MiB；文本、PDF前40页、DOCX先提取文本再交给文本模型。扫描PDF无可提取文字时明确提示改发图片。二进制文件可通过base64 data URL传入；不接受任意本机路径。未解析附件仍留下明确占位记录。

有完整节点的转发递归展开；只有转发ID时，通过OneBot get_forward_msg读取。群文件优先通过get_group_file_url取得下载地址，get_file返回本地路径时使用其可选Base64内容回退；也记录group_upload通知。QQBot接收循环与事件消费分离，所以等待OneBot动作回执不会堵塞回执自身。

撤回事件只追加系统标记，原消息不删除；原内容优先从当前上下文查找，无法定位时保留消息ID。普通文本中的换行与分隔符转义，避免伪装成新的日志记录；转发子消息使用缩进。机器人文本回复主动入库，10秒内的对应回显去重。

## AI 撤回消息

主 Agent 新增 `inspect_message` 和 `recall_message` 工具。发送者本人明确申请撤回自己的消息时，Agent直接批准；第三方申请则由Agent结合上下文判断，不要求申请者必须是群管理员。机器人也可撤回自己的误发消息。目标不明确时先询问，不能猜测消息ID。

可以引用目标消息并 @机器人说“撤回我这条消息”；请求撤回别人消息时，可说明原因供Agent判断。沿用现有 @、昵称唤醒和活跃期规则。程序只允许操作当前会话上下文或当前引用中的消息，引用消息会通过 `get_msg` 校验场景和真实发送者。跨场景上下文不授予跨场景撤回能力。

机器人文本发送现在等待OneBot回执，保存真实message_id；上下文保留消息ID和引用ID，撤回成功后标记recalled，原始记录仍保留。此改动也意味着发送失败或回执超时会被明确报告，不再直接认定发送成功。

撤回返回结构化结果：`recalled`、`permission_denied`（message含“权限不足”）、`recall_failed`、`unknown`（超时，结果未知）、`already_recalled`等。NapCat失败的retcode和原因保留；明确的权限错误映射为permission_denied，模糊失败则查询当前群角色辅助判断。私聊无法撤回对方消息。未知错误不会按某个通用retcode强行归类为权限不足，也不会自动重试结果未知的撤回。

接口依据：[NapCat delete_msg](https://napcat.apifox.cn/226919954e0)。本次29项自动测试通过，包含模拟NapCat回执及Agent工具循环；未在真实QQ群执行撤回，服务器上的具体错误文本、撤回时限仍需实际使用验证。

## 管理接口明细

接口挂在管理服务10010端口，沿用现有项目的可信管理网络边界，不作为公网聊天入口。

| 接口 | 用途 |
| --- | --- |
| GET/POST /api/ai/config | 模型、身份、AGENT.md及运行配置；Key不回传 |
| GET /api/ai/files | 列出画像与通用知识文件 |
| GET/POST /api/ai/file | 读取/原子保存Markdown；保存需expected_hash |
| POST /api/ai/reindex | 重建索引 |
| GET/POST /api/ai/search | 搜索服务配置 |
| POST /memory/record | 记录消息、可选附件理解，并进入上下文 |
| POST /memory/query | Attention检索 |
| POST /memory/integrate | incremental快照或full周期日期 |
| POST /memory/understand_file | 文件/图片描述 |

增量快照使用上下文管理器的场景字典，每个场景含recent_messages，其中每条记录有raw_line。也兼容文档中的scene_id/messages外壳：消息提供qq、nickname、content，可选timestamp和is_self；服务会格式化为带身份的记录。API不会允许Attention或文件编辑器读取raw；检索结果只能引用已回读的候选来源。

## 验证

本次完成21项自动测试、本地BGE/Chroma真实检索、管理页面保存和应用加载验证。尚未配置真实API Key，未进行DeepSeek与QQ在线联调。日任务保存已处理日志的内容摘要，延迟写入已结束周期的消息会在后续调度补做整合。

`python -B -m unittest discover -s tests -v` 使用临时目录、模拟模型与HTTP回执，覆盖03:00边界、原子失败与并发修改检测、身份隔离、消息追加与回显、附件/转发/撤回、来源检索、整合去重、压缩并发、日任务、管理接口和SSE工具解析。真实BGE/Chroma检索另由verify_ai_vector.py覆盖。

外部API来源：
- https://api-docs.deepseek.com/zh-cn/guides/vision/
- https://huggingface.co/BAAI/bge-small-zh-v1.5
- https://docs.trychroma.com/docs/collections/add-data
- https://docs.trychroma.com/docs/querying-collections/query-and-get
