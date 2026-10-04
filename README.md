# 源码驱动的智能手册与流程平台

已实现附件设计中的本机业务闭环：Java/JDT 索引 → DeepSeek/LangGraph 模块分析与源码问答 → 功能登记和手册候选 → 内容审核、完整知识快照发布 → 按租户实际版本提供帮助与问答 → 只读流程预览、确认、执行及结果查询。TestPilot 通过独立认证的 HTTP API 提供探索、脚本修订、摘要审批、执行和结果对账。

验收结果及尚未具备的生产条件见 [验收报告](docs/验收报告.md)，分析任务的实际状态、预算和七个工具见 [Agent 工作流](docs/源码分析Agent工作流.md)。附件中的正式平台规划与本机验收范围在报告中分别列明。

## 环境与启动

要求 Python 3.12+、uv、Node.js 22.12+、JDK 17+、Maven、Git、PostgreSQL。当前工作目录为 `/root/code_development`，与 TestPilot 的数据库、会话和任务队列分离。

```bash
uv sync --extra dev
mvn -q -f java-indexer/pom.xml package
cd frontend
npm ci
npm run typecheck
npm run build
cd ..
```

将 `.env.example` 复制为本机 `.env`，配置数据库和目录白名单。本次已配置的数据库为本机 `source_knowledge_live`；新实例使用自己的数据库。`DATABASE_URL` 使用 PostgreSQL DSN，留空时进入 SQLite 兼容模式。密钥文件不进入版本控制。

```bash
# Linux peer 认证示例；使用当前操作系统用户对应的 PostgreSQL 角色。
createdb source_knowledge
uv run python -m source_demo.seed
uv run python -m source_demo.doctor
uv run python scripts/dev.py
```

打开内部工作台 `http://127.0.0.1:8000/internal-ui` 和客户门户 `http://127.0.0.1:8000/customer-ui`。本机账号 `analyst`、`editor`、`customer-a`、`customer-b`，默认口令 `demo-only`，由 `DEMO_PASSWORD` 修改。固定账号供许可样本验收使用。接口契约见 `/docs`。

启动器仅监听 127.0.0.1，创建 API、Mock 和 Worker 三个进程；Ctrl+C 关闭它创建的进程。需要分别部署时运行：

```bash
uv run uvicorn source_demo.api:app --host 127.0.0.1 --port 8000
uv run uvicorn source_demo.mock_business:app --host 127.0.0.1 --port 9100
uv run python -m source_demo.jobs
```

Worker 可按任务类型拆开运行，例如 `--kinds repository_index,module_analysis,code_answer` 或 `--kinds workflow_execute,test_run,legacy_test_run`。任务领取使用 PostgreSQL `SKIP LOCKED`、租约续期和 `run_epoch`；业务状态更新使用事务保护。

## Java 接入与源码分析

`SOURCE_ALLOWED_ROOTS` 配置允许接入的目录，多个目录用冒号分隔。内部 `editor` 登记源码目录及已准备的依赖 JAR、Java release；Git 仓库由 `analyst` 固定 ref 或完整 SHA 创建索引，普通目录直接按当前文件生成内容哈希快照。索引不执行业务仓库构建钩子；classpath 由已审阅构建提供。JDT 在临时源码树中解析，原源码不被修改。

模块、符号、方法重载、参数、返回类型、调用候选、继承/实现/覆盖、条件分支、Spring 声明、MyBatis/SQL/配置均带固定快照和行号。缺依赖、无法解析的调用及动态装配保留为缺口，不能用扫描文件数代替功能覆盖率。

接入上限为单文件 1 MB、累计 100 MB、默认 20,000 个允许文件；超限明确失败，不静默遗漏源码。大型工程需先按许可模块拆分输入并建立正式容量基线。

本机已有一个四模块许可样本，包含一个故意无法解析的模块。准备命令：

```bash
uv run python scripts/prepare_java_fixture.py
mvn -q -f fixtures/java-project/pom.xml -pl common,orders,api -am install
mvn -q -f fixtures/java-project/api/pom.xml dependency:copy-dependencies -DoutputDirectory=/root/code_development/data/java-classpath
```

`data/java-fixture` 保留独立 Git 历史，准备脚本不覆盖已有源码。在工作台登记其绝对路径，填写 `data/java-classpath` 下 JAR 的绝对路径，索引 `base`、`change` 或完整提交 SHA。两个历史版本分别包含导出上限 5000、1000，以及策略类移动；历史中的 `broken` 模块保留缺依赖诊断。未提交修改不进入 Git 快照。版本比较测试与评测脚本使用临时仓库。

后续接入的 Java 业务项目统一放在 `data/repositories/<项目名>/`，各项目保留自己的 `.git`；整个 `data/` 由主项目 Git 忽略。`fixtures/` 只存放可复现的回归测试样例，随系统代码提交，测试不依赖本机业务仓库。VS Code 工作区关闭子仓库自动发现，默认只显示主项目仓库。

源码问答支持当前模块和跨模块调用链；引用由服务器校验。模块 Agent 通过薄 LangGraph、七个只读工具、有界预算、持久化检查点和摘要审核产生内部画像。编辑者可以确认功能，再生成手册候选。手册、测试和 TestPilot 运行可以人工登记模块映射，其登记不能证明同构建实测。

