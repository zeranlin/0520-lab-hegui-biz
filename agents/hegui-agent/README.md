# hegui-agent

`hegui-agent` 是政府采购行业招标文件合规性审查生产线的 Agent 封装层。

它只做一件事：接收一个待审查招标文件，读取 `config/hegui.yaml` 指向的只读 LLM Wiki 知识，调用 `config/` 中的 LLM 配置完成审查，并把审查报告和运行记录输出到 `outputs/`。

## 职责边界

`hegui_cli.py` 不是审查知识库，不在 biz 侧沉淀政府采购业务规则。

biz 侧负责：

- 按 LLM Wiki 入口指引调度审查生产线。
- 记录各阶段使用的 Wiki 页面、动作、风险审查点和原文证据。
- 当 Wiki 协议无法稳定驱动审查时，形成反馈记录。
- 维护模型配置、文件抽取、结构化表格可读性、大文件处理、输出隔离、跨平台兼容、运行记录和错误诊断等工程能力。

biz 侧不负责：

- 修改、维护或反哺 LLM Wiki。
- 在执行器内写死业务风险判断。
- 用代码规则替代 Wiki 的审查协议。
- 使用外部标注、标准答案或历史审查记录反推风险点。

相关治理文档：

- `docs/hegui_cli_review_sop.md`
- `docs/hegui_cli_wiki_driven_issues.md`

## 使用方式

原有入口保持不变：

```bash
python agents/hegui-agent/hegui_cli.py '<待审查文件相对路径>'
```

SOP 治理版新入口：

```bash
python agents/hegui-agent/hegui_cli_sop.py '<待审查文件相对路径>'
```

只执行 SOP 运行前检查，不调用模型：

```bash
python agents/hegui-agent/hegui_cli_sop.py --sop-check-only
```

待审查文件可以使用本项目内相对路径，也可以使用本项目内文件的绝对路径。文件画像、品类路由和专项动作由 LLM Wiki 审查协议决定。

Windows 下请使用 `python` 显式启动脚本。默认要求 `lab-hegui-biz` 和 `lab-hegui-llm` 位于同一上级目录；如知识库在其他位置，可以设置 `HEGUI_WIKI_HOME`。

## 运行时

`hegui_cli.py` 直接读取本项目配置：

```text
config/hegui.yaml
config/config.toml
config/auth.json
```

`config/config.toml` 提供模型、服务地址等配置，`config/auth.json` 提供鉴权信息。执行器直接调用 LLM 服务完成分阶段审查。

## 输出

```text
outputs/对应分类/xxx-审查报告.md
outputs/对应分类/xxx-AI调度运行记录.md
```

## V0.1 范围

- 只支持单文件招标文件审查。
- 只读取 `lab-hegui-llm` 既有知识。
- 不修改、不更新、不反哺 LLM Wiki。
- 不生成知识更新申请、人工复盘记录或来源笔记。
- 必须生成审查报告和 AI 调度运行记录。
- 不对 LLM Wiki 运行 `./lint`、`./ingest`、`./query` 等维护命令。
- 不得修改 `raw/招标文件/` 原始文件。
- 不得使用外部 V2、标准答案或人工标注反推风险点。
