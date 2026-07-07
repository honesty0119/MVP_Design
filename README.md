# 市场线索到 MQL/SQL 的简化系统

一个无第三方依赖、开箱即用的候选人作业 MVP。重点不是 UI 堆叠，而是演示线索进入、清洗、分配、跟进、MQL/SQL 转化与指标统计的业务闭环。

## 1 分钟启动

要求：Python 3.10+。

```bash
python app.py
```

浏览器打开 `http://127.0.0.1:8000`。首次启动自动创建 SQLite 数据库和 10 条模拟线索。

重置演示数据：

```bash
python app.py --reset
```

运行测试：

```bash
python -m unittest -v
```

## 已实现

- 新增模拟线索，手机号唯一去重
- 按状态、来源、负责人筛选
- 销售负责人分配
- 跟进记录与最近跟进时间
- 受约束的状态机及完整状态历史
- MQL/SQL 准入校验
- 线索评分与 MQL 判定解释：展示加分项、阻塞项和是否建议转 MQL
- 轻量角色边界：页面可切换销售/主管，销售只能操作自己负责的线索
- 审计记录：关键写操作记录操作人、动作和时间
- 漏斗、转化率、48 小时未跟进提醒
- 可视化状态漏斗、指标口径说明、超时线索一键筛选
- OpenAI-compatible LLM Agent：模型选择受控数据工具并基于真实结果回答
- Agent 执行链路展示：问题、工具、参数、服务端校验和数据边界可见
- 未配置密钥或模型故障时自动回退规则查询，Demo 不失效
- CSV 页面导入与去重、CSV 导出 API
- 具备事件幂等性与防回滚边界的模拟外呼回调

## 演示路径

1. 查看漏斗与“跟进超时”提醒。
2. 新增一条模拟线索，验证重复手机号不会创建两条记录。
3. 打开详情，分配负责人并新增跟进。
4. 查看线索评分与 MQL 阻塞项，说明为什么“不能只靠按钮改状态”。
5. 按允许路径推动状态；尝试跨级跳到 SQL，观察系统拒绝。
6. 切换当前操作人为另一名销售，演示跨负责人操作被服务端拒绝；主管可处理转移和复核。
7. 对无效线索填写原因；对 SQL 填写明确商机说明。

## 技术与目录

- `app.py`：Python 标准库 HTTP 服务、API、SQLite 访问与业务规则
- `agent.py`：LLM → Tool Calling → 工具结果回填 → 最终回答的 Agent 循环
- `static/index.html`：原生 HTML/CSS/JS 单页界面
- `schema.sql`：数据模型与索引
- `tests.py`：关键边界测试
- `业务设计文档.docx`：2–4 页业务设计
- `AI-coding使用说明.md`：AI 参与、问题、人工修正及迭代计划
- `项目复盘与改进说明.md`：截图复盘、改进理由与仍未覆盖的生产边界
- `演示脚本.md`：3–5 分钟现场演示顺序与讲解要点

## API 摘要

- `GET/POST /api/leads`
- `GET /api/leads/{id}`
- `POST /api/leads/{id}/assign`
- `POST /api/leads/{id}/follow-ups`
- `POST /api/leads/{id}/status`
- `GET /api/funnel`
- `POST /api/import`（JSON rows，单次最多 500 条）
- `POST /api/query`（受控 Agent 查询，不执行任意 SQL）
- `POST /api/callback/call`（按 `event_id` 幂等）
- `GET /api/export.csv`

## 明确边界

- 全部数据均为模拟数据；不接触真实手机号或客户隐私。
- Demo 已实现轻量角色模拟与服务端权限校验，但不等同于真实登录；生产环境必须接入统一身份、会话、数据范围和审计策略。
- 无效线索可“重新打开”仅用于演示纠错，且强制填写原因并要求主管角色；生产环境还应记录复核意见、审批链路和原状态保留。
- 转化率采用当前存量快照口径，适合 Demo；经营分析应使用按进入时间分 cohort 的口径，避免跨周期分母失真。
- 三方回调已演示事件落库、去重与合法状态推进；生产环境还需签名验真、重试队列、死信、乱序版本和字段映射版本。

## 接入真实 LLM Agent

本项目兼容 OpenAI `/chat/completions` 与 Tool Calling 协议，不增加第三方 Python 依赖。密钥只从操作系统环境变量读取。

PowerShell 示例：

```powershell
$env:AGENT_LLM_MODE="openai-compatible"
$env:AGENT_LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:AGENT_LLM_MODEL="qwen-plus"
$env:OPENAI_API_KEY="你的密钥"
python app.py
```

也可参照 `.env.example` 切换到其他 OpenAI-compatible 服务。Agent 只开放三个查询工具：

- `channel_sql_conversion`
- `overdue_leads`
- `funnel_snapshot`

模型无法执行任意 SQL。若未配置密钥，`auto` 模式会自动使用规则回退，并在页面标注实际执行模式。

## 面试现场追问题库

**如果每天新增 100 条无效线索，怎么看来源问题？**  
先按来源/渠道聚合无效率，再拆无效原因，如空号、重复、无需求、预算不匹配。当前 Demo 已保留来源、渠道和无效原因；生产环境还应接入投放成本和落地页版本，避免只看数量不看成本。

**同一个手机号从多个渠道重复进线，归因怎么处理？**  
Demo 用手机号唯一并保留首次归因，避免重复创建。生产环境应拆成“客户主体”和“线索记录”，按业务线、时间窗口、最后触点/首触点规则做归因，不能简单覆盖原记录。

**外呼回调失败、重复或延迟怎么办？**  
当前用 `event_id` 幂等，且只有待外呼状态能被回调推进。生产环境还需要验签、重试队列、死信队列、业务版本号和乱序处理，避免旧事件覆盖人工后续操作。

**Agent 为什么不直接做 Text-to-SQL？**  
本作业的重点是稳定业务口径。Agent 只选择受控工具，服务端执行预定义参数化查询并显示数据边界；这样能防止任意 SQL、字段泄露和指标口径漂移。

**如果扩展成真实系统，节奏怎么排？**  
第一周补真实登录/RBAC、审计和导入任务；第二周接外呼/企微回调与可靠重试；一个月内补 cohort 漏斗、渠道成本、阶段停留时长和异常运营看板。
