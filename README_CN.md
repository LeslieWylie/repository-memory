# Repository Memory(中文速览)

给 AI agent 的引用优先记忆系统:**一套零依赖运行时,四种接入方式**,答得出必带
出处(commit + 路径 + 行号),答不出明说弃权,绝不编造。英文完整文档见
[README.md](README.md),逐步安装见 [INSTALL.md](INSTALL.md)。

## 三个记忆平面,一次提问同时检索

| 平面 | 内容 | 证据形态 |
|---|---|---|
| repository | 注册的 Git 语料(文档/日报/代码) | 钉死 commit 的引用,可回读校验 |
| memory | 本机 L0–L3 会话记忆 | 记忆层 + 读回校验 |
| team | 评审过的团队决策/失败/方案/交接 | 经验出处(不冒充 Git 引用) |

模型侧只需要一句:`memory_search(query=用户原话)`。答案从
`answerable`/`results` 读;`abstain=true` 就是"库里没证据";
`citation.pinned=false` 表示证据来自工作区而非提交。

## 四种接入

- **Claude Code / Codex**:Skill + 审计 stdio MCP(协议 `2026-07-28`,自动回退
  四个旧版本)
- **OpenClaw**:原生 `repository_memory_*` 工具 + 生命周期自动捕获
- **其他任何宿主**:`repository-memory mcp`(stdio)或直接 CLI
  `search|get|doctor --json`

## 一句话安装(在目标知识仓库根目录)

```bash
curl -fsSL https://raw.githubusercontent.com/LeslieWylie/repository-memory/main/bootstrap.sh | sh -s -- --target auto --source-url "<该仓库的 HTTPS 地址>" --source-branch "$(git branch --show-current)" --json
```

验收铁律:装完跑一条正例(必须带 commit 引用)和一条编造负例(必须弃权),
以命令输出为准,提示词本身不是安装成功的证据。常见坑(SSH alias、git 身份、
OpenClaw 配置路径)见 [INSTALL.md](INSTALL.md)。

## 团队记忆循环

捕获(hook 自动)→ 候选进 Git inbox(`team-publish` 定时)→ 评审激活
(`supervise`,显式)→ 激活分发回 Git → 所有节点 hydrate 后可引用。
候选永远不进回答面;激活是带评审人记录的显式操作。

## 质量门

公共回归集 P@1 / R@5 / MRR / 负例弃权全 1.0(有无 jieba 两条分词路径),
CI 覆盖 Ubuntu/macOS/Windows × Python 3.10/3.12/3.13。任何检索行为变化
必须先过门再合并。

MIT 协议。问题与贡献:见 [CONTRIBUTING.md](CONTRIBUTING.md) 与
[SECURITY.md](SECURITY.md)。