Git Webhook 需 `GIT_WEBHOOK_SECRET`、HMAC SHA-256 签名和唯一事件 ID；输入仅为已登记仓库及完整 SHA。索引成功后在事务中为受影响模块入队分析，重复事件不重复批准或发布。

## DeepSeek 与 TestPilot

`TESTPILOT_ENV_FILE` 默认 `/root/testpilot-plus/.env`。DeepSeek 配置依次读取新平台 `DEEPSEEK_*` 和该文件的 `GATEWAY_BASE_URL`、`GATEWAY_MODEL`、`GATEWAY_API_KEY`。密钥在服务端读取，不复制到新数据库、不返回给前端、不写入验收报告。

TestPilot 集成账号使用 `TESTPILOT_EMAIL/PASSWORD`，为空时从其 env 的管理员配置读取。每次建立独立认证会话，通过现有 API 导入资产和核对结果。登录账号的个人模型设置会覆盖服务器模型设置，这是 TestPilot 自身的行为；本次验证只调整了独立数据库副本的个人模型设置。

`TESTPILOT_ALLOW_RUN=true` 允许内部编辑者发起运行。脚本必须先按 SHA-256 摘要批准，提交前再次核对上游摘要。上游无幂等创建契约时，网络异常保留 unknown，不自动重发；明确 4xx 拒绝与 unknown 分开记录，通过“核对目标实际状态”继续对账。执行关联到具体尝试，不能把重试后修订的成功状态用于之前失败的请求；缺少关联或存在多个新尝试时保留 unknown。运行、探索、重新探索、生成、审批、执行和受控重试均有入口。

本次 8099 服务使用独立验证数据库及产物目录；旧系统原数据库、源码和 DTM 框架未被新平台修改。真正的 Browser Agent 查询和 DTM 确定性脚本均已执行。脚本修订包含执行尝试及 Allure 地址；HTML 入口通过新平台仅作附件下载，完整交互报告在 TestPilot 独立服务中查看并使用其登录。

```bash
uv run python -m source_demo.doctor --live
```

此命令验证真实模型响应、独立 TestPilot 认证及只读 Mock，不输出凭据。

## 内容、客户问答与流程

手册支持 Markdown/文本导入、不可变原文、分段、段落哈希补丁、并发基线冲突、摘要审批、完整快照发布及 CAS 回滚。客户显示安全渲染后的 Markdown：原始 HTML 被转义，图片不触发外部请求。源码草稿及内部路径不会直接发布到客户知识。

客户问答按服务端身份、部署版本和当前知识指针检索已发布资料，返回引用、资料不足状态和反馈入口。可见性撤回在查询时生效。运营页提供脱敏问题、知识缺口、任务分配与高频流程候选。

客户流程只运行版本化的 `order.summary.read` 固定只读模板，支持两租户验证、审批、发布、停用、参数校验、有效期预览、摘要确认、幂等执行、取消、撤权核对和结果查询。内部测试工作室验证已批准候选的预期值与实际值，包含刻意失败的断言。TestPilot 的判定结果单独保留为旧系统判定。

## Vue 目录

```text
frontend/src/
  apps/
    internal/           # main.ts、App.vue、内部业务 views
    customer/           # main.ts、App.vue、客户业务 views
  api/                  # HTTP 与错误处理
  components/           # 登录及通用展示组件
  composables/          # 会话、异步动作
  types/                # 接口类型
  assets/styles/        # 公共样式和移动端布局
```

两个 Vite 入口独立构建与提供静态资源。源码页使用 SSE 展示 Agent 进度；客户任务页轮询结果。客户业务页显示操作信息、引用及错误，不展示内部 JSON 调试数据。

## 回归、评测与恢复

```bash
uv run pytest -q
TEST_DATABASE_URL=postgresql:///source_knowledge_tests uv run pytest -q
uv run python scripts/evaluate_source.py --live --repeat 3
/root/testpilot-plus/.venv/bin/python scripts/browser_smoke.py
```

PostgreSQL 回归为每个场景创建独立 schema。普通回归使用临时 SQLite，即使本机 `.env` 指向 PostgreSQL，也不会修改运行中的数据库。真实评测运行 20 道固定 SHA 的问题，各重复三次，记录结构校验、字面边界和人工语义复核状态；模型故障的拒绝和降级保留在记录中。

```bash
uv run python -m source_demo.ops backup data/backups/platform.dump
uv run python -m source_demo.ops verify data/backups/platform.dump
createdb source_knowledge_restored
pg_restore --exit-on-error --dbname=postgresql:///source_knowledge_restored data/backups/platform.dump
```

恢复到新数据库核对后再切换 `DATABASE_URL`。PostgreSQL 备份包含业务数据和 LangGraph 检查点。SQLite 兼容备份另存检查点文件，需暂停 Worker 后形成同一维护窗口的备份。SQLite 数据迁移至空 PostgreSQL 使用 `scripts/migrate_sqlite.py`，拒绝覆盖已有数据；既有 SQLite 检查点保留供审计，不自动跨后端重放未完成分析。
# code_development
